from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select, text

from database.models import AuditLog, AwardStatus, DisputeResolution
from services.errors import Conflict, InputError, InvalidTransition, PermissionDenied
from tests.conftest import HEAD_MOD, MOD, OWNER, PAYMENT_MOD, make_address, member, plain_member
from utils.currency import AmountError, format_sol, parse_sol


class TestCurrency:
    @pytest.mark.parametrize(
        ("text", "lamports"),
        [
            ("0.01", 10_000_000),
            ("1", 1_000_000_000),
            (".5", 500_000_000),
            ("0.000000001", 1),
            ("2.5 SOL", 2_500_000_000),
            ("  0.1  ", 100_000_000),
            ("123456789012.123456789", 123_456_789_012_123_456_789),
        ],
    )
    def test_exact_parsing(self, text: str, lamports: int) -> None:
        assert parse_sol(text) == lamports

    @pytest.mark.parametrize(
        "text",
        [
            "0",
            "0.0",
            "-1",
            "+1",
            "1e-3",
            "1,000",
            "0.0000000001",
            "abc",
            "",
            "nan",
            "inf",
            "1.2.3",
            "0x10",
            "1_000",
            "１",
        ],
    )
    def test_rejects_bad_amounts(self, text: str) -> None:
        with pytest.raises(AmountError):
            parse_sol(text)

    def test_formatting_is_exact(self) -> None:
        assert format_sol(10_000_000) == "0.01"
        assert format_sol(1) == "0.000000001"
        assert format_sol(1_000_000_000) == "1"
        assert format_sol(10_000_000, fixed=True) == "0.010000000"
        # 0.1 + 0.2 is the classic float trap; lamports stay exact.
        assert format_sol(parse_sol("0.1") + parse_sol("0.2")) == "0.3"
        assert Decimal(format_sol(123_456_789_012_123_456_789)) == Decimal("123456789012.123456789")


async def _approved_award(services, discord_id=10, amount=5_000_000, reason="Clip of the week"):
    await services.wallets.set_wallet(member(discord_id), make_address(discord_id))
    award = await services.tips.add_manual(MOD, member(discord_id), amount, reason)
    return await services.tips.approve(HEAD_MOD, award.id)


async def test_manual_award_creation(services) -> None:
    award = await services.tips.add_manual(MOD, member(10), parse_sol("0.25"), "Won stream call")
    assert award.status == AwardStatus.AWAITING_WALLET
    assert award.amount_lamports == 250_000_000
    assert award.source == "manual" and award.event_id is None


async def test_manual_award_limits_and_self_awards(services) -> None:
    with pytest.raises(InputError):
        await services.tips.add_manual(MOD, member(10), services.limits.max_manual_award + 1, "x")
    with pytest.raises(InputError):
        await services.tips.add_manual(MOD, member(10), 1, "   ")
    with pytest.raises(PermissionDenied):
        await services.tips.add_manual(MOD, member(MOD.discord_id), 1, "me me me")
    with pytest.raises(PermissionDenied):
        await services.tips.add_manual(plain_member(5), member(10), 1, "nope")


async def test_duplicate_manual_award_prevention(services, clock) -> None:
    await services.tips.add_manual(MOD, member(10), 5_000_000, "Call win")
    with pytest.raises(Conflict):
        await services.tips.add_manual(MOD, member(10), 5_000_000, "Call win")
    clock.advance(minutes=11)
    await services.tips.add_manual(MOD, member(10), 5_000_000, "Call win")


async def test_approval_snapshots_wallet(services) -> None:
    award = await _approved_award(services)
    assert award.status == AwardStatus.APPROVED
    assert award.payout_wallet == make_address(10)
    assert award.approved_by_discord_id == HEAD_MOD.discord_id


async def test_approval_permissions(services) -> None:
    await services.wallets.set_wallet(member(HEAD_MOD.discord_id), make_address(3))
    own = await services.tips.add_manual(MOD, member(HEAD_MOD.discord_id), 1_000, "Mod pay")
    with pytest.raises(PermissionDenied):
        await services.tips.approve(HEAD_MOD, own.id)  # no approving your own award
    with pytest.raises(PermissionDenied):
        await services.tips.approve(MOD, own.id)
    assert (await services.tips.approve(OWNER, own.id)).status == AwardStatus.APPROVED


async def test_invalid_transitions(services) -> None:
    pending = await services.tips.add_manual(MOD, member(10), 1_000, "No wallet yet")
    with pytest.raises(InvalidTransition):
        await services.tips.approve(HEAD_MOD, pending.id)  # awaiting_wallet -> approved
    approved = await _approved_award(services, discord_id=11)
    with pytest.raises(InvalidTransition):
        await services.tips.approve(HEAD_MOD, approved.id)  # approved twice
    voided = await services.tips.void(HEAD_MOD, approved.id, "Duplicate")
    assert voided.status == AwardStatus.VOIDED
    with pytest.raises(InvalidTransition):
        await services.tips.approve(HEAD_MOD, voided.id)
    with pytest.raises(InvalidTransition):
        await services.tips.void(HEAD_MOD, voided.id, "again")


async def test_voiding_keeps_history(services, db) -> None:
    award = await _approved_award(services)
    with pytest.raises(PermissionDenied):
        await services.tips.void(MOD, award.id, "nope")
    with pytest.raises(PermissionDenied):
        await services.tips.void(PAYMENT_MOD, award.id, "nope")
    await services.tips.void(HEAD_MOD, award.id, "Alt account")
    row = await services.tips.get(HEAD_MOD, award.id)
    assert row.status == AwardStatus.VOIDED
    async with db.transaction() as session:
        actions = list(
            await session.scalars(
                select(AuditLog.action)
                .where(AuditLog.entity_type == "award", AuditLog.entity_id == str(award.id))
                .order_by(AuditLog.id)
            )
        )
        with pytest.raises(Exception, match="never deleted"):
            await session.execute(text("DELETE FROM tip_awards"))
    assert actions == ["award.manual_requested", "award.approved", "award.voided"]


async def test_amount_is_immutable(services, db) -> None:
    award = await _approved_award(services)
    with pytest.raises(Exception, match="immutable"):
        async with db.transaction() as session:
            await session.execute(
                text("UPDATE tip_awards SET amount_lamports = 999 WHERE id = :id"), {"id": award.id}
            )


async def test_duplicate_rollcall_award_prevented_by_constraint(services, clock, db) -> None:
    event = await services.rollcalls.start(
        MOD, guild_id=1, channel_id=1, duration_minutes=1, reward_lamports=1_000
    )
    await services.rollcalls.check_in(event.id, member(10))
    clock.advance(minutes=2)
    await services.rollcalls.finalize_due()
    with pytest.raises(Exception, match="UNIQUE"):
        async with db.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO tip_awards (user_id, source, event_id, event_entry_id, reason,"
                    " amount_lamports, status, created_at, updated_at)"
                    " SELECT user_id, 'rollcall', event_id, id, 'dup', 1000, 'pending_review',"
                    " entered_at, entered_at FROM event_entries"
                )
            )


async def test_bulk_event_approval(services, clock) -> None:
    for i in (1, 2, 3):
        await services.wallets.set_wallet(member(i), make_address(i))
    await services.wallets.set_wallet(member(HEAD_MOD.discord_id), make_address(50))
    event = await services.rollcalls.start(
        MOD, guild_id=1, channel_id=1, duration_minutes=1, reward_lamports=10_000_000
    )
    for i in (1, 2, 3, 4, HEAD_MOD.discord_id):
        await services.rollcalls.check_in(event.id, member(i))
    clock.advance(minutes=2)
    await services.rollcalls.finalize_due()
    result = await services.tips.approve_event(HEAD_MOD, event.id)
    assert result.approved == 3 and result.skipped_own == 1
    assert result.total_lamports == 30_000_000


async def test_profile_totals(services, clock) -> None:
    await services.wallets.set_wallet(member(10), make_address(10))
    event = await services.rollcalls.start(
        MOD, guild_id=1, channel_id=1, duration_minutes=1, reward_lamports=10_000_000
    )
    await services.rollcalls.check_in(event.id, member(10))
    clock.advance(minutes=2)
    await services.rollcalls.finalize_due()
    manual = await services.tips.add_manual(MOD, member(10), 250_000_000, "Call win")
    await services.tips.approve(HEAD_MOD, manual.id)
    voided = await services.tips.add_manual(MOD, member(10), 1_000_000_000, "Mistake")
    await services.tips.void(HEAD_MOD, voided.id, "Typo")

    profile = await services.tips.profile(10)
    assert profile.wallet == make_address(10)
    assert profile.rollcall_count == 1
    assert profile.by_status[AwardStatus.PENDING_REVIEW].lamports == 10_000_000
    assert profile.by_status[AwardStatus.APPROVED].lamports == 250_000_000
    assert profile.by_status[AwardStatus.VOIDED].lamports == 1_000_000_000
    assert profile.earned_lamports == 260_000_000  # voided awards are excluded
    assert profile.paid_manual_lamports == 0

    unknown = await services.tips.profile(424242)
    assert unknown.earned_lamports == 0 and unknown.wallet is None


async def test_member_history_privacy(services) -> None:
    award = await services.tips.add_manual(MOD, member(10), 1_000, "Thing")
    own = await services.tips.list_awards(plain_member(10), discord_user_id=10)
    assert [row.id for row in own.items] == [award.id]
    with pytest.raises(PermissionDenied):
        await services.tips.list_awards(plain_member(11), discord_user_id=10)
    with pytest.raises(PermissionDenied):
        await services.tips.list_awards(plain_member(11))
    with pytest.raises(PermissionDenied):
        await services.tips.get(plain_member(11), award.id)


async def test_pagination_and_filtering(services) -> None:
    for i in range(23):
        await services.tips.add_manual(MOD, member(100 + i), 1_000 + i, f"Award {i}")
    page1 = await services.tips.list_awards(
        HEAD_MOD, status=AwardStatus.AWAITING_WALLET, page_size=10
    )
    page3 = await services.tips.list_awards(
        HEAD_MOD, status=AwardStatus.AWAITING_WALLET, page=3, page_size=10
    )
    assert page1.total == 23 and page1.pages == 3 and len(page1.items) == 10
    assert len(page3.items) == 3
    none = await services.tips.list_awards(HEAD_MOD, status=AwardStatus.PAID)
    assert none.total == 0 and none.items == []


async def test_dispute_flow(services) -> None:
    award = await _approved_award(services)
    dispute = await services.tips.flag(MOD, award.id, "Possible alt account")
    assert (await services.tips.get(HEAD_MOD, award.id)).status == AwardStatus.DISPUTED
    with pytest.raises(PermissionDenied):
        await services.tips.resolve_dispute(MOD, dispute.id, DisputeResolution.VOIDED, "x")
    with pytest.raises(InvalidTransition):  # nothing was paid, can't settle as paid
        await services.tips.resolve_dispute(HEAD_MOD, dispute.id, DisputeResolution.SETTLED, "x")
    await services.tips.resolve_dispute(HEAD_MOD, dispute.id, DisputeResolution.REOPENED, "Legit")
    row = await services.tips.get(HEAD_MOD, award.id)
    assert row.status == AwardStatus.PENDING_REVIEW and row.payout_wallet is None
    with pytest.raises(Conflict):
        await services.tips.resolve_dispute(HEAD_MOD, dispute.id, DisputeResolution.VOIDED, "x")
