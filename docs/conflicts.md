# Hidden Pallet Identity Conflicts

A first-class concept ported from the V0.3 handbook.

---

## The problem

The station cargo manager elevator displays all pallets grouped into stacks
**by destination**, but does **not label** which destination each stack
belongs to. The pilot can see pallet sizes and commodity (via mouseover) but
cannot read the destination off the UI.

This means the pilot must deduce destinations from:
1. **Unique pallet sizes** — a size that only one destination has.
2. **Different pallet counts** — if Everus has 6×8 and Baijini has 3×8,
   the 6-count stack is obviously Everus.

A **conflict** exists when a pallet size has the **same count** across
two or more destinations. At that point, individual pallets of that size
are visually indistinguishable — grabbing the wrong one causes a
"0/1 delivered" rejection at the destination.

---

## Canonical example

- Contract 1: Yellow Core → Everus, 55 SCU Tungsten, max 8
  Pallets: **6×8 + 1×4 + 1×2 + 1×1**

- Contract 2: Yellow Core → Baijini, 27 SCU Tungsten, max 8
  Pallets: **3×8 + 1×2 + 1×1**

| Size | Everus count | Baijini count | Status |
|------|-------------|---------------|--------|
| 8    | 6           | 3             | ✅ Distinguishable — different counts |
| 4    | 1           | 0             | ✅ Unique to Everus |
| 2    | 1           | 1             | ⚠ **CONFLICT** — same count |
| 1    | 1           | 1             | ⚠ **CONFLICT** — same count |

On the elevator you see stacks of 6×8 and 3×8 — identifiable by count.
But you see two separate 2 SCU pallets and two separate 1 SCU pallets with
no way to tell which stack each belongs to.

---

## Ambiguity rule

> A pallet size is **ambiguous** when ≥ 2 delivery destinations have the
> **same count** of that size, for the same commodity, at the same pickup.

Commodity is included because the elevator mouseover shows commodity name —
2 SCU Tungsten and 2 SCU Aluminum from different contracts are **not**
ambiguous.

---

## The fix

The pilot manually selects which pallets ride the elevator. Zone isolation
makes the conflict testable at delivery:

1. Assign each destination's cargo to a **different zone** onboard.
2. At delivery, **bring up only the zone for that contract**.
3. Any pallets the station **rejects** do not belong here — by elimination,
   they belong to the other destination's zone.
4. Reload rejected pallets to the correct sibling zone.

---

## Detection algorithm

Per pickup station × commodity group:

1. **Palletize** every cargo line (largest-first into legal SCU sizes).
2. **Build per-destination pallet-count maps**: `{size: count}` for each
   delivery destination.
3. **Compare counts** for each size across destinations.
4. **Ambiguous** = same count in ≥ 2 destinations → pilot cannot distinguish.
5. **Unique** = size appears in only one destination → safe to load.
6. **Distinguishable by count** = size appears in multiple destinations but
   with different counts → pilot can count stacks to tell them apart → not
   flagged as a conflict.
7. If any ambiguous sizes exist → create a **ConflictGroup**.

---

## Conflict statuses

Used in display and the `pallet_conflicts.status` column:

| Status | Meaning |
|--------|---------|
| `active`             | Conflict identified, pallets onboard, none unloaded yet |
| `staged`             | Pallets isolated in assigned zones; ready to test |
| `resolving`          | Currently at a delivery stop working through this group |
| `resolved`           | All this destination's pallets accepted |
| `partially_resolved` | Some accepted, some rejected and relocated |
| `cleared`            | Only one destination's pallets remain — conflict gone |
| `unresolved`         | End of route, ambiguous pallets still onboard |

---

## Zone assignment doctrine for conflict pallets

- Keep conflict pallets **separated by destination** in different zones.
- **Two-pass assignment**: non-conflict cargo fills zones first; conflict
  cargo is assigned to whatever zone has the most remaining capacity for
  that contract — giving conflict pallets elbow room for the test-and-reload
  workflow.
- Never blend two pickup stations' conflict groups into the same holding zone
  unless absolutely unavoidable.
- If the ship is over capacity and isolation is impossible, the planner logs:
  **"CANNOT ISOLATE — unload all pallets at both stops to sort."**

---

## Stop-handling order at a delivery stop with conflicts

Per handbook §24 / §12:

1. **Unload non-conflict cargo first** — clear zones with no ambiguity.
   `"Offload all zone R5: Contract 1, Contract 3 — no conflicts."`

2. **Handle ONE conflict group at a time, one destination at a time:**
   - Bring up **only** the zone for this destination's contract.
   - Unload **unique pallets first** (safe — they can only be yours).
   - **Test ambiguous pallets last** — send them down, watch for rejection.
   - Any **rejected pallet** → reload to the sibling destination's zone.
   - Mark this contract's conflict as `resolved` or `partially_resolved`.

3. **Move to the next conflict group** if any remain at this stop.

Do not mix new cargo from different conflict groups during resolution.
Complete one group's resolution before opening another.

---

## Multiple pickups, multiple conflict groups

If cargo from two different pickups (e.g. Yellow Core and Shallow Fields)
both have conflicts with destinations visited at the same stop, they are
**separate conflict groups** (identified by pickup × commodity).

- Each group gets its own zone pair.
- At the delivery stop, resolve one group, then the next.
- Rejected pallets from Group 1 go to Group 1's sibling zone — not mixed
  with Group 2's pallets.

---

## Three-way conflicts (warning)

When **3 or more** destinations share the same count of the same pallet size
from the same pickup, elimination is harder:

- After stop A: rejected pallets could belong to B or C — still ambiguous.
- You must now test B's zone next. Rejected pallets from B's test → C.

The planner warns on 3+ way conflicts and sequences deliveries to minimize
repeated handling. Zone isolation is still required — all three destinations
must be in separate zones.

---

## Display rules

### Compact mode (default)

```
R1 → 55 SCU Tungsten → Everus Harbor
```

### Conflict mode (auto-applied when conflicts exist)

```
R1 → 55 SCU Tungsten → Everus Harbor  [Contract 1]  ⚠ CONFLICT
```

### Conflict Notes section (at relevant stops)

```
⚠ GROUP 1 — Yellow Core × Tungsten
  Everus Harbor → zone R1
    Unique (safe): 6×8 + 1×4 SCU Tungsten
    Ambiguous ⚠:  1×2 + 1×1 SCU Tungsten

  Baijini Point → zone R4
    Ambiguous ⚠:  1×2 + 1×1 SCU Tungsten

  At Baijini: bring up zone R4 only.
    Unique (deliver confidently): 3×8 SCU
    Ambiguous (test these last): 1×2 + 1×1 SCU
    If station REJECTS → it belongs to Everus. Reload to R1.
```

In the visual cargo grid:
- Conflicted pallets have a **red border / striped fill** and pulse animation.
- Hovering shows conflict group ID and ambiguous sizes.

---

## Recovery — when the elevator rejects a pallet

Per §25:

1. Mark the pallet as **rejected** for the current destination.
2. Reload it to the assigned zone for the *other* candidate destination.
3. If only two destinations were ambiguous, a rejected pallet at one
   becomes a known pallet for the other (by elimination).
4. Finish resolving the current contract's conflict before opening another
   conflict set.
