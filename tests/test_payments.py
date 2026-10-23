from __future__ import annotations

import asyncio
import csv
import io
from decimal import Decimal

import pytest
from sqlalchemy import select

from config.settings import Limits
from database.models import AwardStatus, BatchStatus, DisputeResolution, PaymentStatus, TipAward
from database.session import Database
from services.container import build_services
from services.errors import Conflict, InputError, NotFound, PermissionDenied
from services.payment_service import BATCH_CSV_HEADER, batch_csv, summary_csv
from tests.conftest import (
    HEAD_MOD,
    MOD,
    OWNER,
    PAYMENT_MOD,
    PAYMENT_MOD_2,
    make_address,
    make_signature,
    member,
    plain_member,
)
from utils.csv_export import safe_cell
from utils.currency import parse_sol


async def approve_awards(services, specs):
    """specs: list of (discord_id, amount_text, reason). Returns award rows."""
    rows = []
    for discord_id, amount, reason in specs:
        if await services.wallets.get_wallet(discord_id) is None:
            await services.wallets.set_wallet(member(discord_id), make_address(discord_id))
        award = await services.tips.add_manual(MOD, member(discord_id), parse_sol(amount), reason)
        rows.append(await services.tips.approve(HEAD_MOD, award.id))
    return rows


def parse_csv(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


async def test_batch_creation_and_csv(services) -> None:
    awards = await approve_awards(
        services,
        [(10, "0.01", "Roll call"), (10, "0.02", "Call win"), (11, "0.1", "=HYPERLINK(evil)")],
    )
    pending = await services.tips.add_manual(MOD, member(12), 1_000, "Not approved")

    detail = await services.payments.create_batch(PAYMENT_MOD)
    assert detail.batch.status == BatchStatus.OPEN
    assert detail.batch.award_count == 3
    assert detail.batch.total_lamports == parse_sol("0.13")
    assert {line.award_id for line in detail.lines} == {a.id for a in awards}
    assert pending.id not in {line.award_id for line in detail.lines}
    for award in awards:
        assert (await services.tips.get(HEAD_MOD, award.id)).status == AwardStatus.QUEUED

    rows = parse_csv(batch_csv(detail))
    assert list(rows[0]) == list(BATCH_CSV_HEADER)
    assert sum(Decimal(r["amount_sol"]) for r in rows) == Decimal("0.13")  # exact totals
    assert {r["batch_id"] for r in rows} == {str(detail.batch.id)}
    injected = next(r for r in rows if r["discord_user_id"] == "11")
    assert injected["reason"] == "'=HYPERLINK(evil)"

    summary = parse_csv(summary_csv(detail))
    by_wallet = {r["wallet"]: r for r in summary}
    assert Decimal(by_wallet[make_address(10)]["amount_sol"]) == Decimal("0.03")
    assert by_wallet[make_address(10)]["award_count"] == "2"
    assert sum(Decimal(r["amount_sol"]) for r in summary) == Decimal("0.13")


def test_formula_escaping() -> None:
    for dangerous in ("=1+1", "+1", "-1", "@SUM(A1)", "\t=x"):
        assert safe_cell(dangerous).startswith("'")
    assert safe_cell("normal") == "normal" and safe_cell(None) == ""


async def test_empty_batch_and_permissions(services) -> None:
    with pytest.raises(NotFound):
        await services.payments.create_batch(PAYMENT_MOD)
    await approve_awards(services, [(10, "0.01", "x")])
    for actor in (MOD, HEAD_MOD):
        with pytest.raises(PermissionDenied):
            await services.payments.create_batch(actor)


async def test_concurrent_batch_creation_never_double_assigns(db_url, limits, clock) -> None:
    # Two separate Database objects = two independent connection pools/locks, like two
    # processes. Only SQLite's BEGIN IMMEDIATE stands between them.
    db_a, db_b = Database(db_url), Database(db_url)
    try:
        svc_a = build_services(db_a, limits, clock)
        svc_b = build_services(db_b, limits, clock)
        await approve_awards(svc_a, [(i, "0.01", f"r{i}") for i in range(1, 21)])
        results = await asyncio.gather(
            svc_a.payments.create_batch(PAYMENT_MOD),
            svc_b.payments.create_batch(PAYMENT_MOD_2),
            svc_a.payments.create_batch(OWNER),
            return_exceptions=True,
        )
        batches = [r for r in results if not isinstance(r, Exception)]
        errors = [r for r in results if isinstance(r, Exception)]
        assert len(batches) == 1, results
        assert all(isinstance(e, NotFound) for e in errors)
        async with db_a.transaction() as session:
            awards = list(await session.scalars(select(TipAward)))
        assert all(a.batch_id == batches[0].batch.id for a in awards)
        assert all(a.status == AwardStatus.QUEUED for a in awards)
    finally:
        await db_a.dispose()
        await db_b.dispose()


async def test_batch_cancellation_releases_only_unpaid(services) -> None:
    awards = await approve_awards(services, [(10, "0.01", "a"), (11, "0.02", "b")])
    detail = await services.payments.create_batch(PAYMENT_MOD)
    await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(10),
        amount_lamports=parse_sol("0.01"),
        signature=make_signature(1),
    )
    with pytest.raises(PermissionDenied):
        await services.payments.cancel_batch(MOD, detail.batch.id, "oops")
    result = await services.payments.cancel_batch(PAYMENT_MOD, detail.batch.id, "Wrong day")
    assert (result.released, result.retained) == (1, 1)
    assert result.batch.status == BatchStatus.CANCELLED
    released = await services.tips.get(HEAD_MOD, awards[1].id)
    assert released.status == AwardStatus.APPROVED and released.batch_id is None
    kept = await services.tips.get(HEAD_MOD, awards[0].id)
    assert kept.status == AwardStatus.PAYMENT_SUBMITTED and kept.batch_id == detail.batch.id
    with pytest.raises(Conflict):
        await services.payments.cancel_batch(PAYMENT_MOD, detail.batch.id, "again")

    # Released awards can go into a fresh batch; the cancelled batch stays in history.
    second = await services.payments.create_batch(PAYMENT_MOD)
    assert [line.award_id for line in second.lines] == [awards[1].id]
    history = await services.payments.list_batches(PAYMENT_MOD)
    assert [b.status for b in history.items] == [BatchStatus.OPEN, BatchStatus.CANCELLED]


async def test_payment_recording_and_confirmation(services) -> None:
    a1, a2, b1 = await approve_awards(
        services, [(10, "0.01", "a"), (10, "0.02", "b"), (11, "0.05", "c")]
    )
    detail = await services.payments.create_batch(PAYMENT_MOD)
    record = await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(10),
        amount_lamports=parse_sol("0.03"),
        signature=make_signature(1),
    )
    assert record.award_ids == [a1.id, a2.id] and not record.flagged
    assert record.payment.status == PaymentStatus.SUBMITTED
    assert (await services.tips.get(HEAD_MOD, a1.id)).status == AwardStatus.PAYMENT_SUBMITTED

    confirmed = await services.payments.confirm_payment(PAYMENT_MOD, record.payment.id)
    assert confirmed.payment.status == PaymentStatus.MANUALLY_CONFIRMED
    paid = await services.tips.get(HEAD_MOD, a1.id)
    assert paid.status == AwardStatus.PAID and paid.verification_source == "manual"
    assert paid.payment_signature == make_signature(1)
    with pytest.raises(Conflict):
        await services.payments.confirm_payment(PAYMENT_MOD, record.payment.id)

    # Batch completes only once the last wallet is settled.
    assert (await services.payments.get_batch(PAYMENT_MOD, detail.batch.id)).batch.status == "open"
    second = await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(11),
        amount_lamports=parse_sol("0.05"),
        signature=make_signature(2),
        award_ids=[b1.id],
    )
    await services.payments.confirm_payment(PAYMENT_MOD, second.payment.id)
    final = await services.payments.get_batch(PAYMENT_MOD, detail.batch.id)
    assert final.batch.status == BatchStatus.COMPLETED


async def test_duplicate_signature_and_double_settlement(services) -> None:
    a1, _ = await approve_awards(services, [(10, "0.01", "a"), (11, "0.01", "b")])
    detail = await services.payments.create_batch(PAYMENT_MOD)
    await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(10),
        amount_lamports=parse_sol("0.01"),
        signature=make_signature(1),
    )
    with pytest.raises(Conflict, match="already recorded"):
        await services.payments.record_payment(
            PAYMENT_MOD,
            batch_id=detail.batch.id,
            wallet=make_address(11),
            amount_lamports=parse_sol("0.01"),
            signature=make_signature(1),
        )
    with pytest.raises(Conflict, match="payment submitted"):
        await services.payments.record_payment(
            PAYMENT_MOD,
            batch_id=detail.batch.id,
            wallet=make_address(10),
            amount_lamports=parse_sol("0.01"),
            signature=make_signature(2),
            award_ids=[a1.id],
        )


async def test_payment_mismatch_is_rejected_or_flagged(services) -> None:
    a1, b1 = await approve_awards(services, [(10, "0.01", "a"), (11, "0.01", "b")])
    detail = await services.payments.create_batch(PAYMENT_MOD)
    with pytest.raises(InputError, match="Nothing was recorded"):
        await services.payments.record_payment(
            PAYMENT_MOD,
            batch_id=detail.batch.id,
            wallet=make_address(10),
            amount_lamports=parse_sol("0.02"),
            signature=make_signature(1),
        )
    with pytest.raises(InputError, match="payout wallet differs"):
        await services.payments.record_payment(
            PAYMENT_MOD,
            batch_id=detail.batch.id,
            wallet=make_address(99),
            amount_lamports=parse_sol("0.01"),
            signature=make_signature(1),
            award_ids=[b1.id],
        )
    assert (await services.tips.get(HEAD_MOD, a1.id)).status == AwardStatus.QUEUED

    flagged = await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(10),
        amount_lamports=parse_sol("0.02"),
        signature=make_signature(1),
        flag_mismatch=True,
    )
    assert flagged.flagged and flagged.payment.status == PaymentStatus.FLAGGED
    assert (await services.tips.get(HEAD_MOD, a1.id)).status == AwardStatus.DISPUTED
    with pytest.raises(Conflict, match="flagged"):
        await services.payments.confirm_payment(PAYMENT_MOD, flagged.payment.id)

    [dispute] = (await services.tips.list_disputes(HEAD_MOD)).items
    with pytest.raises(Exception, match="pay it twice"):
        await services.tips.resolve_dispute(HEAD_MOD, dispute.id, DisputeResolution.REOPENED, "x")
    await services.tips.resolve_dispute(
        HEAD_MOD, dispute.id, DisputeResolution.SETTLED, "Overpaid by 0.01; member agreed"
    )
    settled = await services.tips.get(HEAD_MOD, a1.id)
    assert (
        settled.status == AwardStatus.PAID and settled.verification_source == "dispute_resolution"
    )


async def test_unauthorized_payment_confirmation(services) -> None:
    await approve_awards(services, [(10, "0.01", "a")])
    detail = await services.payments.create_batch(PAYMENT_MOD)
    record = await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(10),
        amount_lamports=parse_sol("0.01"),
        signature=make_signature(1),
    )
    for actor in (MOD, HEAD_MOD, plain_member(10)):
        with pytest.raises(PermissionDenied):
            await services.payments.confirm_payment(actor, record.payment.id)
        with pytest.raises(PermissionDenied):
            await services.payments.record_payment(
                actor,
                batch_id=detail.batch.id,
                wallet=make_address(10),
                amount_lamports=1,
                signature=make_signature(3),
            )


async def test_two_person_rule(db, clock) -> None:
    services = build_services(db, Limits(require_separate_payment_confirmer=True), clock)
    await approve_awards(services, [(10, "0.01", "a")])
    detail = await services.payments.create_batch(PAYMENT_MOD)
    record = await services.payments.record_payment(
        PAYMENT_MOD,
        batch_id=detail.batch.id,
        wallet=make_address(10),
        amount_lamports=parse_sol("0.01"),
        signature=make_signature(1),
    )
    with pytest.raises(PermissionDenied, match="Two-person"):
        await services.payments.confirm_payment(PAYMENT_MOD, record.payment.id)
    await services.payments.confirm_payment(PAYMENT_MOD_2, record.payment.id)


async def test_invalid_payment_inputs(services) -> None:
    await approve_awards(services, [(10, "0.01", "a")])
    detail = await services.payments.create_batch(PAYMENT_MOD)
    base = {
        "batch_id": detail.batch.id,
        "wallet": make_address(10),
        "amount_lamports": 10_000_000,
        "signature": make_signature(1),
    }
    with pytest.raises(InputError):
        await services.payments.record_payment(PAYMENT_MOD, **{**base, "signature": "abc"})
    with pytest.raises(InputError):
        await services.payments.record_payment(PAYMENT_MOD, **{**base, "wallet": "nope"})
    with pytest.raises(InputError):
        await services.payments.record_payment(PAYMENT_MOD, **{**base, "amount_lamports": 0})
    with pytest.raises(NotFound):
        await services.payments.record_payment(PAYMENT_MOD, **{**base, "batch_id": 999})
    with pytest.raises(NotFound):
        await services.payments.record_payment(PAYMENT_MOD, **{**base, "award_ids": [999]})
