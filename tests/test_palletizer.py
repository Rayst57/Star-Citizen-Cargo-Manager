"""Unit tests for the palletizer module.

Handbook §8.1 examples are the primary golden tests.
"""

import pytest
from src.planner.palletizer import palletize, palletize_summary, VALID_SIZES


# ── Handbook §8.1 golden examples ────────────────────────────────────────

def test_55_scu_max_8():
    result = palletize(55, 8)
    assert result == [8, 8, 8, 8, 8, 8, 4, 2, 1]
    assert sum(result) == 55


def test_27_scu_max_8():
    result = palletize(27, 8)
    assert result == [8, 8, 8, 2, 1]
    assert sum(result) == 27


def test_45_scu_max_16():
    result = palletize(45, 16)
    assert result == [16, 16, 8, 4, 1]
    assert sum(result) == 45


# ── Edge cases ────────────────────────────────────────────────────────────

def test_1_scu_max_1():
    assert palletize(1, 1) == [1]


def test_1_scu_max_32():
    assert palletize(1, 32) == [1]


def test_32_scu_max_32():
    assert palletize(32, 32) == [32]


def test_32_scu_max_8():
    result = palletize(32, 8)
    assert sum(result) == 32
    assert all(s <= 8 for s in result)


def test_exact_multiple_of_max():
    # 24 SCU at max 8 → exactly 3 × 8
    result = palletize(24, 8)
    assert result == [8, 8, 8]


def test_max_clamp_to_valid_size():
    # max_pallet_size=7 → nearest valid ≤ 7 is 4
    result = palletize(10, 7)
    assert all(s <= 4 for s in result)
    assert sum(result) == 10


def test_large_cargo_max_32():
    result = palletize(100, 32)
    assert sum(result) == 100
    assert max(result) == 32
    # Should use 3×32 = 96, then 4 remaining
    assert result[:3] == [32, 32, 32]
    assert sum(result[3:]) == 4


@pytest.mark.parametrize("scu,max_size", [
    (s, s) for s in VALID_SIZES
])
def test_single_pallet_exact_size(scu, max_size):
    """A cargo equal to one pallet size should return exactly one pallet."""
    result = palletize(scu, max_size)
    assert result == [scu]
    assert sum(result) == scu


# ── Input validation ──────────────────────────────────────────────────────

def test_zero_scu_raises():
    with pytest.raises(ValueError, match="scu must be > 0"):
        palletize(0, 8)


def test_negative_scu_raises():
    with pytest.raises(ValueError):
        palletize(-5, 8)


def test_max_below_minimum_raises():
    with pytest.raises(ValueError, match="below the minimum"):
        palletize(5, 0)


# ── palletize_summary ────────────────────────────────────────────────────

def test_summary_format_handbook_example():
    pallets = palletize(55, 8)
    assert palletize_summary(pallets) == "6×8 + 1×4 + 1×2 + 1×1"


def test_summary_single_pallet():
    assert palletize_summary([32]) == "1×32"


def test_summary_empty():
    assert palletize_summary([]) == "0 pallets"


def test_summary_all_same():
    assert palletize_summary([8, 8, 8]) == "3×8"
