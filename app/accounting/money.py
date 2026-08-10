"""Micro-unit money arithmetic (DESIGN.md §7).

Everything in the ledger is an int count of micro-units: 1 share or $1 (or
S$1, ¥1, …) is 1_000_000 units, and fx_rate is the rate ×1e6. Decimal exists
only at this boundary; ints everywhere below it.
"""
from decimal import Decimal, ROUND_HALF_EVEN

MICRO = 1_000_000
_Q = Decimal(1)


def to_micro(value) -> int:
    """Decimal/str/int units → micro int. Floats are refused: they are the bug
    this module exists to prevent."""
    if isinstance(value, float):
        raise TypeError(f"float {value!r} refused — pass Decimal, str, or int")
    d = (Decimal(value) * MICRO).quantize(_Q, rounding=ROUND_HALF_EVEN)
    return int(d)


def from_micro(n: int) -> Decimal:
    return Decimal(n) / MICRO


def mul_micro(a: int, b: int) -> int:
    """(a × b) where both are micro-scaled, result micro-scaled.
    E.g. qty × price → notional; amount × fx_rate → converted amount."""
    return int((Decimal(a) * Decimal(b) / MICRO).quantize(_Q, rounding=ROUND_HALF_EVEN))


def pro_rata(total: int, part: int, whole: int) -> int:
    """part/whole share of total, banker's-rounded to a micro."""
    if whole == 0:
        return 0
    return int((Decimal(total) * Decimal(part) / Decimal(whole))
               .quantize(_Q, rounding=ROUND_HALF_EVEN))
