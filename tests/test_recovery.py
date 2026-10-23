from __future__ import annotations

import pytest
from sqlalchemy import func, select

import services.rollcall_service as rollcall_module
from database.models import AwardStatus, Event, EventStatus, TipAward
from database.session import Database
from services.audit_service import AuditAction
from services.container import build_services
from services.rollcall_service import CheckInOutcome
from tests.conftest import MOD, make_address, member


async def count_awards(db: Database) -> int:
    async with db.transaction() as session:
        return int(await session.scalar(select(func.count()).select_from(TipAward)) or 0)


async def test_restart_during_active_rollcall(db_url, limits, clock) -> None:
    db = Database(db_url)
    services = build_services(db, limits, clock)
    event = await services.rollcalls.start(
        MOD, guild_id=1, channel_id=2, duration_minutes=3, reward_lamports=10_000_000
    )
    await services.rollcalls.check_in(event.id, member(1))
    await db.dispose()  # "process exits"

    db2 = Database(db_url)  # "process restarts"
    try:
        restarted = build_services(db2, limits, clock)
        report = await restarted.recovery.recover()
        assert report.finalized == []
        assert [e.id for e in report.still_active] == [event.id]
        again = await restarted.rollcalls.check_in(event.id, member(1))
        assert again.outcome == CheckInOutcome.ALREADY_CHECKED_IN  # entry survived the restart
        new = await restarted.rollcalls.check_in(event.id, member(2))
        assert new.outcome == CheckInOutcome.ACCEPTED and new.entry_count == 2
    finally:
        await db2.dispose()


async def test_expired_while_offline_is_finalized_on_startup(db_url, limits, clock) -> None:
    db = Database(db_url)
    services = build_services(db, limits, clock)
    await services.wallets.set_wallet(member(1), make_address(1))
    event = await services.rollcalls.start(
        MOD, guild_id=1, channel_id=2, duration_minutes=3, reward_lamports=10_000_000
    )
    await services.rollcalls.check_in(event.id, member(1))
    await services.rollcalls.check_in(event.id, member(2))
    await db.dispose()

    clock.advance(hours=8)  # the PC was off overnight
    db2 = Database(db_url)
    try:
        restarted = build_services(db2, limits, clock)
        report = await restarted.recovery.recover()
        assert len(report.finalized) == 1
        assert report.finalized[0].created_awards == 2
        assert [e.id for e in report.needs_discord_sync] == [event.id]

        # Running recovery again (e.g. a second restart) changes nothing.
        second = await restarted.recovery.recover()
        assert second.finalized == []
        assert await count_awards(db2) == 2

        await restarted.rollcalls.mark_synced(event.id, announcement=True, staff=True)
        assert (await restarted.recovery.recover()).needs_discord_sync == []
    finally:
        await db2.dispose()


async def test_crash_during_finalization_rolls_back_then_recovers(
    db_url, limits, clock, monkeypatch
) -> None:
    db = Database(db_url)
    services = build_services(db, limits, clock)
    event = await services.rollcalls.start(
        MOD, guild_id=1, channel_id=2, duration_minutes=1, reward_lamports=10_000_000
    )
    for i in range(4):
        await services.rollcalls.check_in(event.id, member(i))
    clock.advance(minutes=2)

    real_record = rollcall_module.record_audit

    def crash_on_close(session, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("action") == AuditAction.ROLLCALL_CLOSED:
            raise RuntimeError("simulated power cut mid-finalization")
        return real_record(session, **kwargs)

    monkeypatch.setattr(rollcall_module, "record_audit", crash_on_close)
    with pytest.raises(RuntimeError):
        await services.rollcalls.finalize_expired(event.id)
    # Awards were added before the crash point but the transaction never committed.
    assert await count_awards(db) == 0
    assert (await services.rollcalls.get(event.id)).status == EventStatus.ACTIVE
    await db.dispose()

    monkeypatch.setattr(rollcall_module, "record_audit", real_record)
    db2 = Database(db_url)
    try:
        restarted = build_services(db2, limits, clock)
        report = await restarted.recovery.recover()
        assert len(report.finalized) == 1 and report.finalized[0].created_awards == 4
        assert await count_awards(db2) == 4
        async with db2.transaction() as session:
            statuses = set(await session.scalars(select(TipAward.status)))
            closed = await session.get(Event, event.id)
        assert statuses == {AwardStatus.AWAITING_WALLET}
        assert closed.status == EventStatus.CLOSED
    finally:
        await db2.dispose()


async def test_scheduler_failure_is_isolated(services, clock, monkeypatch) -> None:
    first = await services.rollcalls.start(
        MOD, guild_id=1, channel_id=2, duration_minutes=1, reward_lamports=1_000
    )
    second = await services.rollcalls.start(
        MOD, guild_id=2, channel_id=2, duration_minutes=1, reward_lamports=1_000
    )
    clock.advance(minutes=2)
    real = services.rollcalls.finalize_expired

    async def flaky(event_id: int):  # type: ignore[no-untyped-def]
        if event_id == first.id:
            raise RuntimeError("transient")
        return await real(event_id)

    monkeypatch.setattr(services.rollcalls, "finalize_expired", flaky)
    results = await services.rollcalls.finalize_due()
    assert [r.event.id for r in results] == [second.id]
    monkeypatch.setattr(services.rollcalls, "finalize_expired", real)
    assert [r.event.id for r in await services.rollcalls.finalize_due()] == [first.id]
