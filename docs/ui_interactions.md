# UI Interactions

How the Windows desktop app surfaces and accepts user input, beyond
voice. Voice and manual paths produce identical state changes — see
`manual_entry.md` for input forms, `giant_commands.md` for voice.

---

## Main window layout (active workday)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Star Citizen Cargo Manager                                  ─ ☐ ✕  │
├──────────────────────────────────────────────────────────────────────┤
│ ┌──────────────┐ ┌─────────────────────────────┐ ┌───────────────┐ │
│ │ CONTRACTS    │ │       C2 HERCULES BAY       │ │  ROUTE        │ │
│ │ + Add        │ │                             │ │  ↻ Recompute  │ │
│ │              │ │   (top-down zone view)      │ │  (scrollable) │ │
│ │ #1 Seraphim  │ │                             │ │               │ │
│ │   → 5 SCU…   │ │   F1 [▼ dest]   F2  F3      │ │  Stop 1: …    │ │
│ │ #2 Yellow    │ │   ┌──┐┌──┐┌──┐              │ │   View Load → │ │
│ │   → 27 SCU…  │ │   │  ││  ││  │              │ │  Stop 2: …    │ │
│ │  …           │ │   └──┘└──┘└──┘              │ │   View Load → │ │
│ │              │ │                             │ │  …            │ │
│ │              │ │   R1  R2  R3  R4            │ │               │ │
│ │              │ │   ┌──┐┌──┐┌──┐┌──┐          │ │               │ │
│ │              │ │   │  ││  ││  ││  │          │ │               │ │
│ │              │ │   └──┘└──┘└──┘└──┘          │ │               │ │
│ │              │ │                             │ │               │ │
│ │              │ │   357 / 696 SCU             │ │               │ │
│ └──────────────┘ └─────────────────────────────┘ └───────────────┘ │
│ ┌──────────────────────────────────────────────────────────────────┐│
│ │ ⚠  Modifications made — recompute required           [Recompute] ││
│ └──────────────────────────────────────────────────────────────────┘│
├──────────────────────────────────────────────────────────────────────┤
│ Status bar: 🎤 listening · Stop 3 of 11 · 357/696 SCU · ⚙ Settings  │
└──────────────────────────────────────────────────────────────────────┘
```

Three primary panes:

1. **Contracts** (left) — list of active contracts, with destinations
   and SCU. Add, edit, remove buttons.
2. **Cargo bay view** (center) — top-down ship visualization.
3. **Route** (right) — scrollable list of stops with View Load buttons.

---

## Cargo bay view (top-down)

The C2 is rendered as two top-down rectangles representing the
forward and rear bays, drawn to scale (forward 6 wide × 9 deep, rear
8 wide × 15 deep).

Each bay shows:

- **Column dividers** as light dotted lines (advisory only — labels
  F1/F2/F3 above forward, R1/R2/R3/R4 above rear).
- **Loaded pallets** as colored rectangles sitting in the actual
  cells they occupy. Pallet width and length match the SCU footprint.
- **Color = destination color** (from `stations.color_hex`). Empty
  cells are neutral grey.
- **Conflicted pallets** are visually marked: red border / striped
  fill / pulse animation. Hovering shows the conflict group ID and
  ambiguous pallet sizes.
- **SCU running totals** displayed under each bay.

### Zone destination dropdown

At the top of each column (F1/F2/F3/R1–R4), a dropdown menu lets the
user manually assign a destination to that zone. The dropdown lists:

- "(auto)" — let the planner pick (default)
- All destinations from active contracts

When a destination is selected:

1. The zone's empty cells display that destination's color (faded).
2. The Recompute pass attempts to honor the assignment.
3. If the assignment can't fit, the validation log records a warning
   on the next recompute and the zone reverts to "(auto)".

### Drag and drop

The user can drag a pallet from one zone to another:

1. While dragging, valid drop targets are highlighted in green; cells
   that don't fit (capacity, footprint) are red.
2. On drop, the move is recorded as a **manual zone override** in
   `zone_assignments.is_manual_override = 1`.
3. The plan is marked dirty (`workdays.plan_dirty = 1`).
4. The "Modifications made — recompute required" banner shows.
5. Recompute respects manual overrides and re-derives only the
   downstream legs / unaffected zones around them.

---

## Recompute banner

A persistent, colored banner appears at the bottom of the main window
whenever `plan_dirty = 1`:

```
⚠  Modifications made — recompute required        [Recompute]
```

- High-contrast (uses accent yellow on deep navy).
- Stays visible until the user clicks Recompute or undoes the
  changes.
- Recompute can also be triggered by voice ("Hey Giant, recompute")
  or hotkey (configurable).

The banner explicitly lists the *number* of pending changes when
useful, e.g. *"3 modifications pending — recompute required"*.

---

## Per-stop "View Load" window

Each stop card in the Route panel has a **View Load** link/button
that opens a modal showing exactly what the cargo bay looks like when
that stop is reached.

### Layout of the Load View modal

```
┌───────────────────────────────────────────────────────────┐
│  Stop 3 — Beautiful Glen — Unload                    ✕   │
├───────────────────────────────────────────────────────────┤
│  Legend:                                                  │
│   F1  F2  F3  →  forward columns (6 wide × 9 deep)        │
│   R1  R2  R3  R4  →  rear columns (8 wide × 15 deep)      │
│   ▶  ramp loading direction                               │
│   colors = destinations (Beautiful Glen = orange,         │
│            Baijini = blue, Everus = green, …)             │
├───────────────────────────────────────────────────────────┤
│                                                           │
│   [ rendered top-down view with pallets at this stop ]    │
│                                                           │
│   Outgoing pallets (unload here) shown with a dashed      │
│   border or striped overlay.                              │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

- **No prose in the body.** Just the visual.
- Legend at the top covers column labels and the destination color
  key.
- Direction indicator (ramp arrow) shows where loading enters.

The user can step through stops with arrow keys / next/prev buttons
without closing the modal.

---

## Color assignment

When a destination first appears in a workday, the app picks the next
unused color from a curated palette (anchored by the brand colors in
`theme/colors.json`) and persists it to `stations.color_hex` so the
same destination keeps the same color across workdays.

The user can override a station's color in Settings → Stations.

---

## Status bar

Bottom of the main window:

- Mic state icon — `🎤 listening`, `🎤 muted`, `🎤 cancelled`
- Wake-word activity indicator (pulses on detection)
- Current stop progress (`Stop 3 of 11`)
- SCU usage (`357 / 696 SCU`)
- Settings cog
- API health indicator (green dot = OpenAI reachable)

---

## Out of scope for v1

- **Drag pallets between bays** — visual drag works only within a bay
  for v1.
- **Slicing pallets** — splitting a 16 SCU pallet into 2× 8 SCU is
  managed by the planner, not the UI.
- **Custom ship visual layouts** — only C2 has a tuned visualization
  layer in v1; future ships use auto-generated layouts from grid data.
