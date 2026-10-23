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
