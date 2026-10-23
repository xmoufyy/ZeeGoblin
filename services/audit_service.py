"""Append-only audit trail. Writes happen inside the caller's transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.permissions import Actor, Permission, require
from database.models import AuditLog
from database.session import Database


class AuditAction(StrEnum):
    WALLET_REGISTERED = "wallet.registered"
    WALLET_CHANGED = "wallet.changed"
    WALLET_REMOVED = "wallet.removed"
    ROLLCALL_CREATED = "rollcall.created"
    ROLLCALL_CLOSED = "rollcall.closed"
    ROLLCALL_CANCELLED = "rollcall.cancelled"
    AWARD_CREATED = "award.created"
    AWARD_MANUAL_REQUESTED = "award.manual_requested"
    AWARD_WALLET_RESOLVED = "award.wallet_resolved"
    AWARD_WALLET_MISSING = "award.wallet_missing"
    AWARD_APPROVED = "award.approved"
    AWARD_VOIDED = "award.voided"
    AWARD_RETURNED_TO_REVIEW = "award.returned_to_review"
    AWARD_FLAGGED = "award.flagged"
    AWARD_QUEUED = "award.queued"
    AWARD_RELEASED = "award.released"
    AWARD_PAYMENT_SUBMITTED = "award.payment_submitted"
    AWARD_PAID = "award.paid"
    DISPUTE_RESOLVED = "dispute.resolved"
    BATCH_CREATED = "batch.created"
    BATCH_CANCELLED = "batch.cancelled"
    BATCH_COMPLETED = "batch.completed"
    BATCH_EXPORTED = "batch.exported"
    PAYMENT_SUBMITTED = "payment.submitted"
    PAYMENT_FLAGGED = "payment.flagged"
    PAYMENT_CONFIRMED = "payment.confirmed"
    SETTING_CHANGED = "admin.setting_changed"
    MEMBER_PREFERENCE_CHANGED = "member.preference_changed"


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    page: int
    page_size: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


def _dump(data: dict[str, Any] | None) -> str | None:
    if data is None:
        return None
    return json.dumps(data, default=_json_default, sort_keys=True, separators=(",", ":"))


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def record_audit(
    session: AsyncSession,
    *,
    actor_id: int | None,
    action: AuditAction,
    entity_type: str,
    entity_id: int | str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
) -> AuditLog:
    """Add an audit row to the current transaction. If the transaction fails, so does this."""
    entry = AuditLog(
        actor_discord_id=actor_id,
        action=str(action),
        entity_type=entity_type,
        entity_id=str(entity_id),
        before_json=_dump(before),
        after_json=_dump(after),
        reason=reason,
    )
    session.add(entry)
    return entry


class AuditService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(
        self,
        actor: Actor,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        actor_filter: int | None = None,
        action_prefix: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> Page[AuditLog]:
        require(actor, Permission.AUDIT_VIEW)
        page = max(1, page)
        conditions = []
        if entity_type:
            conditions.append(AuditLog.entity_type == entity_type)
        if entity_id:
            conditions.append(AuditLog.entity_id == str(entity_id))
        if actor_filter:
            conditions.append(AuditLog.actor_discord_id == actor_filter)
        if action_prefix:
            conditions.append(AuditLog.action.startswith(action_prefix, autoescape=True))
        async with self._db.transaction() as session:
            total = await session.scalar(
                select(func.count()).select_from(AuditLog).where(*conditions)
            )
            rows = await session.scalars(
                select(AuditLog)
                .where(*conditions)
                .order_by(AuditLog.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            return Page(list(rows), page, page_size, int(total or 0))
