from __future__ import annotations

import pytest
from sqlalchemy import select, text, update

from database.models import AwardStatus, TipAward, Wallet, WalletHistory
from services.errors import Conflict, InputError, NotFound
from tests.conftest import HEAD_MOD, MOD, make_address, make_signature, member
from utils.validation import ValidationError, b58decode, validate_solana_address

# Real, well-known Solana addresses.
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class TestAddressValidation:
    def test_valid_public_keys(self) -> None:
        assert validate_solana_address(TOKEN_PROGRAM) == TOKEN_PROGRAM
        assert validate_solana_address(f"  {SYSTEM_PROGRAM} ") == SYSTEM_PROGRAM
        assert len(b58decode(SYSTEM_PROGRAM)) == 32

    def test_off_curve_address_is_accepted(self) -> None:
        # The System Program ID (all zero bytes) is not a valid ed25519 point but is a valid key.
        assert validate_solana_address(SYSTEM_PROGRAM)

    @pytest.mark.parametrize(
        "value",
        [
            "0OIl" + TOKEN_PROGRAM[4:],  # characters outside the Base58 alphabet
            TOKEN_PROGRAM[:-1] + "0",
            "not a wallet",
        ],
    )
    def test_invalid_base58(self, value: str) -> None:
        with pytest.raises(ValidationError):
            validate_solana_address(value)

    @pytest.mark.parametrize("value", ["1" * 31, "z" * 44, "2" * 33, "", "1" * 45])
    def test_wrong_decoded_length(self, value: str) -> None:
        with pytest.raises(ValidationError):
            validate_solana_address(value)


async def test_register_wallet(services) -> None:
    address = make_address(1)
    change = await services.wallets.set_wallet(member(10), address)
    assert change.address == address and change.previous_address is None
    wallet = await services.wallets.get_wallet(10)
    assert wallet is not None and wallet.address == address and wallet.is_active


async def test_rejects_invalid_and_duplicate_registration(services) -> None:
    with pytest.raises(InputError):
        await services.wallets.set_wallet(member(10), "definitely-not-base58!")
    await services.wallets.set_wallet(member(10), make_address(1))
    with pytest.raises(Conflict):
        await services.wallets.set_wallet(member(10), make_address(1))


async def test_wallet_replacement_and_history(services, db) -> None:
    first, second = make_address(1), make_address(2)
    await services.wallets.set_wallet(member(10), first)
    change = await services.wallets.set_wallet(member(10), second)
    assert change.previous_address == first

    async with db.transaction() as session:
        wallets = list(await session.scalars(select(Wallet).order_by(Wallet.id)))
        history = list(await session.scalars(select(WalletHistory).order_by(WalletHistory.id)))
    assert [w.is_active for w in wallets] == [False, True]
    assert [(h.action, h.old_address, h.new_address) for h in history] == [
        ("registered", None, first),
        ("changed", first, second),
    ]

    removal = await services.wallets.remove_wallet(member(10))
    assert removal.address == second
    assert await services.wallets.get_wallet(10) is None
    with pytest.raises(NotFound):
        await services.wallets.remove_wallet(member(10))
    assert (await services.wallets.history(10))[0].action == "removed"


async def test_database_enforces_one_active_wallet(services, db) -> None:
    await services.wallets.set_wallet(member(10), make_address(1))
    wallet = await services.wallets.get_wallet(10)
    with pytest.raises(Exception, match="UNIQUE"):
        async with db.transaction() as session:
            session.add(
                Wallet(
                    user_id=wallet.user_id,
                    address=make_address(2),
                    is_active=True,
                    registered_at=wallet.registered_at,
                )
            )


async def test_registering_wallet_promotes_awaiting_awards(services) -> None:
    award = await services.tips.add_manual(MOD, member(10), 5_000_000, "Great clip")
    assert award.status == AwardStatus.AWAITING_WALLET
    change = await services.wallets.set_wallet(member(10), make_address(1))
    assert change.awards_now_reviewable == 1
    assert (await services.tips.get(HEAD_MOD, award.id)).status == AwardStatus.PENDING_REVIEW

    removal = await services.wallets.remove_wallet(member(10))
    assert removal.awards_back_to_awaiting_wallet == 1
    assert (await services.tips.get(HEAD_MOD, award.id)).status == AwardStatus.AWAITING_WALLET


async def test_payout_snapshot_survives_wallet_change(services, db) -> None:
    old, new = make_address(1), make_address(2)
    await services.wallets.set_wallet(member(10), old)
    award = await services.tips.add_manual(MOD, member(10), 5_000_000, "Stream call win")
    await services.tips.approve(HEAD_MOD, award.id)

    change = await services.wallets.set_wallet(member(10), new)
    assert change.awards_locked_to_previous == 1
    row = await services.tips.get(HEAD_MOD, award.id)
    assert row.status == AwardStatus.APPROVED and row.payout_wallet == old

    # The database itself refuses to silently redirect an approved payout.
    with pytest.raises(Exception, match="snapshot is immutable"):
        async with db.transaction() as session:
            await session.execute(
                update(TipAward).where(TipAward.id == award.id).values(payout_wallet=new)
            )

    # The explicit moderator review path clears the snapshot; re-approval takes the new wallet.
    await services.tips.return_to_review(HEAD_MOD, award.id, "Member changed wallet")
    approved = await services.tips.approve(HEAD_MOD, award.id)
    assert approved.payout_wallet == new


async def test_audit_rows_are_immutable(services, db) -> None:
    await services.wallets.set_wallet(member(10), make_address(1))
    with pytest.raises(Exception, match="append-only"):
        async with db.transaction() as session:
            await session.execute(text("UPDATE audit_logs SET action = 'tampered'"))
    with pytest.raises(Exception, match="append-only"):
        async with db.transaction() as session:
            await session.execute(text("DELETE FROM audit_logs"))


def test_refuses_seed_phrases_and_private_keys() -> None:
    # Any 64-byte Base58 value has the shape of a secret key.
    with pytest.raises(ValidationError, match="seed phrase"):
        validate_solana_address(" ".join(["abandon"] * 11 + ["about"]))
    with pytest.raises(ValidationError, match="private key"):
        validate_solana_address(make_signature(5))
