"""Wallet registration. Changing a wallet never redirects already-approved awards."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    LOCKED_PAYOUT_STATUSES,
    AwardStatus,
    TipAward,
    Wallet,
    WalletAction,
    WalletHistory,
)
from database.session import Database
from services.accounts import DiscordIdentity, active_wallet, ensure_user, find_user_id
from services.audit_service import AuditAction, record_audit
from services.award_states import transition
from services.errors import Conflict, InputError, NotFound
from utils.timeutil import Clock, utcnow
from utils.validation import ValidationError, validate_solana_address


@dataclass(frozen=True)
class WalletChange:
    address: str
    previous_address: str | None
    awards_now_reviewable: int
    # Approved/queued/paid awards that keep paying the *previous* address (snapshot).
    awards_locked_to_previous: int


@dataclass(frozen=True)
class WalletRemoval:
    address: str
    awards_back_to_awaiting_wallet: int
    awards_locked_to_previous: int


class WalletService:
    def __init__(self, db: Database, clock: Clock = utcnow) -> None:
        self._db = db
        self._clock = clock

    async def set_wallet(self, identity: DiscordIdentity, address: str) -> WalletChange:
        try:
            address = validate_solana_address(address)
        except ValidationError as exc:
            raise InputError(str(exc)) from exc

        now = self._clock()
        async with self._db.transaction() as session:
            user = await ensure_user(session, identity)
            current = await active_wallet(session, user.id)
            if current is not None and current.address == address:
                raise Conflict("That wallet is already registered to your account.")

            previous = current.address if current else None
            if current is not None:
                current.is_active = False
                current.deactivated_at = now
                await session.flush()  # free the one-active-wallet slot before inserting

            wallet = Wallet(user_id=user.id, address=address, is_active=True, registered_at=now)
            session.add(wallet)
            action = WalletAction.CHANGED if previous else WalletAction.REGISTERED
            session.add(
                WalletHistory(
                    user_id=user.id,
                    action=action,
                    old_address=previous,
                    new_address=address,
                    actor_discord_id=identity.discord_id,
                    created_at=now,
                )
            )
            await session.flush()
            record_audit(
                session,
                actor_id=identity.discord_id,
                action=AuditAction.WALLET_CHANGED if previous else AuditAction.WALLET_REGISTERED,
                entity_type="wallet",
                entity_id=wallet.id,
                before={"address": previous} if previous else None,
                after={"address": address, "user_id": user.id},
            )

            waiting = await session.scalars(
                select(TipAward).where(
                    TipAward.user_id == user.id,
                    TipAward.status == AwardStatus.AWAITING_WALLET,
                )
            )
            promoted = 0
            for award in waiting:
                transition(
                    session,
                    award,
                    AwardStatus.PENDING_REVIEW,
                    actor_id=None,
                    action=AuditAction.AWARD_WALLET_RESOLVED,
                    details={"wallet_registered": True},
                )
                promoted += 1

            locked = await self._count_locked(session, user.id, address)
            return WalletChange(address, previous, promoted, locked)

    async def remove_wallet(self, identity: DiscordIdentity) -> WalletRemoval:
        now = self._clock()
        async with self._db.transaction() as session:
            user_id = await find_user_id(session, identity.discord_id)
            current = await active_wallet(session, user_id) if user_id else None
            if user_id is None or current is None:
                raise NotFound("You don't have a wallet registered.")
            current.is_active = False
            current.deactivated_at = now
            session.add(
                WalletHistory(
                    user_id=user_id,
                    action=WalletAction.REMOVED,
                    old_address=current.address,
                    new_address=None,
                    actor_discord_id=identity.discord_id,
                    created_at=now,
                )
            )
            record_audit(
                session,
                actor_id=identity.discord_id,
                action=AuditAction.WALLET_REMOVED,
                entity_type="wallet",
                entity_id=current.id,
                before={"address": current.address},
                after={"active": False},
            )
            reviewable = await session.scalars(
                select(TipAward).where(
                    TipAward.user_id == user_id, TipAward.status == AwardStatus.PENDING_REVIEW
                )
            )
            demoted = 0
            for award in reviewable:
                transition(
                    session,
                    award,
                    AwardStatus.AWAITING_WALLET,
                    actor_id=None,
                    action=AuditAction.AWARD_WALLET_MISSING,
                    details={"wallet_removed": True},
                )
                demoted += 1
            locked = await self._count_locked(session, user_id, None)
            return WalletRemoval(current.address, demoted, locked)

    async def get_wallet(self, discord_id: int) -> Wallet | None:
        async with self._db.transaction() as session:
            user_id = await find_user_id(session, discord_id)
            return await active_wallet(session, user_id) if user_id else None

    async def history(self, discord_id: int, limit: int = 10) -> list[WalletHistory]:
        async with self._db.transaction() as session:
            user_id = await find_user_id(session, discord_id)
            if user_id is None:
                return []
            rows = await session.scalars(
                select(WalletHistory)
                .where(WalletHistory.user_id == user_id)
                .order_by(WalletHistory.id.desc())
                .limit(limit)
            )
            return list(rows)

    @staticmethod
    async def _count_locked(session: AsyncSession, user_id: int, new_address: str | None) -> int:
        """Unpaid approved awards whose locked payout wallet differs from the new wallet."""
        conditions = [
            TipAward.user_id == user_id,
            TipAward.status.in_([s for s in LOCKED_PAYOUT_STATUSES if s != AwardStatus.PAID]),
        ]
        if new_address is not None:
            conditions.append(TipAward.payout_wallet != new_address)
        return int(
            await session.scalar(select(func.count()).select_from(TipAward).where(*conditions)) or 0
        )
