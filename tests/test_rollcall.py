from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from database.models import AwardStatus, Event, EventEntry, EventStatus, TipAward
from services.errors import Conflict, InputError, PermissionDenied
from services.rollcall_service import CheckInOutcome
from tests.conftest import HEAD_MOD, MOD, make_address, member, plain_member

GUILD, CHANNEL = 555, 777
REWARD = 10_000_000  # 0.01 SOL


async def start(services, **overrides):
    params = {
        "guild_id": GUILD,
        "channel_id": CHANNEL,
        "duration_minutes": 3,
        "reward_lamports": REWARD,
    }
    params.update(overrides)
    return await services.rollcalls.start(MOD, **params)


async def test_start_event_records_deadline(services, clock) -> None:
    event = await start(services)
    assert event.status == EventStatus.ACTIVE
    assert event.started_at == clock.now
    assert (event.expires_at - event.started_at).total_seconds() == 180
    assert event.budget_lamports == services.limits.max_rollcall_budget
    assert event.participant_cap == 500


@pytest.mark.parametrize(
    "overrides",
    [
        {"duration_minutes": 0},
        {"duration_minutes": 61},
        {"reward_lamports": 0},
        {"reward_lamports": 100_000_001},
        {"budget_lamports": 5_000_000_001},
        {"budget_lamports": REWARD - 1},
        {"max_participants": 0},
    ],
)
async def test_start_validation(services, overrides) -> None:
    with pytest.raises(InputError):
        await start(services, **overrides)


async def test_start_requires_permission_and_single_active(services) -> None:
    with pytest.raises(PermissionDenied):
        await services.rollcalls.start(
            plain_member(1),
            guild_id=GUILD,
            channel_id=CHANNEL,
            duration_minutes=3,
            reward_lamports=REWARD,
        )
    await start(services)
    with pytest.raises(Conflict):
        await start(services)


async def test_valid_and_duplicate_check_in(services) -> None:
    event = await start(services)
    first = await services.rollcalls.check_in(event.id, member(1))
    assert first.outcome == CheckInOutcome.ACCEPTED and first.entry_count == 1
    assert first.has_wallet is False
    again = await services.rollcalls.check_in(event.id, member(1))
    assert again.outcome == CheckInOutcome.ALREADY_CHECKED_IN
    assert (await services.rollcalls.get(event.id)).entry_count == 1


async def test_expired_event_rejects_check_in(services, clock) -> None:
    event = await start(services)
    clock.advance(minutes=3)  # exactly at the deadline counts as expired
    result = await services.rollcalls.check_in(event.id, member(1))
    assert result.outcome == CheckInOutcome.CLOSED


async def test_unknown_event(services) -> None:
    assert (await services.rollcalls.check_in(999, member(1))).outcome == CheckInOutcome.NOT_FOUND


async def test_max_participants(services) -> None:
    event = await start(services, max_participants=2)
    outcomes = [(await services.rollcalls.check_in(event.id, member(i))).outcome for i in range(3)]
    assert outcomes == [CheckInOutcome.ACCEPTED, CheckInOutcome.ACCEPTED, CheckInOutcome.FULL]


async def test_budget_limits_participants(services) -> None:
    event = await start(services, budget_lamports=25_000_000)  # 2.5 rewards -> 2 people
    assert event.participant_cap == 2
    outcomes = [(await services.rollcalls.check_in(event.id, member(i))).outcome for i in range(3)]
    assert outcomes.count(CheckInOutcome.ACCEPTED) == 2
    assert outcomes[-1] == CheckInOutcome.FULL


async def test_simultaneous_check_ins(services, db) -> None:
    event = await start(services, max_participants=25)
    # 40 different members plus every one of the first 10 clicking twice, all at once.
    identities = [member(i) for i in range(40)] + [member(i) for i in range(10)]
    results = await asyncio.gather(
        *(services.rollcalls.check_in(event.id, ident) for ident in identities)
    )
    accepted = [r for r in results if r.outcome == CheckInOutcome.ACCEPTED]
    assert len(accepted) == 25
    async with db.transaction() as session:
        entries = await session.scalar(
            select(func.count()).select_from(EventEntry).where(EventEntry.event_id == event.id)
        )
    assert entries == 25
    assert (await services.rollcalls.get(event.id)).entry_count == 25


async def test_automatic_closure_creates_awards(services, clock, db) -> None:
    await services.wallets.set_wallet(member(1), make_address(1))
    await services.wallets.set_wallet(member(2), make_address(2))
    event = await start(services)
    for i in (1, 2, 3):
        await services.rollcalls.check_in(event.id, member(i))

    assert await services.rollcalls.finalize_due() == []  # not expired yet
    with pytest.raises(Conflict):
        await services.rollcalls.finalize_expired(event.id)

    clock.advance(minutes=3, seconds=1)
    [result] = await services.rollcalls.finalize_due()
    assert result.created_awards == 3
    assert result.pending_review == 2
    assert result.awaiting_wallet == 1  # member 3 never registered a wallet
    assert result.total_lamports == 3 * REWARD
    assert result.event.status == EventStatus.CLOSED
    assert result.event.close_reason == "expired"

    async with db.transaction() as session:
        awards = list(await session.scalars(select(TipAward).order_by(TipAward.id)))
    assert [a.status for a in awards] == [
        AwardStatus.PENDING_REVIEW,
        AwardStatus.PENDING_REVIEW,
        AwardStatus.AWAITING_WALLET,
    ]
    assert all(a.amount_lamports == REWARD and a.event_id == event.id for a in awards)
    assert all(a.payout_wallet is None for a in awards)  # snapshot happens at approval

    # No check-ins after closure, even with the clock rewound.
    clock.advance(minutes=-10)
    late = await services.rollcalls.check_in(event.id, member(99))
    assert late.outcome == CheckInOutcome.CLOSED


async def test_duplicate_finalization_is_idempotent(services, clock, db) -> None:
    event = await start(services)
    for i in range(5):
        await services.rollcalls.check_in(event.id, member(i))
    clock.advance(minutes=4)

    results = await asyncio.gather(
        services.rollcalls.finalize_expired(event.id),
        services.rollcalls.finalize_expired(event.id),
        services.rollcalls.finalize_due(),
        services.rollcalls.end(HEAD_MOD, event.id),
    )
    fresh = [r for r in [*results[:2], results[3]] if not r.already_finalized]
    fresh += results[2]
    assert len(fresh) == 1
    async with db.transaction() as session:
        count = await session.scalar(select(func.count()).select_from(TipAward))
    assert count == 5


async def test_shared_wallets_are_flagged(services, clock) -> None:
    shared = make_address(7)
    await services.wallets.set_wallet(member(1), shared)
    await services.wallets.set_wallet(member(2), shared)
    event = await start(services)
    await services.rollcalls.check_in(event.id, member(1))
    await services.rollcalls.check_in(event.id, member(2))
    clock.advance(minutes=5)
    [result] = await services.rollcalls.finalize_due()
    assert result.shared_wallets == {shared: [1, 2]}
    assert result.created_awards == 2  # flagged for review, not auto-excluded


async def test_end_early_and_cancel(services, db) -> None:
    event = await start(services)
    await services.rollcalls.check_in(event.id, member(1))
    with pytest.raises(PermissionDenied):
        await services.rollcalls.end(MOD, event.id)  # moderators can start but not end
    result = await services.rollcalls.end(HEAD_MOD, event.id)
    assert result.event.close_reason == "ended_early" and result.created_awards == 1

    second = await start(services)
    await services.rollcalls.check_in(second.id, member(1))
    cancelled = await services.rollcalls.end(HEAD_MOD, second.id, award=False)
    assert cancelled.event.status == EventStatus.CANCELLED and cancelled.created_awards == 0
    async with db.transaction() as session:
        events = list(await session.scalars(select(Event).order_by(Event.id)))
    assert [e.status for e in events] == [EventStatus.CLOSED, EventStatus.CANCELLED]
