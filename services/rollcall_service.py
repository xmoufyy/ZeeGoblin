"""Roll calls: start, check in, close, and finalize into awards.

SQLite is authoritative. Deadlines are checked against the stored ``expires_at`` on every
check-in; finalization is a single transaction and is idempotent, so a crash, a restart, or
two schedulers racing can never lose entries or create duplicate awards.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select

from config.permissions import Actor, Permission, require
from config.settings import Limits
from database.models import (
    AwardSource,
    AwardStatus,
    Event,
    EventEntry,
    EventKind,
    EventStatus,
    TipAward,
)
from database.session import Database
from services.accounts import (
    DiscordIdentity,
    active_wallet,
    active_wallets,
    discord_ids_for,
    ensure_user,
    find_user_id,
)
from services.audit_service import AuditAction, Page, record_audit
from services.errors import Conflict, InputError, NotFound
from utils.currency import format_sol
from utils.timeutil import Clock, utcnow

log = logging.getLogger(__name__)


class CheckInOutcome(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_CHECKED_IN = "already_checked_in"
    CLOSED = "closed"
    FULL = "full"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class CheckInResult:
    outcome: CheckInOutcome
    event_id: int
    entry_count: int = 0
    has_wallet: bool = False
    reward_lamports: int = 0


@dataclass(frozen=True)
class FinalizeResult:
    event: Event
    already_finalized: bool
    created_awards: int = 0
    pending_review: int = 0
    awaiting_wallet: int = 0
    total_lamports: int = 0
    # Wallet -> Discord IDs of participants sharing it (possible alt accounts; flag only).
    shared_wallets: dict[str, list[int]] = field(default_factory=dict)
    # Discord IDs of participants whose award waits for a wallet (for reminder DMs).
    awaiting_wallet_discord_ids: list[int] = field(default_factory=list)


class RollCallService:
    def __init__(self, db: Database, limits: Limits, clock: Clock = utcnow) -> None:
        self._db = db
        self._limits = limits
        self._clock = clock

    # ------------------------------------------------------------------ start / attach

    async def start(
        self,
        actor: Actor,
        *,
        guild_id: int,
        channel_id: int,
        duration_minutes: int,
        reward_lamports: int,
        max_participants: int | None = None,
        budget_lamports: int | None = None,
    ) -> Event:
        require(actor, Permission.ROLLCALL_START)
        limits = self._limits
        if not 1 <= duration_minutes <= limits.max_rollcall_duration_minutes:
            raise InputError(
                f"Duration must be between 1 and {limits.max_rollcall_duration_minutes} minutes."
            )
        if reward_lamports <= 0:
            raise InputError("Reward must be greater than zero.")
        if reward_lamports > limits.max_rollcall_reward:
            raise InputError(
                f"Reward can't exceed {format_sol(limits.max_rollcall_reward)} SOL per person."
            )
        if max_participants is not None and not (
            1 <= max_participants <= limits.max_rollcall_participants
        ):
            raise InputError(
                f"Max participants must be between 1 and {limits.max_rollcall_participants}."
            )
        budget = limits.max_rollcall_budget if budget_lamports is None else budget_lamports
        if budget > limits.max_rollcall_budget:
            raise InputError(
                f"Budget can't exceed {format_sol(limits.max_rollcall_budget)} SOL per roll call."
            )
        if budget < reward_lamports:
            raise InputError("Budget must cover at least one reward.")

        # A roll call whose deadline just passed may not be finalized yet (the scheduler runs
        # every few seconds). Close it now rather than refusing the new one.
        await self.finalize_due()
        now = self._clock()
        async with self._db.transaction() as session:
            existing = await session.scalar(
                select(Event.id).where(
                    Event.guild_id == guild_id, Event.status == EventStatus.ACTIVE
                )
            )
            if existing is not None:
                raise Conflict(
                    f"Roll call #{existing} is still running. End it before starting another."
                )
            event = Event(
                kind=EventKind.ROLLCALL,
                status=EventStatus.ACTIVE,
                guild_id=guild_id,
                channel_id=channel_id,
                created_by_discord_id=actor.discord_id,
                reward_lamports=reward_lamports,
                max_participants=max_participants,
                budget_lamports=budget,
                started_at=now,
                expires_at=now + timedelta(minutes=duration_minutes),
            )
            session.add(event)
            await session.flush()
            record_audit(
                session,
                actor_id=actor.discord_id,
                action=AuditAction.ROLLCALL_CREATED,
                entity_type="event",
                entity_id=event.id,
                after={
                    "reward_lamports": reward_lamports,
                    "budget_lamports": budget,
                    "max_participants": max_participants,
                    "duration_minutes": duration_minutes,
                    "expires_at": event.expires_at,
                    "channel_id": channel_id,
                },
            )
            log.info(
                "Roll call #%s started by %s: %s SOL each, cap %s, expires %s",
                event.id,
                actor.discord_id,
                format_sol(reward_lamports),
                event.participant_cap,
                event.expires_at.isoformat(),
            )
            return event

    async def attach_message(self, event_id: int, message_id: int) -> None:
        async with self._db.transaction() as session:
            event = await session.get(Event, event_id)
            if event is None:
                raise NotFound(f"Roll call #{event_id} not found.")
            event.message_id = message_id

    async def abandon_unannounced(self, event_id: int, actor_id: int, detail: str) -> None:
        """Cancel a roll call whose Discord announcement could not be posted."""
        async with self._db.transaction() as session:
            event = await session.get(Event, event_id)
            if event is None or event.status != EventStatus.ACTIVE or event.entry_count:
                return
            event.status = EventStatus.CANCELLED
            event.closed_at = self._clock()
            event.close_reason = "announcement_failed"
            event.announcement_synced = True
            event.staff_notified = True
            record_audit(
                session,
                actor_id=actor_id,
                action=AuditAction.ROLLCALL_CANCELLED,
                entity_type="event",
                entity_id=event.id,
                after={"status": EventStatus.CANCELLED, "close_reason": "announcement_failed"},
                reason=detail[:300],
            )

    # ------------------------------------------------------------------ check-in

    async def check_in(
        self, event_id: int, identity: DiscordIdentity, *, source: str = "button"
    ) -> CheckInResult:
        now = self._clock()
        async with self._db.transaction() as session:
            event = await session.get(Event, event_id)
            if event is None or event.kind != EventKind.ROLLCALL:
                return CheckInResult(CheckInOutcome.NOT_FOUND, event_id)
            if event.status != EventStatus.ACTIVE or now >= event.expires_at:
                return CheckInResult(CheckInOutcome.CLOSED, event_id, event.entry_count)

            user = await ensure_user(session, identity)
            already = await session.scalar(
                select(EventEntry.id).where(
                    EventEntry.event_id == event_id, EventEntry.user_id == user.id
                )
            )
            if already is not None:
                return CheckInResult(CheckInOutcome.ALREADY_CHECKED_IN, event_id, event.entry_count)
            if event.entry_count >= event.participant_cap:
                return CheckInResult(CheckInOutcome.FULL, event_id, event.entry_count)

            session.add(
                EventEntry(event_id=event_id, user_id=user.id, source=source, entered_at=now)
            )
            event.entry_count += 1
            await session.flush()
            has_wallet = await active_wallet(session, user.id) is not None
            return CheckInResult(
                CheckInOutcome.ACCEPTED,
                event_id,
                event.entry_count,
                has_wallet,
                event.reward_lamports,
            )

    # ------------------------------------------------------------------ finalize

    async def end(self, actor: Actor, event_id: int, *, award: bool = True) -> FinalizeResult:
        """Close a roll call early. ``award=False`` cancels it without creating awards."""
        require(actor, Permission.ROLLCALL_END)
        return await self._finalize(event_id, actor_id=actor.discord_id, award=award, force=True)

    async def finalize_expired(self, event_id: int) -> FinalizeResult:
        return await self._finalize(event_id, actor_id=None, award=True, force=False)

    async def finalize_due(self) -> list[FinalizeResult]:
        """Finalize every active roll call whose deadline has passed (scheduler + recovery)."""
        now = self._clock()
        async with self._db.transaction() as session:
            due = list(
                await session.scalars(
                    select(Event.id)
                    .where(Event.status == EventStatus.ACTIVE, Event.expires_at <= now)
                    .order_by(Event.id)
                )
            )
        results = []
        for event_id in due:
            try:
                result = await self.finalize_expired(event_id)
            except Exception:
                log.exception("Failed to finalize roll call #%s; will retry", event_id)
                continue
            if not result.already_finalized:
                results.append(result)
        return results

    async def _finalize(
        self, event_id: int, *, actor_id: int | None, award: bool, force: bool
    ) -> FinalizeResult:
        now = self._clock()
        async with self._db.transaction() as session:
            event = await session.get(Event, event_id)
            if event is None:
                raise NotFound(f"Roll call #{event_id} not found.")
            if event.status != EventStatus.ACTIVE:
                return FinalizeResult(event, already_finalized=True)
            if not force and now < event.expires_at:
                raise Conflict(f"Roll call #{event_id} has not expired yet.")

            entries = list(
                await session.scalars(
                    select(EventEntry)
                    .where(EventEntry.event_id == event_id)
                    .order_by(EventEntry.id)
                )
            )
            wallets = await active_wallets(session, [entry.user_id for entry in entries])
            already_awarded = set(
                await session.scalars(
                    select(TipAward.event_entry_id).where(TipAward.event_id == event_id)
                )
            )

            pending = awaiting = total = 0
            awaiting_user_ids: list[int] = []
            if award:
                for entry in entries:
                    if entry.id in already_awarded:
                        continue
                    has_wallet = entry.user_id in wallets
                    status = (
                        AwardStatus.PENDING_REVIEW if has_wallet else AwardStatus.AWAITING_WALLET
                    )
                    tip = TipAward(
                        user_id=entry.user_id,
                        source=AwardSource.ROLLCALL,
                        event_id=event_id,
                        event_entry_id=entry.id,
                        reason=f"Roll call #{event_id}",
                        amount_lamports=event.reward_lamports,
                        status=status,
                        created_by_discord_id=None,
                        created_at=now,
                    )
                    session.add(tip)
                    await session.flush()
                    record_audit(
                        session,
                        actor_id=None,
                        action=AuditAction.AWARD_CREATED,
                        entity_type="award",
                        entity_id=tip.id,
                        after={
                            "status": status,
                            "user_id": entry.user_id,
                            "amount_lamports": tip.amount_lamports,
                            "event_id": event_id,
                            "source": AwardSource.ROLLCALL,
                        },
                    )
                    total += tip.amount_lamports
                    if has_wallet:
                        pending += 1
                    else:
                        awaiting += 1
                        awaiting_user_ids.append(entry.user_id)

            by_wallet: dict[str, list[int]] = defaultdict(list)
            for entry in entries:
                if entry.user_id in wallets:
                    by_wallet[wallets[entry.user_id]].append(entry.user_id)
            shared_users = {w: users for w, users in by_wallet.items() if len(users) > 1}
            discord_ids = await discord_ids_for(
                session,
                [u for users in shared_users.values() for u in users] + awaiting_user_ids,
            )
            shared = {w: [discord_ids[u][0] for u in users] for w, users in shared_users.items()}

            event.status = EventStatus.CLOSED if award else EventStatus.CANCELLED
            event.closed_at = now
            event.closed_by_discord_id = actor_id
            event.close_reason = (
                "expired" if actor_id is None else ("ended_early" if award else "cancelled")
            )
            event.award_count = pending + awaiting
            event.awaiting_wallet_count = awaiting
            event.awarded_lamports = total
            record_audit(
                session,
                actor_id=actor_id,
                action=AuditAction.ROLLCALL_CLOSED if award else AuditAction.ROLLCALL_CANCELLED,
                entity_type="event",
                entity_id=event_id,
                before={"status": EventStatus.ACTIVE},
                after={
                    "status": event.status,
                    "close_reason": event.close_reason,
                    "entries": len(entries),
                    "awards": pending + awaiting,
                    "awaiting_wallet": awaiting,
                    "total_lamports": total,
                    "shared_wallet_count": len(shared),
                },
            )
            log.info(
                "Roll call #%s %s: %s entries, %s awards (%s awaiting wallet), %s SOL",
                event_id,
                event.status,
                len(entries),
                pending + awaiting,
                awaiting,
                format_sol(total),
            )
            return FinalizeResult(
                event,
                already_finalized=False,
                created_awards=pending + awaiting,
                pending_review=pending,
                awaiting_wallet=awaiting,
                total_lamports=total,
                shared_wallets=shared,
                awaiting_wallet_discord_ids=[discord_ids[u][0] for u in awaiting_user_ids],
            )

    # ------------------------------------------------------------------ discord sync flags

    async def mark_synced(
        self, event_id: int, *, announcement: bool | None = None, staff: bool | None = None
    ) -> None:
        async with self._db.transaction() as session:
            event = await session.get(Event, event_id)
            if event is None:
                return
            if announcement is not None:
                event.announcement_synced = announcement
            if staff is not None:
                event.staff_notified = staff

    async def unsynced_closed_events(self, max_age: timedelta = timedelta(days=3)) -> list[Event]:
        cutoff = self._clock() - max_age
        async with self._db.transaction() as session:
            rows = await session.scalars(
                select(Event)
                .where(
                    Event.status != EventStatus.ACTIVE,
                    (Event.announcement_synced.is_(False)) | (Event.staff_notified.is_(False)),
                    Event.closed_at >= cutoff,
                )
                .order_by(Event.id)
            )
            return list(rows)

    # ------------------------------------------------------------------ queries

    async def get(self, event_id: int) -> Event | None:
        async with self._db.transaction() as session:
            return await session.get(Event, event_id)

    async def active_events(self, guild_id: int | None = None) -> list[Event]:
        async with self._db.transaction() as session:
            query = select(Event).where(Event.status == EventStatus.ACTIVE)
            if guild_id is not None:
                query = query.where(Event.guild_id == guild_id)
            return list(await session.scalars(query.order_by(Event.id)))

    async def history(self, actor: Actor, *, page: int = 1, page_size: int = 10) -> Page[Event]:
        require(actor, Permission.ROLLCALL_VIEW)
        page = max(1, page)
        async with self._db.transaction() as session:
            total = await session.scalar(select(func.count()).select_from(Event))
            rows = await session.scalars(
                select(Event)
                .order_by(Event.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            return Page(list(rows), page, page_size, int(total or 0))

    async def participation_count(self, discord_user_id: int) -> int:
        async with self._db.transaction() as session:
            user_id = await find_user_id(session, discord_user_id)
            if user_id is None:
                return 0
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(EventEntry)
                    .where(EventEntry.user_id == user_id)
                )
                or 0
            )

    @staticmethod
    def seconds_left(event: Event, now: datetime) -> int:
        return max(0, int((event.expires_at - now).total_seconds()))
