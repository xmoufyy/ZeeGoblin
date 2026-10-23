"""Exact SOL <-> lamport conversion.

Every amount is stored as an integer number of lamports (1 SOL = 1,000,000,000 lamports).
Parsing and formatting go through :class:`decimal.Decimal`; floats are never used.
"""

from __future__ import annotations

import re
from decimal import Decimal, localcontext

LAMPORTS_PER_SOL = 1_000_000_000
SOL_DECIMALS = 9

# Plain non-negative decimal: "1", "0.01", ".5". Integer part capped at 12 digits so a typo
# like "10000000000000000000" is rejected instead of silently producing a giant obligation.
_SOL_PATTERN = re.compile(r"^(?:[0-9]{1,12}(?:\.[0-9]+)?|\.[0-9]+)$")


class AmountError(ValueError):
    """Raised when user-supplied SOL text is not an acceptable amount."""


def parse_sol(text: str) -> int:
    """Convert a SOL amount such as ``"0.01"`` to integer lamports.

    Rejects zero, negatives, exponents, thousands separators, more than nine decimal places
    and anything else ambiguous. An optional trailing ``SOL`` unit is tolerated.
    """
    if not isinstance(text, str):
        raise AmountError("Amounts must be given as text, e.g. 0.01")
    cleaned = text.strip()
    if cleaned.lower().endswith("sol"):
        cleaned = cleaned[:-3].strip()
    if not cleaned:
        raise AmountError("Enter an amount in SOL, e.g. 0.01")
    if not _SOL_PATTERN.fullmatch(cleaned):
        raise AmountError(
            f"`{text.strip()[:32]}` is not a valid SOL amount. Use plain digits like 0.01 "
            "(no commas, signs or exponents)."
        )
    if "." in cleaned and len(cleaned.split(".", 1)[1]) > SOL_DECIMALS:
        raise AmountError("SOL amounts can have at most 9 decimal places (1 lamport).")

    with localcontext() as ctx:
        ctx.prec = 40
        lamports = Decimal(cleaned) * LAMPORTS_PER_SOL
        if lamports != lamports.to_integral_value():
            raise AmountError("SOL amounts can have at most 9 decimal places (1 lamport).")
    value = int(lamports)
    if value <= 0:
        raise AmountError("Amount must be greater than zero.")
    return value


def lamports_to_decimal(lamports: int) -> Decimal:
    return Decimal(int(lamports)).scaleb(-SOL_DECIMALS)


def format_sol(lamports: int, *, fixed: bool = False) -> str:
    """Render lamports as SOL text. ``fixed=True`` always shows nine decimals (for exports)."""
    text = f"{lamports_to_decimal(lamports):.{SOL_DECIMALS}f}"
    if fixed:
        return text
    trimmed = text.rstrip("0").rstrip(".")
    return trimmed or "0"


def ensure_within(lamports: int, maximum: int, label: str) -> None:
    if lamports > maximum:
        raise AmountError(
            f"{label} of {format_sol(lamports)} SOL exceeds the limit of {format_sol(maximum)} SOL."
        )
