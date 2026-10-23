"""Input validation for Solana identifiers and free-text fields."""

from __future__ import annotations

import unicodedata

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {char: index for index, char in enumerate(BASE58_ALPHABET)}

SOLANA_PUBKEY_BYTES = 32
SOLANA_SIGNATURE_BYTES = 64


class ValidationError(ValueError):
    """Raised when user input fails validation; the message is safe to show."""


def b58decode(value: str) -> bytes:
    """Decode a Bitcoin-alphabet Base58 string (the encoding Solana uses)."""
    if not value:
        raise ValidationError("Value is empty.")
    number = 0
    for char in value:
        digit = _BASE58_INDEX.get(char)
        if digit is None:
            raise ValidationError(f"`{char}` is not a valid Base58 character.")
        number = number * 58 + digit
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeros + body


def validate_solana_address(value: str) -> str:
    """Return the address if it is a Base58 string decoding to exactly 32 bytes.

    The key is deliberately *not* required to be on the ed25519 curve: program-derived
    addresses are valid recipients. This proves format only, never ownership.
    """
    address = (value or "").strip()
    if len(address.split()) >= 12:
        raise ValidationError(
            "🚨 That looks like a **seed phrase**. Never share it with anyone - not staff, not "
            "bots. Anyone with it can empty your wallet; consider moving your funds to a fresh "
            "wallet. Send only your **public** wallet address."
        )
    if (
        80 <= len(address) <= 90
        and all(c in _BASE58_INDEX for c in address)
        and len(b58decode(address)) == 64
    ):
        raise ValidationError(
            "🚨 That looks like a **private key**, not a public address. Never share it - "
            "consider moving your funds to a fresh wallet. Send only your **public** address."
        )
    if not 32 <= len(address) <= 44:
        raise ValidationError(
            "That doesn't look like a Solana wallet address (expected 32-44 Base58 characters)."
        )
    decoded = b58decode(address)
    if len(decoded) != SOLANA_PUBKEY_BYTES:
        raise ValidationError(
            f"That address decodes to {len(decoded)} bytes; Solana public keys are 32 bytes."
        )
    return address


def validate_transaction_signature(value: str) -> str:
    signature = (value or "").strip()
    if not 64 <= len(signature) <= 88:
        raise ValidationError("A Solana transaction signature is 64-88 Base58 characters.")
    decoded = b58decode(signature)
    if len(decoded) != SOLANA_SIGNATURE_BYTES:
        raise ValidationError(
            f"That signature decodes to {len(decoded)} bytes; Solana signatures are 64 bytes."
        )
    return signature


def clean_text(
    value: str | None, *, field: str, max_length: int = 300, required: bool = True
) -> str:
    """Strip control characters and enforce a length limit on free text (reasons, notes)."""
    text = "".join(
        ch for ch in (value or "") if ch in "\n " or unicodedata.category(ch)[0] != "C"
    ).strip()
    if required and not text:
        raise ValidationError(f"{field} is required.")
    if len(text) > max_length:
        raise ValidationError(f"{field} must be at most {max_length} characters.")
    return text


def parse_id_list(value: str | None, *, field: str = "IDs", max_items: int = 200) -> list[int]:
    """Parse "12, 13 14" into [12, 13, 14] (deduplicated, order preserved)."""
    if not value or not value.strip():
        return []
    ids: list[int] = []
    for token in value.replace(",", " ").split():
        token = token.lstrip("#")
        if not (token.isascii() and token.isdigit()) or int(token) <= 0:
            raise ValidationError(f"`{token[:20]}` is not a valid ID in {field}.")
        number = int(token)
        if number not in ids:
            ids.append(number)
    if len(ids) > max_items:
        raise ValidationError(f"At most {max_items} {field} can be given at once.")
    return ids


def shorten_wallet(address: str | None) -> str:
    if not address:
        return "—"
    return f"{address[:4]}…{address[-4:]}" if len(address) > 12 else address
