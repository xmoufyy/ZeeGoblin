"""End-to-end lifecycle on a real migrated SQLite file, through the same services the bot uses."""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from sqlalchemy import select

from database.models import AuditLog, AwardStatus, BatchStatus, PaymentStatus
from services.payment_service import batch_csv, summary_csv
from tests.conftest import (
    HEAD_MOD,
    MOD,
    PAYMENT_MOD,
    make_address,
    make_signature,
    member,
    plain_member,
)


async def test_full_rollcall_to_payment_lifecycle(services, clock, db) -> None:
    # Members register wallets (one forgets).
    for discord_id in (1, 2, 3):
        await services.wallets.set_wallet(member(discord_id), make_address(discord_id))

    # 1. A moderator starts a roll call.
    event = await services.rollcalls.start(
        MOD, guild_id=42, channel_id=43, duration_minutes=3, reward_lamports=10_000_000
    )
    # 2. Members click "I'M HERE!" (one double-clicks).
    for discord_id in (1, 2, 3, 4, 1):
        await services.rollcalls.check_in(event.id, member(discord_id))

    # 3. The event expires and the scheduler finalizes it.
    clock.advance(minutes=3, seconds=5)
    [closed] = await services.rollcalls.finalize_due()
    # 4. Pending awards exist for every participant.
    assert (closed.created_awards, closed.pending_review, closed.awaiting_wallet) == (4, 3, 1)

    # The forgetful member registers late; their award becomes reviewable.
    await services.wallets.set_wallet(member(4), make_address(4))

    # 5. A head moderator approves the whole roll call.
    approval = await services.tips.approve_event(HEAD_MOD, event.id)
    assert approval.approved == 4 and approval.total_lamports == 40_000_000

    # 6. A payment moderator exports a batch.
    detail = await services.payments.create_batch(PAYMENT_MOD)
    assert detail.batch.total_lamports == 40_000_000
    rows = list(csv.DictReader(io.StringIO(batch_csv(detail).decode("utf-8-sig"))))
    assert {r["wallet"] for r in rows} == {make_address(i) for i in (1, 2, 3, 4)}
    assert sum(Decimal(r["amount_sol"]) for r in rows) == Decimal("0.04")
    summary = list(csv.DictReader(io.StringIO(summary_csv(detail).decode("utf-8-sig"))))
    assert len(summary) == 4

    # 7 + 8. They send SOL in their own wallet, then record and confirm each transaction.
    for index, total in enumerate(detail.wallets):
        record = await services.payments.record_payment(
            PAYMENT_MOD,
            batch_id=detail.batch.id,
            wallet=total.wallet,
            amount_lamports=total.lamports,
            signature=make_signature(index),
        )
        confirmed = await services.payments.confirm_payment(PAYMENT_MOD, record.payment.id)
        assert confirmed.payment.status == PaymentStatus.MANUALLY_CONFIRMED

    # 9. Every award is paid (manually confirmed, not chain-verified) and the batch completed.
    awards = await services.tips.list_awards(HEAD_MOD, event_id=event.id)
    assert {row.status for row in awards.items} == {AwardStatus.PAID}
    assert {row.verification_source for row in awards.items} == {"manual"}
    final = await services.payments.get_batch(PAYMENT_MOD, detail.batch.id)
    assert final.batch.status == BatchStatus.COMPLETED

    # Members can privately see their payout status.
    profile = await services.tips.profile(1)
    assert profile.paid_manual_lamports == 10_000_000 and profile.rollcall_count == 1
    own = await services.tips.list_awards(plain_member(1), discord_user_id=1)
    assert own.items[0].payment_signature is not None

    # 10. The audit trail tells the whole story for one award.
    award_id = own.items[0].id
    async with db.transaction() as session:
        trail = list(
            await session.scalars(
                select(AuditLog.action)
                .where(AuditLog.entity_type == "award", AuditLog.entity_id == str(award_id))
                .order_by(AuditLog.id)
            )
        )
        event_trail = list(
            await session.scalars(
                select(AuditLog.action)
                .where(AuditLog.entity_type == "event", AuditLog.entity_id == str(event.id))
                .order_by(AuditLog.id)
            )
        )
    assert trail == [
        "award.created",
        "award.approved",
        "award.queued",
        "award.payment_submitted",
        "award.paid",
    ]
    assert event_trail == ["rollcall.created", "rollcall.closed"]

    audit_page = await services.audit.list(HEAD_MOD, entity_type="batch")
    assert [entry.action for entry in audit_page.items] == ["batch.completed", "batch.created"]


async def test_audit_brief_end_to_end_with_restart(db_url, limits, clock) -> None:
    """The 15-step scenario from the audit brief, ending with a restart + recovery."""
    from database.session import Database
    from services.container import build_services
    from tests.conftest import OWNER

    db = Database(db_url)
    services = build_services(db, limits, clock)
    await services.wallets.set_wallet(member(1), make_address(1))
    # 1-3. Roll call, check-ins (with a duplicate click), expiry.
    event = await services.rollcalls.start(
        MOD, guild_id=9, channel_id=9, duration_minutes=3, reward_lamports=10_000_000
    )
    for discord_id in (1, 2, 3, 2):
        await services.rollcalls.check_in(event.id, member(discord_id))
    clock.advance(minutes=4)
    # 4. Awards generated (scheduler path).
    [closed] = await services.rollcalls.finalize_due()
    assert closed.created_awards == 3
    # 5. Missing wallets identified.
    missing = await services.notifications.missing_wallets(HEAD_MOD)
    assert {m.discord_user_id for m in missing} == {2, 3}
    # 6. Wallets registered.
    await services.wallets.set_wallet(member(2), make_address(2))
    await services.wallets.set_wallet(member(3), make_address(3))
    # 7-8. Review then bulk-approve.
    pending = await services.tips.list_awards(HEAD_MOD, status=AwardStatus.PENDING_REVIEW)
    ids = [row.id for row in pending.items]
    plan = await services.tips.preview_approval(HEAD_MOD, ids)
    assert len(plan.eligible) == 3 and plan.total_lamports == 30_000_000
    approval = await services.tips.approve_many(HEAD_MOD, ids)
    assert approval.approved == 3
    # 9-10. Batch + CSV.
    detail = await services.payments.create_batch(PAYMENT_MOD)
    rows = list(csv.DictReader(io.StringIO(batch_csv(detail).decode("utf-8-sig"))))
    assert sum(Decimal(r["amount_sol"]) for r in rows) == Decimal("0.03")
    # 11-12. Record + confirm one wallet's payment.
    record = await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(1),
        amount_lamports=10_000_000,
        signature=make_signature(1),
    )
    await services.payments.confirm_payment(PAYMENT_MOD, record.payment.id)
    # 13. The member sees it.
    history = await services.tips.list_awards(plain_member(1), discord_user_id=1)
    assert history.items[0].status == AwardStatus.PAID
    # 14. Audit entries.
    audit = await services.audit.list(OWNER, entity_type="payment")
    assert [e.action for e in audit.items] == ["payment.confirmed", "payment.submitted"]
    await db.dispose()

    # 15. Restart: recovery is a no-op, nothing duplicated, work continues.
    db2 = Database(db_url)
    try:
        restarted = build_services(db2, limits, clock)
        report = await restarted.recovery.recover()
        assert report.finalized == [] and report.still_active == []
        everything = await restarted.tips.list_awards(OWNER, page_size=50)
        assert everything.total == 3
        batch = await restarted.payments.get_batch(PAYMENT_MOD, detail.batch.id)
        assert batch.progress.paid_awards == 1
        assert batch.progress.remaining_lamports == 20_000_000
        tips_report = await restarted.tips.report(OWNER, event_id=event.id)
        assert tips_report.created_count == 3 and tips_report.awarded_lamports == 30_000_000
        assert tips_report.confirmed_payments == 1
        assert tips_report.outstanding_lamports == 20_000_000
    finally:
        await db2.dispose()
