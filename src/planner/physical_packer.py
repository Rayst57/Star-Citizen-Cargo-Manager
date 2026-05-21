"""
Physical pallet packer — shared by the planner (placement validation)
and the BayCanvas (rendering).

Pallets are 1.25 m cubes per data/scu_boxes.json. Each pallet has a
footprint (width x length x height in cubes) and may rotate horizontally
if its metadata says so. A pack succeeds when every pallet finds a
uniform-support spot at a single height level, no two pallets overlap,
and no pallet exceeds the zone's height limit.

The planner uses `can_fit` and `best_pack` to verify a placement
decision is physically realizable before committing. The renderer uses
`best_pack` to position pallets on screen. Both go through the same
algorithm so the planner can never commit a layout the renderer can't
actually draw.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


LARGE_SIZES = frozenset({8, 16, 24, 32})


# ── Box catalog ──────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def box_footprints() -> dict[int, dict]:
    """Return {scu: {width, length, height, rotatable?}} from
    data/scu_boxes.json."""
    path = Path(__file__).resolve().parents[2] / "data" / "scu_boxes.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {b["scu"]: b for b in data["boxes"]}


def box_for(size: int) -> dict:
    return box_footprints().get(size, {"width": 1, "length": 1, "height": 1})


# ── Grid placement (best-fit, uniform support) ──────────────────────────

def place_in_grid(
    grid: list[list[int]],
    w: int, l: int, h: int,
    zone_w: int, zone_l: int,
    stack_limit: int,
    *,
    from_far_end: bool = False,
) -> tuple[int, int, int] | None:
    """Best-fit placement requiring uniform support.

    Returns (x, y, z) of the spot or None. Mutates *grid* in-place
    when a spot is found.
    """
    candidates: list[tuple[int, int, int]] = []
    for y in range(zone_l - l + 1):
        for x in range(zone_w - w + 1):
            heights = [
                grid[x + dx][y + dy]
                for dx in range(w)
                for dy in range(l)
            ]
            if len(set(heights)) != 1:
                continue
            z = heights[0]
            if z + h > stack_limit:
                continue
            candidates.append((y, x, z))
    if not candidates:
        return None
    if from_far_end:
        candidates.sort(key=lambda c: (-c[0], -c[2], c[1]))
    else:
        candidates.sort(key=lambda c: (c[0], c[2], c[1]))
    y, x, z = candidates[0]
    for dx in range(w):
        for dy in range(l):
            grid[x + dx][y + dy] = z + h
    return (x, y, z)


# ── Single-pass pack ────────────────────────────────────────────────────

def _try_place(
    grid: list[list[int]],
    size: int,
    zone_w: int, zone_l: int, zone_h: int,
    *,
    from_far: bool,
) -> tuple[int, int, int, int, int, int] | None:
    """Place a single pallet, returning (w, l, h, x, y, z) or None."""
    box = box_for(size)
    w, l, h = box["width"], box["length"], box["height"]
    if w > zone_w and box.get("rotatable") and l <= zone_w:
        w, l = l, w
    spot = place_in_grid(
        grid, w, l, h, zone_w, zone_l, zone_h, from_far_end=from_far,
    )
    if spot is None:
        return None
    x, y, z = spot
    return (w, l, h, x, y, z)


def try_pack_one_pass(
    zone_w: int, zone_l: int, zone_h: int,
    keyed_large: list[tuple[Any, int]],
    keyed_small: list[tuple[Any, int]],
    *,
    smalls_first: bool = False,
) -> tuple[list[tuple], list[tuple[Any, int]]]:
    """Run one packing pass.

    Returns (placements, overflow). Each placement is a tuple
    (key, size, w, l, h, x, y, z). Overflow is the list of
    (key, size) pairs that couldn't be placed.

    Pass ordering: large pallets always pack from the far end; small
    pallets from the ramp end. `smalls_first=True` packs the small
    pallets before the large ones, useful for awkward zone lengths
    where smalls at the ramp open up clean rows for larges from the
    far end.
    """
    grid = [[0] * zone_l for _ in range(zone_w)]
    placements: list = []
    overflow: list[tuple[Any, int]] = []

    batches = (
        ((keyed_small, False), (keyed_large, True))
        if smalls_first else
        ((keyed_large, True), (keyed_small, False))
    )
    for batch, from_far in batches:
        for key, size in batch:
            rec = _try_place(
                grid, size, zone_w, zone_l, zone_h, from_far=from_far,
            )
            if rec is None:
                overflow.append((key, size))
            else:
                w, l, h, x, y, z = rec
                placements.append((key, size, w, l, h, x, y, z))
    return placements, overflow


def best_pack(
    zone_w: int, zone_l: int, zone_h: int,
    keyed_pallets: list[tuple[Any, int]],
) -> tuple[list[tuple], list[tuple[Any, int]]]:
    """Pack *keyed_pallets* using the best of four orderings:
       (large-first vs smalls-first) x (smalls ASC vs DESC).

    Returns the (placements, overflow) of the attempt with the
    fewest overflows; ties broken by most placements.
    """
    keyed_large = sorted(
        [p for p in keyed_pallets if p[1] in LARGE_SIZES],
        key=lambda p: -p[1],
    )
    small_pool = [p for p in keyed_pallets if p[1] not in LARGE_SIZES]
    small_asc = sorted(small_pool, key=lambda p: p[1])
    small_desc = sorted(small_pool, key=lambda p: -p[1])

    attempts = [
        try_pack_one_pass(
            zone_w, zone_l, zone_h, keyed_large, small_asc),
        try_pack_one_pass(
            zone_w, zone_l, zone_h, keyed_large, small_desc),
        try_pack_one_pass(
            zone_w, zone_l, zone_h, keyed_large, small_asc,
            smalls_first=True),
        try_pack_one_pass(
            zone_w, zone_l, zone_h, keyed_large, small_desc,
            smalls_first=True),
    ]
    attempts.sort(key=lambda r: (len(r[1]), -len(r[0])))
    return attempts[0]


# ── Convenience predicates for the planner ──────────────────────────────

def can_fit(
    zone_w: int, zone_l: int, zone_h: int,
    pallet_sizes: list[int],
) -> bool:
    """True iff every pallet size can be packed simultaneously into
    the zone footprint. Pure size list — keys are unused."""
    if not pallet_sizes:
        return True
    keyed = [(None, s) for s in pallet_sizes]
    _, overflow = best_pack(zone_w, zone_l, zone_h, keyed)
    return not overflow


def max_fitting_subset(
    zone_w: int, zone_l: int, zone_h: int,
    existing_sizes: list[int],
    candidate_sizes: list[int],
) -> tuple[list[int], list[int]]:
    """Of *candidate_sizes*, return the largest subset that can be
    packed on top of *existing_sizes* in the given zone. Returns
    (fitting_sizes, overflow_sizes) — fitting + overflow always
    equals candidate_sizes as a multiset.

    Used by the planner's split path: when a whole cargo line can't
    fit, ask "how many pallets CAN we cram into this zone before we
    move on?" Largest-pallet-first to maximize SCU per placement.
    """
    if not candidate_sizes:
        return [], []

    # The planner tracks pallets by INDEX so duplicates are preserved.
    fitting_indices: set[int] = set()
    indexed = list(enumerate(candidate_sizes))
    indexed.sort(key=lambda iv: -iv[1])  # largest first

    placed_so_far: list[int] = []
    for idx, size in indexed:
        trial = existing_sizes + placed_so_far + [size]
        if can_fit(zone_w, zone_l, zone_h, trial):
            placed_so_far.append(size)
            fitting_indices.add(idx)

    fitting = [s for i, s in enumerate(candidate_sizes) if i in fitting_indices]
    overflow = [s for i, s in enumerate(candidate_sizes) if i not in fitting_indices]
    return fitting, overflow
