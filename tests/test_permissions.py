from __future__ import annotations

import pytest

from config.permissions import PERMISSION_MATRIX, Actor, Permission, Role, require, resolve_roles
from services.errors import PermissionDenied

P = Permission
IDS = {"owner_user_id": 1, "head_mod_role_id": 10, "mod_role_id": 20, "payment_mod_role_id": 30}


def actor_with(*roles: Role) -> Actor:
    return Actor(99, frozenset({Role.MEMBER, *roles}))


def test_role_resolution() -> None:
    assert resolve_roles(5, [], **IDS) == {Role.MEMBER}
    assert resolve_roles(1, [], **IDS) == {Role.MEMBER, Role.OWNER}
    assert resolve_roles(5, [10, 30, 777], **IDS) == {Role.MEMBER, Role.HEAD_MOD, Role.PAYMENT_MOD}
    # Holding the owner's *role* is meaningless: ownership is the configured user ID only.
    assert Role.OWNER not in resolve_roles(5, [1], **IDS)


def test_owner_has_everything() -> None:
    owner = actor_with(Role.OWNER)
    assert all(owner.can(permission) for permission in Permission)


def test_head_moderator() -> None:
    head = actor_with(Role.HEAD_MOD)
    for permission in (
        P.ROLLCALL_START,
        P.ROLLCALL_END,
        P.AWARD_APPROVE,
        P.AWARD_VOID,
        P.DISPUTE_RESOLVE,
        P.AUDIT_VIEW,
        P.AWARD_REWALLET,
    ):
        assert head.can(permission), permission
    for permission in (
        P.BATCH_EXPORT,
        P.PAYMENT_RECORD,
        P.PAYMENT_CONFIRM,
        P.ADMIN_CONFIG,
        P.APPROVE_OWN_AWARD,
    ):
        assert not head.can(permission), permission


def test_moderator() -> None:
    mod = actor_with(Role.MOD)
    for permission in (P.ROLLCALL_START, P.AWARD_REQUEST, P.AWARD_VIEW_QUEUE, P.AWARD_FLAG):
        assert mod.can(permission), permission
    for permission in (
        P.ROLLCALL_END,
        P.AWARD_APPROVE,
        P.AWARD_VOID,
        P.AUDIT_VIEW,
        P.BATCH_EXPORT,
        P.PAYMENT_CONFIRM,
        P.ADMIN_CONFIG,
    ):
        assert not mod.can(permission), permission


def test_payment_moderator() -> None:
    payer = actor_with(Role.PAYMENT_MOD)
    for permission in (
        P.BATCH_EXPORT,
        P.BATCH_CANCEL,
        P.PAYMENT_RECORD,
        P.PAYMENT_CONFIRM,
        P.PAYMENT_VIEW,
    ):
        assert payer.can(permission), permission
    for permission in (
        P.AWARD_APPROVE,
        P.AWARD_VOID,
        P.ROLLCALL_START,
        P.AUDIT_VIEW,
        P.ADMIN_CONFIG,
    ):
        assert not payer.can(permission), permission


def test_member_restrictions() -> None:
    plain = actor_with()
    assert plain.permissions == PERMISSION_MATRIX[Role.MEMBER]
    assert plain.permissions == {P.WALLET_SELF, P.PROFILE_SELF, P.ROLLCALL_JOIN, P.TIP_HISTORY_SELF}
    with pytest.raises(PermissionDenied):
        require(plain, P.AWARD_APPROVE)


def test_combined_roles_are_a_union() -> None:
    both = actor_with(Role.MOD, Role.PAYMENT_MOD)
    assert both.can(P.AWARD_REQUEST) and both.can(P.PAYMENT_RECORD)
    assert not both.can(P.AWARD_APPROVE)
