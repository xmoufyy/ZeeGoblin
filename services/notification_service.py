"""Member DM preferences and rate-limited wallet reminders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from config.permissions import Actor, Permission, require
from database.models import AwardStatus, DiscordAccount, TipAward, User, Wallet
from database.session import Database
from services.accounts import DiscordIdentity, ensure_user
from services.audit_service import AuditAction, record_audit
from utils.timeutil import Clock, utcnow

REMINDER_COOLDOWN = timedelta(hours=24)


@dataclass(frozen=True)
class MissingWallet:
    discord_user_id: int
    username: str
    award_count: int
    lamports: int
    last_reminded_at: datetime | None
    dm_opt_out: bool


class NotificationService:
    def __init__(self, db: Database, clock: Clock = utcnow) -> None:
        self._db = db
        self._clock = clock

    async def set_dm_opt_out(self, identity: DiscordIdentity, opt_out: bool) -> None:
        async with self._db.transaction() as session:
            user = await ensure_user(session, identity)
            if user.dm_opt_out == opt_out:
                return
            user.dm_opt_out = opt_out
            record_audit(
                session,
                actor_id=identity.discord_id,
                action=AuditAction.MEMBER_PREFERENCE_CHANGED,
                entity_type="user",
                entity_id=user.id,
                after={"dm_opt_out": opt_out},
            )

    async def dm_opt_out(self, discord_user_id: int) -> bool:
        async with self._db.transaction() as session:
            return bool(
                await session.scalar(
                    select(User.dm_opt_out)
                    .join(DiscordAccount, DiscordAccount.user_id == User.id)
                    .where(DiscordAccount.discord_user_id == discord_user_id)
                )
            )

    async def opted_out(self, discord_user_ids: list[int]) -> set[int]:
        if not discord_user_ids:
            return set()
        async with self._db.transaction() as session:
            return set(
                await session.scalars(
                    select(DiscordAccount.discord_user_id)
                    .join(User, User.id == DiscordAccount.user_id)
                    .where(
                        DiscordAccount.discord_user_id.in_(discord_user_ids),
                        User.dm_opt_out.is_(True),
                    )
                )
            )

    async def claim_wallet_reminders(
        self, discord_user_ids: list[int] | None = None
    ) -> tuple[list[int], int]:
        """Pick members to remind about a missing wallet and stamp them, atomically.

        Only members who still have no wallet, have an award waiting on one, allow DMs and
        weren't reminded in the last 24 hours. ``None`` means everyone who qualifies.
        Returns (discord IDs to DM, number skipped for cooldown/opt-out).
        """
        now = self._clock()
        async with self._db.transaction() as session:
            has_wallet = (
                select(Wallet.id)
                .where(Wallet.user_id == User.id, Wallet.is_active.is_(True))
                .exists()
            )
            waiting = (
                select(TipAward.id)
                .where(TipAward.user_id == User.id, TipAward.status == AwardStatus.AWAITING_WALLET)
                .exists()
            )
            query = (
                select(User, DiscordAccount.discord_user_id)
                .join(DiscordAccount, DiscordAccount.user_id == User.id)
                .where(waiting, ~has_wallet)
            )
            if discord_user_ids is not None:
                query = query.where(DiscordAccount.discord_user_id.in_(discord_user_ids))
            chosen, skipped = [], 0
            for user, discord_id in await session.execute(query):
                recent = (
                    user.last_wallet_reminder_at is not None
                    and user.last_wallet_reminder_at > now - REMINDER_COOLDOWN
                )
                if user.dm_opt_out or recent:
                    skipped += 1
                    continue
                user.last_wallet_reminder_at = now
                chosen.append(discord_id)
            return chosen, skipped

    async def missing_wallets(self, actor: Actor) -> list[MissingWallet]:
        """Members with awards stuck on 'awaiting wallet', biggest amounts first."""
        require(actor, Permission.AWARD_VIEW_QUEUE)
        async with self._db.transaction() as session:
            rows = await session.execute(
                select(
                    DiscordAccount.discord_user_id,
                    DiscordAccount.username,
                    func.count(TipAward.id),
                    func.sum(TipAward.amount_lamports),
                    User.last_wallet_reminder_at,
                    User.dm_opt_out,
                )
                .join(User, User.id == TipAward.user_id)
                .join(DiscordAccount, DiscordAccount.user_id == User.id)
                .where(TipAward.status == AwardStatus.AWAITING_WALLET)
                .group_by(User.id)
                .order_by(func.sum(TipAward.amount_lamports).desc())
            )
            return [
                MissingWallet(d, u, int(c), int(s or 0), last, bool(out))
                for d, u, c, s, last, out in rows
            ]
