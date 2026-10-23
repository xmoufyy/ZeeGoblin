"""Payment batches and manual payment tracking.

ZeeGoblin never sends SOL. A payment moderator exports a batch, sends the transfers in their
own wallet, then records each transaction signature here and confirms it. "Manually
confirmed" is never presented as blockchain-verified.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.permissions import Actor, Permission, require
from config.settings import Limits
from database.models import (
    AwardStatus,
    BatchStatus,
    DiscordAccount,
    Dispute,
    DisputeStatus,
    Payment,
    PaymentAward,
    PaymentBatch,
    PaymentStatus,
    TipAward,
    VerificationSource,
)
from database.session import Database
from services.audit_service import AuditAction, Page, record_audit
from services.award_states import label, transition
from services.errors import Conflict, InputError, NotFound, PermissionDenied
from services.tip_service import maybe_complete_batch
from utils.csv_export import build_csv
from utils.currency import format_sol
from utils.timeutil import Clock, utcnow
from utils.validation import (
    ValidationError,
    clean_text,
    validate_solana_address,
    validate_transaction_signature,
)

# Mods pay from these files, so they hold only what's needed to send SOL. Award and batch IDs
# stay in Discord (/batch view) and are matched automatically by /payment record.
BATCH_CSV_HEADER = ("discord_username", "wallet", "amount_sol", "reason")
SUMMARY_CSV_HEADER = BATCH_CSV_HEADER


@dataclass(frozen=True)
class BatchLine:
    award_id: int
    status: str
    discord_user_id: int
    username: str
    wallet: str
    amount_lamports: int
    reason: str
    event_id: int | None
    approved_by_discord_id: int
    approved_at: datetime


@dataclass(frozen=True)
class WalletTotal:
    wallet: str
    lamports: int
    award_ids: list[int]
    discord_user_ids: list[int]
    usernames: list[str]


@dataclass(frozen=True)
class BatchDetail:
    batch: PaymentBatch
    lines: list[BatchLine]
    wallets: list[WalletTotal]
    payments: list[Payment]

    @property
    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for line in self.lines:
            counts[line.status] += 1
        return dict(counts)

    @property
    def progress(self) -> BatchProgress:
        done_wallets = {
            wallet.wallet
            for wallet in self.wallets
            if all(
                line.status in SETTLED_STATUSES
                for line in self.lines
                if line.wallet == wallet.wallet
            )
        }
        return BatchProgress(
            paid_awards=sum(1 for line in self.lines if line.status == AwardStatus.PAID),
            total_awards=len(self.lines),
            remaining_lamports=sum(
                line.amount_lamports for line in self.lines if line.status in OUTSTANDING_STATUSES
            ),
            wallets_done=done_wallets,
        )


# Awards in a batch that still need money to move (or a dispute resolved).
OUTSTANDING_STATUSES = (AwardStatus.QUEUED, AwardStatus.PAYMENT_SUBMITTED, AwardStatus.DISPUTED)
SETTLED_STATUSES = (AwardStatus.PAID, AwardStatus.VOIDED)


@dataclass(frozen=True)
class BatchProgress:
    paid_awards: int
    total_awards: int
    remaining_lamports: int
    wallets_done: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class BatchCancellation:
    batch: PaymentBatch
    released: int
    retained: int


@dataclass(frozen=True)
class PaymentRecord:
    payment: Payment
    award_ids: list[int]
    flagged: bool
    mismatches: list[str]


@dataclass(frozen=True)
class PaymentRow:
    payment: Payment
    award_ids: list[int]


def _wallet_totals(lines: list[BatchLine]) -> list[WalletTotal]:
    grouped: dict[str, list[BatchLine]] = defaultdict(list)
    for line in lines:
        grouped[line.wallet].append(line)
    totals = []
    for wallet, items in sorted(grouped.items()):
        users: dict[int, str] = {}
        for item in items:
            users.setdefault(item.discord_user_id, item.username)
        totals.append(
            WalletTotal(
                wallet=wallet,
                lamports=sum(item.amount_lamports for item in items),
                award_ids=[item.award_id for item in items],
                discord_user_ids=list(users),
                usernames=list(users.values()),
            )
        )
    return totals


def batch_csv(detail: BatchDetail) -> bytes:
    """One row per award, grouped by wallet."""
    return build_csv(
        BATCH_CSV_HEADER,
        (
            (line.username, line.wallet, format_sol(line.amount_lamports), line.reason)
            for line in sorted(detail.lines, key=lambda item: (item.wallet, item.award_id))
        ),
    )


def summary_csv(detail: BatchDetail) -> bytes:
    """One row per wallet with the total to send - what the payer actually pays from."""
    reasons: dict[str, list[str]] = defaultdict(list)
    for line in sorted(detail.lines, key=lambda item: item.award_id):
        if line.reason not in reasons[line.wallet]:
            reasons[line.wallet].append(line.reason)
    return build_csv(
        SUMMARY_CSV_HEADER,
        (
            (
                ", ".join(total.usernames),
                total.wallet,
                format_sol(total.lamports),
                "; ".join(reasons[total.wallet]),
            )
            for total in detail.wallets
        ),
    )


def _validated(func_, value: str) -> str:  # type: ignore[no-untyped-def]
    try:
        return func_(value)
    except ValidationError as exc:
        raise InputError(str(exc)) from exc


class PaymentService:
    def __init__(self, db: Database, limits: Limits, clock: Clock = utcnow) -> None:
        self._db = db
        self._limits = limits
        self._clock = clock

    # ------------------------------------------------------------------ batches

    async def create_batch(self, actor: Actor, *, max_awards: int | None = None) -> BatchDetail:
        """Atomically reserve every approved, unbatched award into a new batch."""
        require(actor, Permission.BATCH_EXPORT)
        if max_awards is not None and max_awards < 1:
            raise InputError("max_awards must be at least 1.")
        now = self._clock()
        async with self._db.transaction() as session:
            query = (
                select(TipAward)
                .where(
                    TipAward.status == AwardStatus.APPROVED,
                    TipAward.batch_id.is_(None),
                    TipAward.payout_wallet.is_not(None),
                )
                .order_by(TipAward.id)
            )
            if max_awards is not None:
                query = query.limit(max_awards)
            awards = list(await session.scalars(query))
            if not awards:
                raise NotFound("No approved awards are waiting for payment. 🧌 The vault is quiet.")

            batch = PaymentBatch(
                status=BatchStatus.OPEN,
                created_by_discord_id=actor.discord_id,
                award_count=len(awards),
                total_lamports=sum(award.amount_lamports for award in awards),
                created_at=now,
            )
            session.add(batch)
            await session.flush()
            for award in awards:
                transition(
                    session,
                    award,
                    AwardStatus.QUEUED,
                    actor_id=actor.discord_id,
                    action=AuditAction.AWARD_QUEUED,
                    details={"batch_id": batch.id},
                )
                award.batch_id = batch.id
            record_audit(
                session,
                actor_id=actor.discord_id,
                action=AuditAction.BATCH_CREATED,
                entity_type="batch",
                entity_id=batch.id,
                after={
                    "award_count": batch.award_count,
                    "total_lamports": batch.total_lamports,
                    "award_ids": [award.id for award in awards],
                },
            )
            await session.flush()
            return await self._detail(session, batch)

    async def get_batch(self, actor: Actor, batch_id: int) -> BatchDetail:
        require(actor, Permission.BATCH_VIEW)
        async with self._db.transaction() as session:
            batch = await session.get(PaymentBatch, batch_id)
            if batch is None:
                raise NotFound(f"Batch #{batch_id} doesn't exist.")
            return await self._detail(session, batch)

    async def export_batch(self, actor: Actor, batch_id: int) -> BatchDetail:
        """Batch detail for a CSV download; the download is audited (files contain wallets)."""
        require(actor, Permission.BATCH_EXPORT)
        async with self._db.transaction() as session:
            batch = await session.get(PaymentBatch, batch_id)
            if batch is None:
                raise NotFound(f"Batch #{batch_id} doesn't exist.")
            record_audit(
                session,
                actor_id=actor.discord_id,
                action=AuditAction.BATCH_EXPORTED,
                entity_type="batch",
                entity_id=batch_id,
                after={"status": batch.status},
            )
            return await self._detail(session, batch)

    async def list_batches(
        self,
        actor: Actor,
        *,
        status: BatchStatus | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> Page[PaymentBatch]:
        require(actor, Permission.BATCH_VIEW)
        page = max(1, page)
        conditions = [PaymentBatch.status == status] if status else []
        async with self._db.transaction() as session:
            total = await session.scalar(
                select(func.count()).select_from(PaymentBatch).where(*conditions)
            )
            rows = await session.scalars(
                select(PaymentBatch)
                .where(*conditions)
                .order_by(PaymentBatch.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            return Page(list(rows), page, page_size, int(total or 0))

    async def batch_progress(
        self, actor: Actor, batch_ids: list[int]
    ) -> dict[int, tuple[int, int]]:
        """batch_id -> (paid award count, lamports still outstanding), in one query."""
        require(actor, Permission.BATCH_VIEW)
        if not batch_ids:
            return {}
        async with self._db.transaction() as session:
            rows = await session.execute(
                select(
                    TipAward.batch_id,
                    TipAward.status,
                    func.sum(TipAward.amount_lamports),
                    func.count(),
                )
                .where(TipAward.batch_id.in_(batch_ids))
                .group_by(TipAward.batch_id, TipAward.status)
            )
            progress: dict[int, tuple[int, int]] = {batch_id: (0, 0) for batch_id in batch_ids}
            for batch_id, status, lamports, count in rows:
                paid, outstanding = progress[batch_id]
                if status == AwardStatus.PAID:
                    paid += int(count)
                elif status in OUTSTANDING_STATUSES:
                    outstanding += int(lamports or 0)
                progress[batch_id] = (paid, outstanding)
            return progress

    async def cancel_batch(self, actor: Actor, batch_id: int, reason: str) -> BatchCancellation:
        require(actor, Permission.BATCH_CANCEL)
        reason = _validated(lambda v: clean_text(v, field="Reason"), reason)
        now = self._clock()
        async with self._db.transaction() as session:
            batch = await session.get(PaymentBatch, batch_id)
            if batch is None:
                raise NotFound(f"Batch #{batch_id} doesn't exist.")
            if batch.status != BatchStatus.OPEN:
                raise Conflict(f"Batch #{batch_id} is already {batch.status}.")
            awards = list(
                await session.scalars(select(TipAward).where(TipAward.batch_id == batch_id))
            )
            released = retained = 0
            for award in awards:
                if award.status == AwardStatus.QUEUED:
                    transition(
                        session,
                        award,
                        AwardStatus.APPROVED,
                        actor_id=actor.discord_id,
                        action=AuditAction.AWARD_RELEASED,
                        reason=reason,
                        details={"released_from_batch": batch_id},
                    )
                    award.batch_id = None
                    released += 1
                else:
                    retained += 1  # payment already submitted/paid: never released
            batch.status = BatchStatus.CANCELLED
            batch.cancelled_by_discord_id = actor.discord_id
            batch.cancelled_at = now
            batch.cancel_reason = reason
            record_audit(
                session,
                actor_id=actor.discord_id,
                action=AuditAction.BATCH_CANCELLED,
                entity_type="batch",
                entity_id=batch_id,
                before={"status": BatchStatus.OPEN},
                after={"status": BatchStatus.CANCELLED, "released": released, "retained": retained},
                reason=reason,
            )
            return BatchCancellation(batch, released, retained)

    async def _detail(self, session: AsyncSession, batch: PaymentBatch) -> BatchDetail:
        rows = await session.execute(
            select(TipAward, DiscordAccount.discord_user_id, DiscordAccount.username)
            .join(DiscordAccount, DiscordAccount.user_id == TipAward.user_id)
            .where(TipAward.batch_id == batch.id)
            .order_by(TipAward.id)
        )
        lines = [
            BatchLine(
                award_id=award.id,
                status=award.status,
                discord_user_id=discord_id,
                username=username,
                wallet=award.payout_wallet or "",
                amount_lamports=award.amount_lamports,
                reason=award.reason,
                event_id=award.event_id,
                approved_by_discord_id=award.approved_by_discord_id or 0,
                approved_at=award.approved_at or award.created_at,
            )
            for award, discord_id, username in rows
        ]
        payments = list(
            await session.scalars(
                select(Payment).where(Payment.batch_id == batch.id).order_by(Payment.id)
            )
        )
        return BatchDetail(batch, lines, _wallet_totals(lines), payments)

    # ------------------------------------------------------------------ payments

    async def record_payment(
        self,
        actor: Actor,
        *,
        batch_id: int,
        wallet: str,
        amount_lamports: int,
        signature: str,
        award_ids: list[int] | None = None,
        note: str | None = None,
        flag_mismatch: bool = False,
    ) -> PaymentRecord:
        """Record a transfer the moderator already sent outside ZeeGoblin.

        Exact matches become ``payment_submitted``. A mismatch (wrong wallet or amount) is
        refused unless ``flag_mismatch`` is set, in which case the payment is stored as
        ``flagged`` and every included award is moved to ``disputed`` for review.
        """
        require(actor, Permission.PAYMENT_RECORD)
        wallet = _validated(validate_solana_address, wallet)
        signature = _validated(validate_transaction_signature, signature)
        note = _validated(lambda v: clean_text(v, field="Note", required=False), note or "")
        if amount_lamports <= 0:
            raise InputError("Amount must be greater than zero.")
        now = self._clock()

        async with self._db.transaction() as session:
            batch = await session.get(PaymentBatch, batch_id)
            if batch is None:
                raise NotFound(f"Batch #{batch_id} doesn't exist.")
            if batch.status != BatchStatus.OPEN:
                raise Conflict(f"Batch #{batch_id} is {batch.status}; payments can't be added.")
            existing = await session.scalar(
                select(Payment.id).where(Payment.signature == signature)
            )
            if existing is not None:
                raise Conflict(f"That signature is already recorded as payment #{existing}.")

            if award_ids:
                awards = list(
                    await session.scalars(
                        select(TipAward).where(TipAward.id.in_(award_ids)).order_by(TipAward.id)
                    )
                )
                missing = sorted(set(award_ids) - {award.id for award in awards})
                if missing:
                    raise NotFound(f"Award(s) {', '.join(f'#{i}' for i in missing)} don't exist.")
            else:
                awards = list(
                    await session.scalars(
                        select(TipAward)
                        .where(
                            TipAward.batch_id == batch_id,
                            TipAward.status == AwardStatus.QUEUED,
                            TipAward.payout_wallet == wallet,
                        )
                        .order_by(TipAward.id)
                    )
                )
                if not awards:
                    raise NotFound(
                        f"Batch #{batch_id} has no unpaid awards for that wallet. "
                        "Pass award_ids explicitly if the wallet was entered differently."
                    )

            problems = []
            for award in awards:
                if award.batch_id != batch_id:
                    problems.append(f"#{award.id} is not in batch #{batch_id}")
                elif award.status != AwardStatus.QUEUED:
                    problems.append(f"#{award.id} is {label(award.status)}")
            if problems:
                raise Conflict("These awards can't be paid in this batch: " + "; ".join(problems))

            mismatches = []
            wrong_wallet = [award.id for award in awards if award.payout_wallet != wallet]
            if wrong_wallet:
                mismatches.append(
                    "approved payout wallet differs for award(s) "
                    + ", ".join(f"#{i}" for i in wrong_wallet)
                )
            expected = sum(award.amount_lamports for award in awards)
            if expected != amount_lamports:
                mismatches.append(
                    f"amount {format_sol(amount_lamports)} SOL != owed {format_sol(expected)} SOL"
                )
            if mismatches and not flag_mismatch:
                raise InputError(
                    "Payment doesn't match the approved awards: "
                    + "; ".join(mismatches)
                    + ". Nothing was recorded. Double-check the transaction, or re-run with "
                    "`flag_mismatch: True` to record it for head-moderator review."
                )

            payment = Payment(
                batch_id=batch_id,
                signature=signature,
                recipient_wallet=wallet,
                amount_lamports=amount_lamports,
                status=PaymentStatus.FLAGGED if mismatches else PaymentStatus.SUBMITTED,
                recorded_by_discord_id=actor.discord_id,
                recorded_at=now,
                note=note or None,
                mismatch_details="; ".join(mismatches) or None,
            )
            session.add(payment)
            await session.flush()
            for award in awards:
                session.add(
                    PaymentAward(
                        payment_id=payment.id,
                        award_id=award.id,
                        amount_lamports=award.amount_lamports,
                    )
                )
                transition(
                    session,
                    award,
                    AwardStatus.PAYMENT_SUBMITTED,
                    actor_id=actor.discord_id,
                    action=AuditAction.AWARD_PAYMENT_SUBMITTED,
                    details={"payment_id": payment.id, "signature": signature},
                )
                if mismatches:
                    reason = "Payment mismatch: " + "; ".join(mismatches)
                    transition(
                        session,
                        award,
                        AwardStatus.DISPUTED,
                        actor_id=actor.discord_id,
                        action=AuditAction.AWARD_FLAGGED,
                        reason=reason,
                        details={"payment_id": payment.id},
                    )
                    session.add(
                        Dispute(
                            award_id=award.id,
                            payment_id=payment.id,
                            status=DisputeStatus.OPEN,
                            previous_status=AwardStatus.PAYMENT_SUBMITTED,
                            reason=reason[:1000],
                            opened_by_discord_id=actor.discord_id,
                            opened_at=now,
                        )
                    )
            record_audit(
                session,
                actor_id=actor.discord_id,
                action=AuditAction.PAYMENT_FLAGGED if mismatches else AuditAction.PAYMENT_SUBMITTED,
                entity_type="payment",
                entity_id=payment.id,
                after={
                    "batch_id": batch_id,
                    "signature": signature,
                    "recipient_wallet": wallet,
                    "amount_lamports": amount_lamports,
                    "award_ids": [award.id for award in awards],
                    "mismatches": mismatches,
                },
                reason=note or None,
            )
            await session.flush()
            return PaymentRecord(
                payment, [award.id for award in awards], bool(mismatches), mismatches
            )

    async def confirm_payment(
        self, actor: Actor, payment_id: int, note: str | None = None
    ) -> PaymentRow:
        """Staff attest the external transfer happened. This is *not* chain verification."""
        require(actor, Permission.PAYMENT_CONFIRM)
        note = _validated(lambda v: clean_text(v, field="Note", required=False), note or "")
        now = self._clock()
        async with self._db.transaction() as session:
            payment = await session.get(Payment, payment_id)
            if payment is None:
                raise NotFound(f"Payment #{payment_id} doesn't exist.")
            if payment.status == PaymentStatus.FLAGGED:
                raise Conflict(
                    f"Payment #{payment_id} was flagged as a mismatch; a head moderator must "
                    "resolve its disputes instead."
                )
            if payment.status != PaymentStatus.SUBMITTED:
                raise Conflict(f"Payment #{payment_id} is already {payment.status}.")
            if (
                self._limits.require_separate_payment_confirmer
                and payment.recorded_by_discord_id == actor.discord_id
                and not actor.is_owner
            ):
                raise PermissionDenied(
                    "Two-person rule: a different staff member must confirm a payment you recorded."
                )

            award_ids = list(
                await session.scalars(
                    select(PaymentAward.award_id).where(PaymentAward.payment_id == payment_id)
                )
            )
            awards = list(await session.scalars(select(TipAward).where(TipAward.id.in_(award_ids))))
            for award in awards:
                if award.status != AwardStatus.PAYMENT_SUBMITTED:
                    raise Conflict(
                        f"Award #{award.id} is {label(award.status)}; resolve that first."
                    )
            payment.status = PaymentStatus.MANUALLY_CONFIRMED
            payment.confirmed_by_discord_id = actor.discord_id
            payment.confirmed_at = now
            if note:
                payment.note = f"{payment.note}\n{note}" if payment.note else note
            for award in awards:
                transition(
                    session,
                    award,
                    AwardStatus.PAID,
                    actor_id=actor.discord_id,
                    action=AuditAction.AWARD_PAID,
                    details={
                        "payment_id": payment_id,
                        "verification_source": VerificationSource.MANUAL,
                    },
                )
                award.paid_at = now
                award.verification_source = VerificationSource.MANUAL
            record_audit(
                session,
                actor_id=actor.discord_id,
                action=AuditAction.PAYMENT_CONFIRMED,
                entity_type="payment",
                entity_id=payment_id,
                before={"status": PaymentStatus.SUBMITTED},
                after={
                    "status": PaymentStatus.MANUALLY_CONFIRMED,
                    "verification_source": VerificationSource.MANUAL,
                    "award_ids": award_ids,
                },
                reason=note or None,
            )
            await maybe_complete_batch(session, payment.batch_id, now)
            return PaymentRow(payment, sorted(award_ids))

    async def get_payment(self, actor: Actor, payment_id: int) -> PaymentRow:
        require(actor, Permission.PAYMENT_VIEW)
        async with self._db.transaction() as session:
            payment = await session.get(Payment, payment_id)
            if payment is None:
                raise NotFound(f"Payment #{payment_id} doesn't exist.")
            award_ids = await session.scalars(
                select(PaymentAward.award_id)
                .where(PaymentAward.payment_id == payment_id)
                .order_by(PaymentAward.award_id)
            )
            return PaymentRow(payment, list(award_ids))

    async def payment_history(
        self,
        actor: Actor,
        *,
        batch_id: int | None = None,
        status: PaymentStatus | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> Page[PaymentRow]:
        require(actor, Permission.PAYMENT_VIEW)
        page = max(1, page)
        conditions = []
        if batch_id is not None:
            conditions.append(Payment.batch_id == batch_id)
        if status is not None:
            conditions.append(Payment.status == status)
        async with self._db.transaction() as session:
            total = await session.scalar(
                select(func.count()).select_from(Payment).where(*conditions)
            )
            payments = list(
                await session.scalars(
                    select(Payment)
                    .where(*conditions)
                    .order_by(Payment.id.desc())
                    .limit(page_size)
                    .offset((page - 1) * page_size)
                )
            )
            links: dict[int, list[int]] = defaultdict(list)
            if payments:
                for payment_id, award_id in await session.execute(
                    select(PaymentAward.payment_id, PaymentAward.award_id)
                    .where(PaymentAward.payment_id.in_([p.id for p in payments]))
                    .order_by(PaymentAward.award_id)
                ):
                    links[payment_id].append(award_id)
            return Page(
                [PaymentRow(p, links[p.id]) for p in payments], page, page_size, int(total or 0)
            )
