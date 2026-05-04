# Cargo Grid Model

The planner uses **slot-based** cargo layout, not capacity-based. Every cargo
bay is a 3D grid of 1.25 m unit cubes (the SCU base unit). Pallets occupy a
specific footprint of contiguous cells, and the assignment engine is a true
3D bin-packing problem on top of route ordering.

---

## Why slot-based

- Matches how real loading actually works in-game — a 32 SCU box can't go
  where two 16s fit even if total SCU is the same.
- Lets the UI show a real, unambiguous visual of where each pallet sits.
- Makes "unload zone" reasoning automatic: pallets nearest the ramp come off
  first.
- Makes overflow detection exact instead of approximate.

## Pallet footprints

Star Citizen's SCU box sizes follow the 1.25 m base cube. Footprints **to be
verified** from sources during the ship-data research pass:

| SCU | Footprint (W × D × H units) | Notes |
|-----|------------------------------|-------|
| 1   | 1 × 1 × 1                    | base cube |
| 2   | 2 × 1 × 1                    | rectangular |
| 4   | 2 × 2 × 1                    | flat square |
| 8   | 2 × 2 × 2                    | cube |
| 16  | 4 × 2 × 2                    | extends along width |
| 24  | 6 × 2 × 2                    | extends along width |
| 32  | 8 × 2 × 2                    | extends along width |

All confirmed. Key invariant: **no SCU box exceeds 2 deep or 2 tall.**
Beyond 8 SCU, growth is purely along the width axis. This dramatically
simplifies the bin-packing search space — height and depth are bounded
constants, only width orientation needs branching.

These values live in `data/scu_boxes.json` so the planner reads them at
startup rather than hardcoding.

## Ship grid representation

Each ship's `cargo_grid` is one or more **bays**. Each bay is a 3D array of
slots:

```
bay = grid[layer][row][column]
```

Each slot is either empty or holds a reference to the pallet occupying it.
Pallets larger than 1×1×1 own a contiguous block; the slot stores the same
pallet ID across the whole footprint.

Bay metadata also stores:
- `ramp_face` — which face of the bay the loading ramp is on (front / rear /
  side). Drives unload-order reasoning.
- `accessible_from` — which faces a forklift can reach. A pallet wedged in
  the back can't be unloaded until the ones in front of it are.

## Assignment algorithm

Two-stage:

1. **Route ordering** (existing problem) — decide stop order.
2. **Slot assignment** — for each pallet, in *reverse delivery order* (last
   stop loaded first, first stop loaded last and nearest the ramp), find a
   legal contiguous block of slots. Use a deterministic 3D first-fit
   decreasing pack so re-plans are stable across small contract changes.

When a pallet can't fit, the engine reports the failure with which bay was
exhausted and what footprint failed — no silent overflow.

## Zone semantics

The previous "F1/F2/F3 + R1-R4 zone" model is replaced by real ship layouts
from `data/ships.json`. The C2 Hercules, for example, is a single wide bay
roughly 4 columns across — not a tandem-zone arrangement. Per-stop "zones"
are now derived from slot positions relative to the ramp at runtime, not
hardcoded.

## Open questions

- 24 and 32 SCU box footprints — confirm during ship research pass.
- Do all ships use the same 1.25 m unit, or do some have non-standard cells?
- How do external grids (Hull series spar arms) differ from internal bays?
