from decimal import Decimal

import pytest

from app.accounting.money import from_micro, mul_micro, pro_rata, to_micro


def test_roundtrip():
    assert to_micro("437.10") == 437_100_000
    assert from_micro(437_100_000) == Decimal("437.10")
    assert to_micro(13) == 13_000_000


def test_floats_refused():
    with pytest.raises(TypeError):
        to_micro(0.1)


def test_mul_micro():
    # 12 shares × $240 = $2880
    assert mul_micro(12_000_000, 240_000_000) == 2_880_000_000
    # $259 = 350 SGD × 0.74
    assert mul_micro(350_000_000, 740_000) == 259_000_000


def test_pro_rata_conserves_nothing_lost():
    total = to_micro("1.50")
    a = pro_rata(total, 10, 12)
    b = pro_rata(total, 2, 12)
    assert a + b == total  # 1.25 + 0.25
