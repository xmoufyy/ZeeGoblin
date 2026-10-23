"""Wires the domain services together. Usable by the bot, tests, or a future dashboard."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from config.settings import Limits
from database.models import BotSetting
from database.session import Database
from services.audit_service import AuditAction, AuditService, record_audit
from services.payment_service import PaymentService
from services.recovery_service import RecoveryService
from services.rollcall_service import RollCallService
from services.tip_service import TipService
from services.wallet_service import WalletService
from utils.timeutil import Clock, utcnow


class SettingsStore:
    """Runtime key/value settings (e.g. bot presence) changed through owner commands."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str) -> str | None:
        async with self._db.transaction() as session:
            return await session.scalar(select(BotSetting.value).where(BotSetting.key == key))

    async def set(self, key: str, value: str, actor_id: int) -> None:
        async with self._db.transaction() as session:
            setting = await session.get(BotSetting, key)
            before = setting.value if setting else None
            if setting is None:
                session.add(BotSetting(key=key, value=value, updated_by_discord_id=actor_id))
            else:
                setting.value = value
                setting.updated_by_discord_id = actor_id
            record_audit(
                session,
                actor_id=actor_id,
                action=AuditAction.SETTING_CHANGED,
                entity_type="setting",
                entity_id=key,
                before={"value": before},
                after={"value": value},
            )


@dataclass
class Services:
    db: Database
    limits: Limits
    wallets: WalletService
    rollcalls: RollCallService
    tips: TipService
    payments: PaymentService
    audit: AuditService
    recovery: RecoveryService
    settings: SettingsStore


def build_services(db: Database, limits: Limits, clock: Clock = utcnow) -> Services:
    rollcalls = RollCallService(db, limits, clock)
    return Services(
        db=db,
        limits=limits,
        wallets=WalletService(db, clock),
        rollcalls=rollcalls,
        tips=TipService(db, limits, clock),
        payments=PaymentService(db, limits, clock),
        audit=AuditService(db),
        recovery=RecoveryService(rollcalls),
        settings=SettingsStore(db),
    )
