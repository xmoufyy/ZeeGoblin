"""The tip ledger: manual awards, approvals, voids, disputes, and member-facing totals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.permissions import Actor, Permission, require
from config.settings import Limits
from database.models import (
    AwardSource,
    AwardStatus,
    BatchStatus,
    DiscordAccount,
    Dispute,
    DisputeResolution,
    DisputeStatus,
    EventEntry,
    Payment,
    PaymentAward,
    PaymentBatch,
    TipAward,
    VerificationSource,
    Wallet,
)
from database.session import Database
from services.accounts import DiscordIdentity, active_wallet, ensure_user, find_user_id
from services.audit_service import AuditAction, Page, record_audit
from services.award_states import transition
from services.errors import Conflict, InputError, InvalidTransition, NotFound, PermissionDenied
from utils.currency import format_sol
from utils.timeutil import Clock, utcnow
from utils.validation import ValidationError, clean_text

DUPLICATE_MANUAL_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class AwardRow:
    id: int
    status: str
    source: str
    amount_lamports: int
    reason: str
    event_id: int | None
    discord_user_id: int
    username: str
    payout_wallet: str | None
    batch_id: int | None
    created_at: datetime
    approved_by_discord_id: int | None
    approved_at: datetime | None
    paid_at: datetime | None
    verification_source: str | None
    payment_signature: str | None
    # The member's wallet right now. Payouts use payout_wallet (locked at approval) instead.
    current_wallet: str | None = None

    @property
    def display_wallet(self) -> str | None:
        return self.payout_wallet or self.current_wallet


@dataclass(frozen=True)
class BulkApproval:
    approved: int
    total_lamports: int
    skipped_own: int
    skipped_no_wallet: int


@dataclass(frozen=True)
class StatusTotal:
    count: int
    lamports: int


@dataclass(frozen=True)
class LedgerSummary:
    by_status: dict[AwardStatus, StatusTotal]
    open_batches: int
    open_disputes: int


@dataclass(frozen=True)
class ProfileSummary:
    discord_user_id: int
    wallet: str | None
    wallet_registered_at: datetime | None
    by_status: dict[AwardStatus, StatusTotal]
    earned_lamports: int
    paid_manual_lamports: int
    paid_other_lamports: int
    rollcall_count: int


def award_rows_query() -> Select:
    signature = (
        select(Payment.signature)
        .join(PaymentAward, PaymentAward.payment_id == Payment.id)
        .where(PaymentAward.award_id == TipAward.id)
        .correlate(TipAward)
        .scalar_subquery()
    )
    current_wallet = (
        select(Wallet.address)
        .where(Wallet.user_id == TipAward.user_id, Wallet.is_active.is_(True))
        .correlate(TipAward)
        .scalar_subquery()
    )
    return select(
        TipAward,
        DiscordAccount.discord_user_id,
        DiscordAccount.username,
        signature,
        current_wallet,
    ).join(DiscordAccount, DiscordAccount.user_id == TipAward.user_id)


def to_row(
    award: TipAward,
    discord_id: int,
    username: str,
    signature: str | None,
    current_wallet: str | None = None,
) -> AwardRow:
    return AwardRow(
        id=award.id,
        status=award.status,
        source=award.source,
        amount_lamports=award.amount_lamports,
        reason=award.reason,
        event_id=award.event_id,
        discord_user_id=discord_id,
        username=username,
        payout_wallet=award.payout_wallet,
        batch_id=award.batch_id,
        created_at=award.created_at,
        approved_by_discord_id=award.approved_by_discord_id,
        approved_at=award.approved_at,
        paid_at=award.paid_at,
        verification_source=award.verification_source,
        payment_signature=signature,
        current_wallet=current_wallet,
    )


async def _status_totals(session: AsyncSession, *conditions) -> dict[AwardStatus, StatusTotal]:  # type: ignore[no-untyped-def]
    rows = await session.execute(
        select(TipAward.status, func.count(), func.coalesce(func.sum(TipAward.amount_lamports), 0))
        .where(*conditions)
        .group_by(TipAward.status)
    )
    totals = {status: StatusTotal(0, 0) for status in AwardStatus}
    for status, count, lamports in rows:
        totals[AwardStatus(status)] = StatusTotal(int(count), int(lamports))
    return totals


def _clean(value: str | None, field: str, *, required: bool = True, max_length: int = 300) -> str:
    try:
        return clean_text(value, field=field, required=required, max_length=max_length)
    except ValidationError as exc:
        raise InputError(str(exc)) from exc


class TipService:
    def __init__(self, db: Database, limits: Limits, clock: Clock = utcnow) -> None:
        self._db = db
        self._limits = limits
        self._clock = clock

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _load(session: AsyncSession, award_id: int) -> tuple[TipAward, int]:
        row = (
            await session.execute(
                select(TipAward, DiscordAccount.discord_user_id)
                .join(DiscordAccount, DiscordAccount.user_id == TipAward.user_id)
                .where(TipAward.id == award_id)
            )
        ).first()
        if row is None:
            raise NotFound(f"Award #{award_id} doesn't exist.")
        return row[0], row[1]

    def _check_not_own(self, actor: Actor, recipient_discord_id: int, verb: str) -> None:
        if recipient_discord_id == actor.discord_id and not actor.can(Permission.APPROVE_OWN_AWARD):
            raise PermissionDenied(f"You can't {verb} your own award. Ask another staff member.")

    async def _approve_in_session(
        self, session: AsyncSession, actor: Actor, award: TipAward, now: datetime
    ) -> None:
        wallet = await active_wallet(session, award.user_id)
        if wallet is None:
            raise InvalidTransition(f"Award #{award.id}'s recipient has no registered wallet.")
        transition(
            session,
            award,
            AwardStatus.APPROVED,
            actor_id=actor.discord_id,
            action=AuditAction.AWARD_APPROVED,
            details={"payout_wallet": wallet.address, "amount_lamports": award.amount_lamports},
        )
        award.payout_wallet = wallet.address
        award.approved_by_discord_id = actor.discord_id
        award.approved_at = now

    # ------------------------------------------------------------------ creation

    async def add_manual(
        self, actor: Actor, recipient: DiscordIdentity, amount_lamports: int, reason: str
    ) -> AwardRow:
        require(actor, Permission.AWARD_REQUEST)
        reason = _clean(reason, "Reason")
        if amount_lamports <= 0:
            raise InputError("Amount must be greater than zero.")
        if amount_lamports > self._limits.max_manual_award:
            raise InputError(
                f"Manual awards are limited to {format_sol(self._limits.max_manual_award)} SOL."
            )
        if recipient.discord_id == actor.discord_id and not actor.is_owner:
            raise PermissionDenied("You can't create an award for yourself.")

        now = self._clock()
        async with self._db.transaction() as session:
            user = await ensure_user(session, recipient)
            duplicate = await session.scalar(
                select(TipAward.id).where(
                    TipAward.user_id == user.id,
                    TipAward.source == AwardSource.MANUAL,
                    TipAward.amount_lamports == amount_lamports,
                    TipAward.reason == reason,
                    TipAward.status != AwardStatus.VOIDED,
                    TipAward.created_at >= now - DUPLICATE_MANUAL_WINDOW,
                )
            )
            if duplicate is not None:
                raise Conflict(
                    f"Award #{duplicate} with the same member, amount and reason was created "
                    "moments ago. Change the reason if this really is a second award."
                )
            has_wallet = await active_wallet(session, user.id) is not None
            status = AwardStatus.PENDING_REVIEW if has_wallet else AwardStatus.AWAITING_WALLET
            award = TipAward(
                user_id=user.id,
                source=AwardSource.MANUAL,
                reason=reason,
                amount_lamports=amount_lamports,
                status=status,
                created_by_discord_id=actor.discord_id,
                created_at=now,
            )
            session.add(award)
            await session.flush()
            record_audit(
                session,
                actor_id=actor.discord_id,
                action=AuditAction.AWARD_MANUAL_REQUESTED,
                entity_type="award",
                entity_id=award.id,
                after={
                    "status": status,
                    "user_id": user.id,
                    "recipient_discord_id": recipient.discord_id,
                    "amount_lamports": amount_lamports,
                    "source": AwardSource.MANUAL,
                },
                reason=reason,
            )
            return to_row(award, recipient.discord_id, recipient.username, None)

    # ------------------------------------------------------------------ review

    async def approve(self, actor: Actor, award_id: int) -> AwardRow:
        require(actor, Permission.AWARD_APPROVE)
        now = self._clock()
        async with self._db.transaction() as session:
            award, discord_id = await self._load(session, award_id)
            self._check_not_own(actor, discord_id, "approve")
            await self._approve_in_session(session, actor, award, now)
            return await self._row(session, award.id)

    async def approve_event(self, actor: Actor, event_id: int) -> BulkApproval:
        require(actor, Permission.AWARD_APPROVE)
        now = self._clock()
        async with self._db.transaction() as session:
            rows = (
                await session.execute(
                    select(TipAward, DiscordAccount.discord_user_id)
                    .join(DiscordAccount, DiscordAccount.user_id == TipAward.user_id)
                    .where(
                        TipAward.event_id == event_id,
                        TipAward.status == AwardStatus.PENDING_REVIEW,
                    )
                    .order_by(TipAward.id)
                )
            ).all()
            approved = total = skipped_own = skipped_wallet = 0
            for award, discord_id in rows:
                if discord_id == actor.discord_id and not actor.can(Permission.APPROVE_OWN_AWARD):
                    skipped_own += 1
                    continue
                if await active_wallet(session, award.user_id) is None:
                    skipped_wallet += 1
                    continue
                await self._approve_in_session(session, actor, award, now)
                approved += 1
                total += award.amount_lamports
            return BulkApproval(approved, total, skipped_own, skipped_wallet)

    async def void(self, actor: Actor, award_id: int, reason: str) -> AwardRow:
        require(actor, Permission.AWARD_VOID)
        reason = _clean(reason, "Reason")
        now = self._clock()
        async with self._db.transaction() as session:
            award, _ = await self._load(session, award_id)
            transition(
                session,
                award,
                AwardStatus.VOIDED,
                actor_id=actor.discord_id,
                action=AuditAction.AWARD_VOIDED,
                reason=reason,
            )
            award.voided_by_discord_id = actor.discord_id
            award.voided_at = now
            award.void_reason = reason
            await self._close_open_dispute(
                session, award.id, actor, DisputeResolution.VOIDED, reason, now
            )
            return await self._row(session, award.id)

    async def return_to_review(self, actor: Actor, award_id: int, reason: str) -> AwardRow:
        """Explicit moderator path to change an approved award's payout wallet.

        The award goes back to review with its snapshot cleared; re-approval snapshots the
        member's *current* wallet. Queued/submitted/paid awards cannot be redirected.
        """
        require(actor, Permission.AWARD_REWALLET)
        reason = _clean(reason, "Reason")
        async with self._db.transaction() as session:
            award, _ = await self._load(session, award_id)
            if award.status != AwardStatus.APPROVED:
                raise InvalidTransition(
                    f"Only approved (not yet batched) awards can be sent back for wallet review. "
                    f"Award #{award.id} is {award.status}."
                )
            previous_wallet = award.payout_wallet
            transition(
                session,
                award,
                AwardStatus.PENDING_REVIEW,
                actor_id=actor.discord_id,
                action=AuditAction.AWARD_RETURNED_TO_REVIEW,
                reason=reason,
                details={"previous_payout_wallet": previous_wallet},
            )
            award.payout_wallet = None
            award.approved_by_discord_id = None
            award.approved_at = None
            return await self._row(session, award.id)

    # ------------------------------------------------------------------ disputes

    async def flag(self, actor: Actor, award_id: int, reason: str) -> Dispute:
        require(actor, Permission.AWARD_FLAG)
        reason = _clean(reason, "Reason")
        now = self._clock()
        async with self._db.transaction() as session:
            award, _ = await self._load(session, award_id)
            if award.status == AwardStatus.QUEUED:
                raise InvalidTransition(
                    f"Award #{award.id} is in open batch #{award.batch_id}. Cancel the batch "
                    "first (or flag it after the payment is recorded)."
                )
            previous = award.status
            payment_id = await session.scalar(
                select(PaymentAward.payment_id).where(PaymentAward.award_id == award.id)
            )
            transition(
                session,
                award,
                AwardStatus.DISPUTED,
                actor_id=actor.discord_id,
                action=AuditAction.AWARD_FLAGGED,
                reason=reason,
            )
            dispute = Dispute(
                award_id=award.id,
                payment_id=payment_id,
                status=DisputeStatus.OPEN,
                previous_status=previous,
                reason=reason,
                opened_by_discord_id=actor.discord_id,
                opened_at=now,
            )
            session.add(dispute)
            await session.flush()
            return dispute

    async def resolve_dispute(
        self, actor: Actor, dispute_id: int, resolution: DisputeResolution, note: str
    ) -> Dispute:
        require(actor, Permission.DISPUTE_RESOLVE)
        note = _clean(note, "Resolution note")
        now = self._clock()
        async with self._db.transaction() as session:
            dispute = await session.get(Dispute, dispute_id)
            if dispute is None:
                raise NotFound(f"Dispute #{dispute_id} doesn't exist.")
            if dispute.status != DisputeStatus.OPEN:
                raise Conflict(f"Dispute #{dispute_id} is already resolved.")
            award, _ = await self._load(session, dispute.award_id)
            has_payment = (
                await session.scalar(
                    select(PaymentAward.id).where(PaymentAward.award_id == award.id)
                )
                is not None
            )

            if resolution == DisputeResolution.REOPENED:
                if has_payment:
                    raise InvalidTransition(
                        "A payment was already recorded for this award, so reopening it could "
                        "pay it twice. Resolve as `settled` (it was paid) or `voided`."
                    )
                transition(
                    session,
                    award,
                    AwardStatus.PENDING_REVIEW,
                    actor_id=actor.discord_id,
                    action=AuditAction.DISPUTE_RESOLVED,
                    reason=note,
                    details={"dispute_id": dispute.id, "resolution": resolution},
                )
                award.payout_wallet = None
                award.approved_by_discord_id = None
                award.approved_at = None
                if await active_wallet(session, award.user_id) is None:
                    transition(
                        session,
                        award,
                        AwardStatus.AWAITING_WALLET,
                        actor_id=None,
                        action=AuditAction.AWARD_WALLET_MISSING,
                    )
            elif resolution == DisputeResolution.VOIDED:
                transition(
                    session,
                    award,
                    AwardStatus.VOIDED,
                    actor_id=actor.discord_id,
                    action=AuditAction.AWARD_VOIDED,
                    reason=note,
                    details={"dispute_id": dispute.id},
                )
                award.voided_by_discord_id = actor.discord_id
                award.voided_at = now
                award.void_reason = note
            else:
                if not has_payment:
                    raise InvalidTransition(
                        "No payment is recorded for this award, so it can't be settled as paid. "
                        "Record the payment first, or resolve as `reopened` / `voided`."
                    )
                transition(
                    session,
                    award,
                    AwardStatus.PAID,
                    actor_id=actor.discord_id,
                    action=AuditAction.DISPUTE_RESOLVED,
                    reason=note,
                    details={
                        "dispute_id": dispute.id,
                        "resolution": resolution,
                        "verification_source": VerificationSource.DISPUTE_RESOLUTION,
                    },
                )
                award.paid_at = now
                award.verification_source = VerificationSource.DISPUTE_RESOLUTION

            dispute.status = DisputeStatus.RESOLVED
            dispute.resolution = resolution
            dispute.resolution_note = note
            dispute.resolved_by_discord_id = actor.discord_id
            dispute.resolved_at = now
            record_audit(
                session,
                actor_id=actor.discord_id,
                action=AuditAction.DISPUTE_RESOLVED,
                entity_type="dispute",
                entity_id=dispute.id,
                before={"status": DisputeStatus.OPEN},
                after={"status": DisputeStatus.RESOLVED, "resolution": resolution},
                reason=note,
            )
            if award.batch_id is not None:
                await maybe_complete_batch(session, award.batch_id, now)
            return dispute

    async def _close_open_dispute(
        self,
        session: AsyncSession,
        award_id: int,
        actor: Actor,
        resolution: DisputeResolution,
        note: str,
        now: datetime,
    ) -> None:
        dispute = await session.scalar(
            select(Dispute).where(
                Dispute.award_id == award_id, Dispute.status == DisputeStatus.OPEN
            )
        )
        if dispute is None:
            return
        dispute.status = DisputeStatus.RESOLVED
        dispute.resolution = resolution
        dispute.resolution_note = note
        dispute.resolved_by_discord_id = actor.discord_id
        dispute.resolved_at = now
        record_audit(
            session,
            actor_id=actor.discord_id,
            action=AuditAction.DISPUTE_RESOLVED,
            entity_type="dispute",
            entity_id=dispute.id,
            after={"status": DisputeStatus.RESOLVED, "resolution": resolution},
            reason=note,
        )

    async def get_dispute(self, actor: Actor, dispute_id: int) -> Dispute:
        require(actor, Permission.AWARD_VIEW_QUEUE)
        async with self._db.transaction() as session:
            dispute = await session.get(Dispute, dispute_id)
            if dispute is None:
                raise NotFound(f"Dispute #{dispute_id} doesn't exist.")
            return dispute

    async def list_disputes(
        self, actor: Actor, *, open_only: bool = True, page: int = 1, page_size: int = 10
    ) -> Page[Dispute]:
        require(actor, Permission.AWARD_VIEW_QUEUE)
        page = max(1, page)
        conditions = [Dispute.status == DisputeStatus.OPEN] if open_only else []
        async with self._db.transaction() as session:
            total = await session.scalar(
                select(func.count()).select_from(Dispute).where(*conditions)
            )
            rows = await session.scalars(
                select(Dispute)
                .where(*conditions)
                .order_by(Dispute.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            return Page(list(rows), page, page_size, int(total or 0))

    # ------------------------------------------------------------------ queries

    async def _row(self, session: AsyncSession, award_id: int) -> AwardRow:
        row = (await session.execute(award_rows_query().where(TipAward.id == award_id))).first()
        if row is None:
            raise NotFound(f"Award #{award_id} doesn't exist.")
        return to_row(*row)

    async def get(self, actor: Actor, award_id: int) -> AwardRow:
        async with self._db.transaction() as session:
            row = await self._row(session, award_id)
        if row.discord_user_id != actor.discord_id:
            require(actor, Permission.TIP_HISTORY_ANY)
        return row

    async def list_awards(
        self,
        actor: Actor,
        *,
        status: AwardStatus | None = None,
        event_id: int | None = None,
        discord_user_id: int | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> Page[AwardRow]:
        if discord_user_id is None or discord_user_id != actor.discord_id:
            require(
                actor,
                Permission.AWARD_VIEW_QUEUE
                if discord_user_id is None
                else Permission.TIP_HISTORY_ANY,
            )
        page = max(1, page)
        conditions = []
        if status is not None:
            conditions.append(TipAward.status == status)
        if event_id is not None:
            conditions.append(TipAward.event_id == event_id)
        if discord_user_id is not None:
            conditions.append(DiscordAccount.discord_user_id == discord_user_id)
        async with self._db.transaction() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(TipAward)
                .join(DiscordAccount, DiscordAccount.user_id == TipAward.user_id)
                .where(*conditions)
            )
            rows = await session.execute(
                award_rows_query()
                .where(*conditions)
                .order_by(TipAward.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            return Page([to_row(*row) for row in rows], page, page_size, int(total or 0))

    async def ledger_summary(self, actor: Actor) -> LedgerSummary:
        require(actor, Permission.AWARD_VIEW_QUEUE)
        async with self._db.transaction() as session:
            totals = await _status_totals(session)
            open_batches = await session.scalar(
                select(func.count())
                .select_from(PaymentBatch)
                .where(PaymentBatch.status == BatchStatus.OPEN)
            )
            open_disputes = await session.scalar(
                select(func.count())
                .select_from(Dispute)
                .where(Dispute.status == DisputeStatus.OPEN)
            )
            return LedgerSummary(totals, int(open_batches or 0), int(open_disputes or 0))

    async def profile(self, discord_user_id: int) -> ProfileSummary:
        async with self._db.transaction() as session:
            user_id = await find_user_id(session, discord_user_id)
            if user_id is None:
                empty = {status: StatusTotal(0, 0) for status in AwardStatus}
                return ProfileSummary(discord_user_id, None, None, empty, 0, 0, 0, 0)
            wallet = await active_wallet(session, user_id)
            totals = await _status_totals(session, TipAward.user_id == user_id)
            paid_rows = await session.execute(
                select(
                    TipAward.verification_source,
                    func.coalesce(func.sum(TipAward.amount_lamports), 0),
                )
                .where(TipAward.user_id == user_id, TipAward.status == AwardStatus.PAID)
                .group_by(TipAward.verification_source)
            )
            paid_manual = paid_other = 0
            for source, lamports in paid_rows:
                if source == VerificationSource.MANUAL:
                    paid_manual += int(lamports)
                else:
                    paid_other += int(lamports)
            rollcalls = await session.scalar(
                select(func.count()).select_from(EventEntry).where(EventEntry.user_id == user_id)
            )
            earned = sum(
                total.lamports for status, total in totals.items() if status != AwardStatus.VOIDED
            )
            return ProfileSummary(
                discord_user_id=discord_user_id,
                wallet=wallet.address if wallet else None,
                wallet_registered_at=wallet.registered_at if wallet else None,
                by_status=totals,
                earned_lamports=earned,
                paid_manual_lamports=paid_manual,
                paid_other_lamports=paid_other,
                rollcall_count=int(rollcalls or 0),
            )


async def maybe_complete_batch(session: AsyncSession, batch_id: int, now: datetime) -> bool:
    """Mark an open batch completed once none of its awards still await settlement."""
    batch = await session.get(PaymentBatch, batch_id)
    if batch is None or batch.status != BatchStatus.OPEN:
        return False
    outstanding = await session.scalar(
        select(func.count())
        .select_from(TipAward)
        .where(
            TipAward.batch_id == batch_id,
            TipAward.status.in_(
                [AwardStatus.QUEUED, AwardStatus.PAYMENT_SUBMITTED, AwardStatus.DISPUTED]
            ),
        )
    )
    if outstanding:
        return False
    batch.status = BatchStatus.COMPLETED
    batch.completed_at = now
    record_audit(
        session,
        actor_id=None,
        action=AuditAction.BATCH_COMPLETED,
        entity_type="batch",
        entity_id=batch_id,
        before={"status": BatchStatus.OPEN},
        after={"status": BatchStatus.COMPLETED},
    )
    return True
