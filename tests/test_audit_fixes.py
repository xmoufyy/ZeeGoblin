"""Regression tests for findings in docs/CODE_AUDIT.md (IDs in each test name/docstring)."""

from __future__ import annotations

import asyncio

from sqlalchemy.exc import OperationalError

from bot.events import user_message
from database.models import AwardStatus, BatchStatus, EventStatus
from services.rollcall_service import CheckInOutcome
from tests.conftest import HEAD_MOD, MOD, OWNER, PAYMENT_MOD, make_address, make_signature, member


async def test_a01_voiding_last_disputed_award_completes_batch(services) -> None:
    """A-01: voiding the last outstanding award in a batch must close the batch."""
    for discord_id in (10, 11):
        await services.wallets.set_wallet(member(discord_id), make_address(discord_id))
        award = await services.tips.add_manual(MOD, member(discord_id), 10_000_000, "x")
        await services.tips.approve(HEAD_MOD, award.id)
    detail = await services.payments.create_batch(PAYMENT_MOD)
    paid = await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(10),
        amount_lamports=10_000_000,
        signature=make_signature(1),
    )
    await services.payments.confirm_payment(PAYMENT_MOD, paid.payment.id)
    # The other wallet's transfer went out wrong and was flagged -> award disputed.
    flagged = await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(11),
        amount_lamports=5_000_000,
        signature=make_signature(2),
        flag_mismatch=True,
    )
    [disputed_id] = flagged.award_ids
    assert (await services.payments.get_batch(PAYMENT_MOD, detail.batch.id)).batch.status == "open"

    await services.tips.void(HEAD_MOD, disputed_id, "Sent wrong amount; refunded off-platform")
    batch = (await services.payments.get_batch(PAYMENT_MOD, detail.batch.id)).batch
    assert batch.status == BatchStatus.COMPLETED
    assert (await services.tips.list_disputes(HEAD_MOD)).total == 0  # dispute closed too


async def test_a02_new_rollcall_right_after_deadline(services, clock) -> None:
    """A-02: starting a roll call just after the last one expired must not be refused."""
    first = await services.rollcalls.start(
        MOD, guild_id=1, channel_id=1, duration_minutes=1, reward_lamports=1_000
    )
    await services.rollcalls.check_in(first.id, member(5))
    clock.advance(minutes=1, seconds=1)  # expired, scheduler hasn't run yet
    second = await services.rollcalls.start(
        MOD, guild_id=1, channel_id=1, duration_minutes=1, reward_lamports=1_000
    )
    assert (await services.rollcalls.get(first.id)).status == EventStatus.CLOSED
    assert second.status == EventStatus.ACTIVE
    awards = await services.tips.list_awards(HEAD_MOD, status=AwardStatus.AWAITING_WALLET)
    assert awards.total == 1  # first roll call's award was created exactly once


async def test_a03_bulk_approval_is_one_transaction_with_reasons(services) -> None:
    """A-03/A-07: bulk approval plans first, skips ineligible awards with a reason."""
    ok = []
    for discord_id in (20, 21):
        await services.wallets.set_wallet(member(discord_id), make_address(discord_id))
        ok.append(await services.tips.add_manual(MOD, member(discord_id), 10_000_000, "ok"))
    no_wallet = await services.tips.add_manual(MOD, member(22), 10_000_000, "no wallet")
    await services.wallets.set_wallet(member(HEAD_MOD.discord_id), make_address(23))
    own = await services.tips.add_manual(MOD, member(HEAD_MOD.discord_id), 10_000_000, "mine")
    ids = [ok[0].id, ok[1].id, no_wallet.id, own.id, 999]

    plan = await services.tips.preview_approval(HEAD_MOD, ids)
    assert [r.id for r in plan.eligible] == [ok[0].id, ok[1].id]
    assert plan.total_lamports == 20_000_000
    assert plan.skipped == {
        no_wallet.id: "is awaiting wallet",
        own.id: "is your own award",
        999: "doesn't exist",
    }
    # Preview changed nothing.
    assert (await services.tips.get(HEAD_MOD, ok[0].id)).status == AwardStatus.PENDING_REVIEW

    result = await services.tips.approve_many(HEAD_MOD, ids)
    assert result.approved_ids == [ok[0].id, ok[1].id] and result.total_lamports == 20_000_000
    again = await services.tips.approve_many(HEAD_MOD, ids)  # duplicate click
    assert again.approved == 0 and again.skipped[ok[0].id] == "is approved"


async def test_concurrent_approvals_never_double_approve(services) -> None:
    """Two moderators approving the same awards at once: each award is approved once."""
    ids = []
    for discord_id in range(30, 40):
        await services.wallets.set_wallet(member(discord_id), make_address(discord_id))
        ids.append((await services.tips.add_manual(MOD, member(discord_id), 1_000, "x")).id)
    results = await asyncio.gather(
        services.tips.approve_many(HEAD_MOD, ids), services.tips.approve_many(OWNER, ids)
    )
    assert sorted(results[0].approved_ids + results[1].approved_ids) == ids


async def test_hundreds_of_simultaneous_checkins(services) -> None:
    event = await services.rollcalls.start(
        MOD,
        guild_id=1,
        channel_id=1,
        duration_minutes=3,
        reward_lamports=1_000,
        max_participants=250,
    )
    results = await asyncio.gather(
        *(services.rollcalls.check_in(event.id, member(1000 + i)) for i in range(300))
    )
    assert sum(r.outcome == CheckInOutcome.ACCEPTED for r in results) == 250
    assert (await services.rollcalls.get(event.id)).entry_count == 250


def test_a05_database_locked_message() -> None:
    """A-05: a lock timeout gives a clear 'nothing changed' message, not a crash notice."""
    error = OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))
    assert "nothing was changed" in user_message(error)
    assert user_message(OperationalError("x", {}, Exception("disk I/O error"))) is None
