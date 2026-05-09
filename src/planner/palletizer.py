"""
Palletizer — largest-first breakdown of an SCU amount into legal pallet sizes.

Handbook §8: Use largest pallet ≤ contract max_pallet_size and ≤ remaining SCU;
repeat until SCU = 0.  Valid sizes: 32, 24, 16, 8, 4, 2, 1.
"""

from __future__ import annotations

VALID_SIZES: list[int] = [32, 24, 16, 8, 4, 2, 1]


def palletize(scu: int, max_pallet_size: int) -> list[int]:
    """Return a largest-first list of pallet sizes that sum to *scu*.

    Args:
        scu:             Total SCU to break down (must be > 0).
        max_pallet_size: Upper bound from the contract.  Clamped to the
                         nearest valid size ≤ max_pallet_size.

    Returns:
        Ordered list of pallet sizes (descending).

    Raises:
        ValueError: If scu <= 0 or max_pallet_size is not a valid size.

    Examples (from handbook §8.1):
        palletize(55, 8)  -> [8,8,8,8,8,8,4,2,1]   (6×8 + 1×4 + 1×2 + 1×1)
        palletize(27, 8)  -> [8,8,8,2,1]            (3×8 + 1×2 + 1×1)
        palletize(45, 16) -> [16,16,8,4,1]           (2×16 + 1×8 + 1×4 + 1×1)
    """
    if scu <= 0:
        raise ValueError(f"scu must be > 0, got {scu}")

    # Clamp to the largest valid size that does not exceed max_pallet_size.
    allowed = [s for s in VALID_SIZES if s <= max_pallet_size]
    if not allowed:
        raise ValueError(
            f"max_pallet_size={max_pallet_size} is below the minimum pallet size (1)"
        )

    result: list[int] = []
    remaining = scu
    for size in allowed:
        while remaining >= size:
            result.append(size)
            remaining -= size

    return result


def palletize_summary(pallets: list[int]) -> str:
    """Human-readable pallet summary, e.g. '6×8 + 1×4 + 1×2 + 1×1'."""
    if not pallets:
        return "0 pallets"
    from collections import Counter

    counts = Counter(pallets)
    parts = [f"{counts[s]}×{s}" for s in VALID_SIZES if s in counts]
    return " + ".join(parts)


def pallet_sizes_set(pallets: list[int]) -> set[int]:
    """Unique set of pallet sizes present in a breakdown."""
    return set(pallets)
