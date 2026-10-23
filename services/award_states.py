"""The award status machine. Every status change goes through :func:`transition`."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AwardStatus, TipAward
from services.audit_service import AuditAction, record_audit
from services.errors import InvalidTransition

S = AwardStatus

ALLOWED_TRANSITIONS: dict[AwardStatus, frozenset[AwardStatus]] = {
    S.AWAITING_WALLET: frozenset({S.PENDING_REVIEW, S.VOIDED, S.DISPUTED}),
    S.PENDING_REVIEW: frozenset({S.APPROVED, S.AWAITING_WALLET, S.VOIDED, S.DISPUTED}),
    # approved -> pending_review is the explicit "re-review payout wallet" path.
    S.APPROVED: frozenset({S.QUEUED, S.PENDING_REVIEW, S.VOIDED, S.DISPUTED}),
    # queued -> approved only happens when a batch is cancelled.
    S.QUEUED: frozenset({S.APPROVED, S.PAYMENT_SUBMITTED}),
    S.PAYMENT_SUBMITTED: frozenset({S.PAID, S.DISPUTED}),
    S.PAID: frozenset(),
    S.DISPUTED: frozenset({S.PENDING_REVIEW, S.VOIDED, S.PAID}),
    S.VOIDED: frozenset(),
}

STATUS_LABELS: dict[AwardStatus, str] = {
    S.AWAITING_WALLET: "awaiting wallet",
    S.PENDING_REVIEW: "pending review",
    S.APPROVED: "approved",
    S.QUEUED: "queued for payment",
    S.PAYMENT_SUBMITTED: "payment submitted",
    S.PAID: "paid",
    S.DISPUTED: "disputed",
    S.VOIDED: "voided",
}


def label(status: str) -> str:
    return STATUS_LABELS.get(AwardStatus(status), status)


def can_transition(current: str, new: AwardStatus) -> bool:
    return new in ALLOWED_TRANSITIONS[AwardStatus(current)]


def transition(
    session: AsyncSession,
    award: TipAward,
    new_status: AwardStatus,
    *,
    actor_id: int | None,
    action: AuditAction,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    old_status = AwardStatus(award.status)
    if new_status not in ALLOWED_TRANSITIONS[old_status]:
        raise InvalidTransition(
            f"Award #{award.id} is **{label(old_status)}** and can't be moved to "
            f"**{label(new_status)}**."
        )
    award.status = new_status
    record_audit(
        session,
        actor_id=actor_id,
        action=action,
        entity_type="award",
        entity_id=award.id,
        before={"status": old_status},
        after={"status": new_status, **(details or {})},
        reason=reason,
    )
