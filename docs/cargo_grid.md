# Cargo Grid Model

**Status:** C2 Hercules is the only ship the planner currently supports.
The dataset and schema leave room to add other ships later.

The planner uses **slot-based** cargo layout: every bay is a 3D grid of
1.25 m unit cubes (the SCU base unit). Pallets occupy a contiguous block
of cells. The bay grid drives capacity, fit validation, and the visual
display.

---

## C2 Hercules — bay topology

The C2 has **two physical bays** stacked along the ship's length, on the
same deck:

| Bay | Dimensions (W × L × H, in 1.25 m cubes) | SCU |
|-----|------------------------------------------|-----|
| **Forward** (forward of the step) | 6 × 9 × 4 | 216 |
| **Rear** (aft, ramp end) | 8 × 15 × 4 | 480 |
| **Total** | | **696** |

The rear bay's loading face is the rear ramp. The forward bay sits
deeper in the ship and is reached by walking through the rear bay.

### Mental column dividers (zones)

Each bay is mentally divided into **2-cube-wide columns** for cockpit
clarity. There is **no physical barrier** between columns — a single
pallet can sit across two or more columns if it's wider than 2 cubes,
or if it's offset on the boundary.

**Forward bay — three columns** (each 2 × 9 × 4 = 72 SCU):

| Column | Cube-width range |
|--------|------------------|
| F1 | 0–1 (left edge) |
| F2 | 2–3 (middle) |
| F3 | 4–5 (right edge) |

**Rear bay — four columns** (each 2 × 15 × 4 = 120 SCU):

| Column | Cube-width range |
|--------|------------------|
| R1 | 0–1 (left edge) |
| R2 | 2–3 |
| R3 | 4–5 |
| R4 | 6–7 (right edge) |

*Left/right are described from aft looking forward.*

### Pallet placement and column spanning

Because columns are advisory, pallet placements that span columns are
legal:

- A 2 SCU pallet (2 × 1 × 1) placed on the F1–F2 boundary occupies
  the rightmost cube of F1 and the leftmost cube of F2.
- A 16 SCU pallet (4 × 2 × 2) takes 2 columns of width by definition;
  it might cover all of F1 + F2, or all of F2 + F3, or span a boundary.
- A 24 SCU pallet (6 × 2 × 2) takes the full 6-cube width of the
  forward bay, occupying F1 + F2 + F3 simultaneously.
- A 32 SCU pallet (8 × 2 × 2) only fits in the rear bay (forward bay
  is 6 wide), spanning R1 + R2 + R3 + R4.

The planner reports cargo placement using **the columns the pallet
touches**, e.g.:

- "F1 row 3" — pallet entirely within F1, third row from the ramp
- "F1–F2 row 3" — pallet spanning F1 and F2
- "F1–F3 row 3" — pallet spanning all three columns (24 SCU forward)

---

## Pallet footprints

Listed in **canonical orientation** (long axis along width). Pallets can
rotate 90° on the horizontal plane during loading; height never rotates.

| SCU | Canonical (W × L × H) | Rotated (W × L × H) | Notes |
|-----|------------------------|---------------------|-------|
| 1   | 1 × 1 × 1              | —                   | base cube |
| 2   | 2 × 1 × 1              | 1 × 2 × 1           | rotatable |
| 4   | 2 × 2 × 1              | —                   | square base |
| 8   | 2 × 2 × 2              | —                   | full cube |
| 16  | 4 × 2 × 2              | 2 × 4 × 2           | rotatable |
| 24  | 6 × 2 × 2              | 2 × 6 × 2           | rotatable |
| 32  | 8 × 2 × 2              | 2 × 8 × 2           | rotatable |

Key invariants:

- **Height never exceeds 2 cubes.** Vertical stacking is bounded.
- **At least two of three axes are ≤ 2 cubes for every pallet.**
- Beyond 8 SCU, growth is along a single horizontal axis (width *or*
  length depending on rotation). The packer branches on rotation only.

These values live in `data/scu_boxes.json` so the planner reads them at
startup rather than hardcoding.

---

## Assignment algorithm (overview)

Two stages:

1. **Route ordering** — decide the stop order for the active workday.
2. **Slot assignment** — for each pallet, in *reverse delivery order*
   (last stop loaded first, first stop loaded last and nearest the
   ramp), find a legal contiguous block of cells. Use a deterministic
   3D first-fit decreasing pack so re-plans are stable across small
   contract changes.

When a pallet can't fit, the engine reports the failure with which bay
was exhausted and what footprint failed — no silent overflow.

For the C2, the rear bay's outer columns (R1, R4) are nearest the ramp
edges, with R1 traditionally used for first-off cargo. The forward bay
is harder to access and is preferred for last-off / late-route cargo.
This ordering is a default — the planner reads it from the ship zone
metadata in the database, not hardcoded.

---

## Hidden pallet identity conflicts

When multiple destinations have **identical pallet sizes** loaded from
the same pickup, the pilot can't visually distinguish which stack
belongs to which destination once on the elevator. The planner detects
these conflicts and stages the pallets in **different cargo zones** so
the pilot can selectively bring up only one contract's pallets at a
time. See `conflicts.md` for the full algorithm and stop-handling
rules.

---

## Adding more ships later

`data/ships.json` already contains raw 1-SCU grid data for 88 ships.
When a second ship is brought online, the work is:

1. Define operational columns ("F1", "PORT", whatever fits the ship)
   in the `ship_zones` table.
2. Provide a top-down visual layout asset (or generate one from the
   grid).
3. Test palletization against real loads.

No code changes — the planner reads ship topology from the database.
