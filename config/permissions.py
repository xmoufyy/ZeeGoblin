"""Role-based permission matrix.

Roles are derived server-side from the configured owner ID and Discord role IDs on every
interaction. Moderators cannot elevate themselves: the matrix is code, the owner ID and
role IDs come from configuration, and no command edits either.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from services.errors import PermissionDenied


class Role(StrEnum):
    OWNER = "owner"
    HEAD_MOD = "head_mod"
    MOD = "mod"
    PAYMENT_MOD = "payment_mod"
    MEMBER = "member"


class Permission(StrEnum):
    # Member self-service
    WALLET_SELF = "wallet.self"
    PROFILE_SELF = "profile.self"
    ROLLCALL_JOIN = "rollcall.join"
    TIP_HISTORY_SELF = "tip.history.self"
    # Moderation
    ROLLCALL_START = "rollcall.start"
    ROLLCALL_VIEW = "rollcall.view"
    AWARD_REQUEST = "award.request"
    AWARD_VIEW_QUEUE = "award.view_queue"
    AWARD_FLAG = "award.flag"
    TIP_HISTORY_ANY = "tip.history.any"
    # Head moderation
    ROLLCALL_END = "rollcall.end"
    AWARD_APPROVE = "award.approve"
    AWARD_VOID = "award.void"
    AWARD_REWALLET = "award.rewallet"
    DISPUTE_RESOLVE = "dispute.resolve"
    AUDIT_VIEW = "audit.view"
    # Payments
    BATCH_EXPORT = "batch.export"
    BATCH_VIEW = "batch.view"
    BATCH_CANCEL = "batch.cancel"
    PAYMENT_RECORD = "payment.record"
    PAYMENT_CONFIRM = "payment.confirm"
    PAYMENT_VIEW = "payment.view"
    # Owner only
    ADMIN_CONFIG = "admin.config"
    APPROVE_OWN_AWARD = "award.approve_own"


_MEMBER = frozenset(
    {
        Permission.WALLET_SELF,
        Permission.PROFILE_SELF,
        Permission.ROLLCALL_JOIN,
        Permission.TIP_HISTORY_SELF,
    }
)
_MOD = _MEMBER | {
    Permission.ROLLCALL_START,
    Permission.ROLLCALL_VIEW,
    Permission.AWARD_REQUEST,
    Permission.AWARD_VIEW_QUEUE,
    Permission.AWARD_FLAG,
    Permission.TIP_HISTORY_ANY,
}
_HEAD_MOD = _MOD | {
    Permission.ROLLCALL_END,
    Permission.AWARD_APPROVE,
    Permission.AWARD_VOID,
    Permission.AWARD_REWALLET,
    Permission.DISPUTE_RESOLVE,
    Permission.AUDIT_VIEW,
    Permission.BATCH_VIEW,
    Permission.PAYMENT_VIEW,
}
_PAYMENT_MOD = _MEMBER | {
    Permission.AWARD_VIEW_QUEUE,
    Permission.TIP_HISTORY_ANY,
    Permission.BATCH_EXPORT,
    Permission.BATCH_VIEW,
    Permission.BATCH_CANCEL,
    Permission.PAYMENT_RECORD,
    Permission.PAYMENT_CONFIRM,
    Permission.PAYMENT_VIEW,
}

PERMISSION_MATRIX: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.HEAD_MOD: frozenset(_HEAD_MOD),
    Role.MOD: frozenset(_MOD),
    Role.PAYMENT_MOD: frozenset(_PAYMENT_MOD),
    Role.MEMBER: _MEMBER,
}


@dataclass(frozen=True)
class Actor:
    """Who is performing an action, with roles resolved server-side."""

    discord_id: int
    roles: frozenset[Role] = field(default_factory=lambda: frozenset({Role.MEMBER}))
    username: str = ""

    @property
    def permissions(self) -> frozenset[Permission]:
        granted: set[Permission] = set()
        for role in self.roles:
            granted |= PERMISSION_MATRIX[role]
        return frozenset(granted)

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    @property
    def is_owner(self) -> bool:
        return Role.OWNER in self.roles


def resolve_roles(
    discord_id: int,
    role_ids: Iterable[int],
    *,
    owner_user_id: int,
    head_mod_role_id: int,
    mod_role_id: int,
    payment_mod_role_id: int,
) -> frozenset[Role]:
    held = set(role_ids)
    roles = {Role.MEMBER}
    if discord_id == owner_user_id:
        roles.add(Role.OWNER)
    if head_mod_role_id in held:
        roles.add(Role.HEAD_MOD)
    if mod_role_id in held:
        roles.add(Role.MOD)
    if payment_mod_role_id in held:
        roles.add(Role.PAYMENT_MOD)
    return frozenset(roles)


def require(actor: Actor, permission: Permission) -> None:
    if not actor.can(permission):
        raise PermissionDenied("You don't have permission to do that.")
