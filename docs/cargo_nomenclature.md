# Cargo Position Nomenclature

Simple, ship-specific pallet position labels. Two label styles depending
on what fits the ship:

1. **Small ships (1 SCU pallets only)** — Lateral letter + sequence number.
2. **Larger ships (8 SCU pallets)** — Section letter + column letter + row
   number, with each ship defining its own position grid.

The underlying 1-SCU grid in `data/ships.json` is preserved for capacity
math. This document defines the *display* labels used in the UI legend,
on the illustration window, and in voice readback.

---

## Style 1 — Small ships (1 SCU only)

Pallet positions arranged on a single deck. Label each position with a
**lateral letter** and **sequence number from the loading face**.

| Letter | Meaning |
|--------|---------|
| `L` | Left |
| `C` | Center (only when there's a true center column) |
| `R` | Right |

Number 1 starts at the loading face (ramp / cargo door). 2, 3, 4 progress
inward.

### Examples

- A 4-position skiff with two rows: `L1, L2, R1, R2`
- A 6-position bay with three columns: `L1, C1, R1, L2, C2, R2`
- A single-file bay: `C1, C2, C3` (or `1, 2, 3` if center is implied)

Applies to: MPUV-Cargo, Cutter, Mustang Alpha, Pisces C8X, Nomad, ROC,
Avenger Titan, and similar small haulers.

---

## Style 2 — Larger ships (8 SCU pallets)

Larger ships are gridded **by 8 SCU pallet footprints** (each occupying
a 2 × 2 × 2 block of the underlying 1 SCU cells). Each ship defines its
own position grid because layouts vary too much for one universal scheme.

### Address format

`[Section][Column][Row]`

- **Section** — single letter for the part of the ship the position is in.
  Omitted on ships with only one section.
- **Column** — letter starting at `A`, port → starboard.
- **Row** — number starting at `1`, from the loading face inward.

### Example: C1 Spirit

C1 has 2 columns × 4 rows on a single section.

```
       Loading face (rear ramp)
       ┌────┬────┐
   1   │ L1 │ R1 │
       ├────┼────┤
   2   │ L2 │ R2 │
       ├────┼────┤
   3   │ L3 │ R3 │
       ├────┼────┤
   4   │ L4 │ R4 │
       └────┴────┘
         L    R
```

Single-section ship → use lateral letters `L`/`R` directly instead of
`A`/`B`. Cleaner to read aloud.

### Example: C2 Hercules

C2 has a fore deck and an aft deck (the stepped bay). Each deck has its
own column-letter grid.

```
       Loading face (rear ramp)
       ┌──────────────┐
       │   AFT DECK   │
       │ A  B  C  D   │
   1   │ AA1 AB1 AC1 AD1
   2   │ AA2 AB2 AC2 AD2
   ...                ┘
       ┌──────────────────┐
       │   FORE DECK      │
       │ A  B  C  D       │
   1   │ FA1 FB1 FC1 FD1
   ...
```

- `AA1` = **A**ft deck, column **A**, row **1**
- `FA1` = **F**ore deck, column **A**, row **1**
- `FB3` = **F**ore deck, column **B**, row **3**

Two tiers stacked? Append a tier suffix only when more than one tier
exists: `FA1` (deck level) and `FA1-T2` (above it). On ships with a
single tier, no suffix.

### Section letter conventions

Used only when a ship has multiple sections.

| Letter | Meaning | Used on |
|--------|---------|---------|
| `F` | Forward / Fore deck | C2, M2, A2 Hercules; multi-section haulers |
| `A` | Aft deck | C2, M2, A2 Hercules |
| `M` | Mid / Middle | Carrack, Polaris |
| `N` | Nose | Caterpillar, Cutlass Black nose |
| `1`–`4` | Numbered modules | Caterpillar (`MOD1` → `1`-prefix) |
| `P` | Port-side hold | Polaris, Hull series |
| `S` | Starboard-side hold | Polaris, Hull series |
| `U` | Upper deck | Galaxy |
| `L` | Lower deck | Galaxy |
| `R` | Rack (Hull series) | `R1A1` = Rack 1, column A, row 1 |

### Section letter conflict resolution

The lateral letters `L`/`R` are also section letters (Lower / Rack) on
some ships. To avoid ambiguity:

- A ship uses **either** lateral L/R style (Style 1 or single-section
  Style 2) **or** section L/R, never both.
- On multi-section ships using Style 2, columns always start at `A` (no
  L/R lateral shorthand).

So the C2 fore deck has columns `A, B, C, D` not `L, ML, MR, R`.

---

## Per-ship position definitions

`data/ships.json` already stores the raw 1-SCU grid. We'll add a
sibling field, `pallet_positions`, listing each 8-SCU position with
its label and which underlying cells it occupies.

```json
"pallet_positions": [
  { "label": "FA1", "section": "F", "column": "A", "row": 1, "tier": 1,
    "cells": [[0,0,0],[1,0,0],[0,1,0],[1,1,0],[0,0,1],[1,0,1],[0,1,1],[1,1,1]] },
  { "label": "FB1", "section": "F", "column": "B", "row": 1, "tier": 1,
    "cells": [...] }
]
```

Cells are referenced as `[col, row, layer]` indices into the underlying
1-SCU grid. This lets the planner work in 8-SCU units for placement but
still validate against the real bay shape (and handle smaller pallets
that occupy a fraction of a position).

### Smaller pallets in 8-SCU positions

When a contract is for a 4 SCU pallet (2×2×1) or smaller, the planner
splits an 8 SCU position into its constituent 1-SCU cells and places
the pallet inside. The position label stays — a 4 SCU pallet at `FA1`
just occupies the lower half. The illustration shows the partial fill.

---

## Loading face

Each ship records its loading face (rear ramp, side door, top, etc.) so
that "row 1 = nearest the loading face" is unambiguous.

```json
{ "loading_face": "stern" }
```

---

## Open questions

1. **Tier suffix style** — `FA1-T2` vs `FA1U` (upper) vs `FA1.2`?
2. **Skiff numbering direction** — confirm "1 = nearest loading face" not
   "1 = nose-most"?
3. **Caterpillar modules** — number-prefixed (`1A1`, `2A1`) clean, or do
   you want `MOD1-A1`?
