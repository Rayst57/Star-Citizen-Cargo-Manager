# Cargo Position Nomenclature

A unified naming system for every cargo position on every ship in the
dataset, modeled on real-world container shipping (BAY-ROW-TIER) and
adapted for ship sizes ranging from a 1 SCU skiff to a Hull E.

**Status:** proposal — not yet applied to `data/ships.json`.

---

## Real-world precedents

### Container ships (commercial maritime)

The international stowage plan format is **BAY-ROW-TIER**, expressed as a
6-digit code (`BBRRTT`):

- **Bay** — numbered from the **bow (01)** aft. Odd bays for 20 ft
  containers, even for 40 ft.
- **Row** — numbered from the **centerline outward**. **Even on port,
  odd on starboard.** Centerline = 00.
- **Tier** — numbered from the bottom. Under-deck: 02, 04, 06… On deck:
  82, 84, 86…

This is what every dockworker on Earth uses to address a cargo cell.

### Military cargo aircraft (USAF)

C-130 / C-17 use sequential **pallet positions** (P1 → P6 for C-130, P1
→ P18 for C-17), numbered front-to-back. Side-by-side rows are denoted
L/R suffixes (e.g. P9L, P9R on C-17 LGS).

This is what loadmasters use for small/medium aircraft.

### Naval (warships, large vessels)

Large vessels use named **holds** and **decks**:

- **Holds** — numbered from bow (No. 1 Hold, No. 2 Hold) or named by
  position (Forward Hold, Aft Hold, Stern Bay).
- **Decks** — named by level (Main Deck, Tween Deck, Lower Hold).
- **Frames** — transverse structural bulkheads, numbered from bow.
  Used for compartment addressing on warships.
- **Port / Starboard / Bow / Stern / Amidships** — directional terms.

---

## Proposed SC system

Apply a different scheme based on ship size class. Every scheme rolls up
to the same canonical address format so the planner can speak one
language internally.

### Tier classification

| Class | Total SCU | Naming style | Example ships |
|-------|-----------|--------------|---------------|
| **Skiff** | ≤ 16 | Pallet positions (aircraft style) | MPUV-Cargo, Cutter, Mustang Alpha, Pisces C8X, Nomad |
| **Hauler** | 17 – 200 | Single-hold BAY-ROW-TIER | Cutlass Black, Avenger Titan, Freelancer, Constellation Taurus |
| **Heavy** | 201 – 1000 | Named holds + BAY-ROW-TIER | C2/M2 Hercules, Caterpillar, Mercury, Starlancer MAX, 600i |
| **Capital** | > 1000 | Full naval nomenclature (multi-hold, multi-deck) | Carrack, Polaris, Galaxy, Hull C/D/E, Reclaimer |

### Canonical address format

`[Hold]-B[Bay]-[Side][Row]-T[Tier]`

- **Hold** — named compartment. Omitted on single-hold ships. Examples:
  `MAIN`, `MOD2`, `HOLD-FWD`, `RACK-3`, `PORT-HOLD`.
- **Bay** — 2-digit longitudinal index from bow. `B01` = bow-most.
- **Side** — `P` (port), `S` (starboard), `C` (centerline, for
  odd-width bays only).
- **Row** — 2-digit lateral index outward from centerline. `P01` = first
  port column, `P02` = second, etc. Note: this departs from the
  commercial even/odd convention because it reads more naturally aloud
  ("port one tier two" vs "row two tier two").
- **Tier** — 2-digit vertical index from deck up. `T01` = deck level.

### Examples

| Ship | Position | Address | Spoken |
|------|----------|---------|--------|
| Cutter | only pallet slot | `P1` | "pallet one" |
| Avenger Titan | port column, 3 frames in, deck | `B03-P01-T01` | "bay 3 port 1 tier 1" |
| C2 Hercules main bay | starboard 2, frame 7, tier 2 | `MAIN-B07-S02-T02` | "main bay 7 starboard 2 tier 2" |
| Caterpillar module 3 | center, frame 2, tier 3 | `MOD3-B02-C00-T03` | "mod 3 bay 2 center tier 3" |
| Carrack mid hold | port 3, frame 5, tier 1 | `HOLD-MID-B05-P03-T01` | "mid hold bay 5 port 3 tier 1" |
| Hull D rack 4 | port 1 frame 8 tier 1 | `RACK4-B08-P01-T01` | "rack 4 bay 8 port 1 tier 1" |

### Skiff exception

Class **Skiff** ships skip the full address and just use `P1`, `P2`, …
sequential from the loading face. A 4 SCU pallet on a Cutter is `P1`,
not `B01-P01-T01`. The data file still stores the underlying grid for
the planner; the display label is the simplified form.

---

## Hold naming convention (multi-hold ships)

Names should describe physical position so the loadmaster can speak them
naturally. Standard tokens:

| Token | Meaning |
|-------|---------|
| `MAIN` | the only / primary hold on a single-hold ship |
| `FWD` / `MID` / `AFT` / `STERN` | longitudinal section |
| `PORT` / `STBD` | lateral position (paired holds) |
| `UPPER` / `LOWER` | deck level on multi-deck ships |
| `MOD1`–`MODn` | numbered modules (Caterpillar, Galaxy) |
| `RACK1`–`RACKn` | external rack rings (Hull series) |
| `NOSE` | nose / bow-most cargo space |
| `STEP` | stepped section of a tapered bay (Hercules series) |

Concrete assignments per ship will be done in the labeling pass. Sample
proposed names:

- C2 / M2 Hercules: `MAIN`, `STEP-AFT`
- 600i Explorer: `BAY-FWD`, `STALL-PORT`, `STALL-STBD`
- Caterpillar: `NOSE`, `MOD1`, `MOD2`, `MOD3`, `MOD4`
- Carrack: `HOLD-FWD`, `HOLD-MID`, `HOLD-AFT`, `STERN`
- Polaris: `HOLD-PORT`, `HOLD-STBD`
- Hull C/D/E: `RACK1` … `RACKn` (per ring)
- Galaxy Cargo: `DECK-UPPER`, `DECK-LOWER`
- Mercury Star Runner: `MAIN`, `NOOK`
- Starlancer MAX: `BAY-MID`, `BAY-AFT-PORT`, `BAY-AFT-STBD`

---

## Loading face metadata

Independent of bay numbering, every bay records which face the cargo
ramp/door is on. This drives "first off, last on" logic without
forcing the bay numbering itself to follow loading order.

```json
{
  "id": "MAIN",
  "ramp_face": "stern",
  "columns": 8,
  "rows": 15,
  "layers": 4
}
```

Possible values: `bow`, `stern`, `port`, `starboard`, `top`.

---

## Legend on the illustration window

Top of the per-stop illustration window shows:

1. A small key diagram of the ship with **bow / stern / port / stbd**
   labeled.
2. The hold(s) outlined and labeled in their bay names.
3. Tier indicator (small stack diagram showing T01 → Tn).
4. A direction arrow from the ramp face into the hold.

No prose. Just labels on a simplified ship outline.

---

## Open questions

1. **Bow-relative vs ramp-relative bay numbering.** Maritime convention
   is bow = 01. SC players unload from the ramp first — should `B01`
   mean bow (universal) or nearest ramp (player-mental-model)? My pick:
   bow, because it's stable across ship orientation and matches every
   real-world precedent. "First-off" reasoning uses `ramp_face`.
2. **Side letter style** — `P/S/C` (1 char) vs `PORT/STBD/CTR` (verbose).
   Pick: `P/S/C` for compact display, full words for voice readback.
3. **Class boundaries** — the 16 / 200 / 1000 SCU thresholds are my
   best guess. Should I adjust to natural fleet groupings instead?
