# Hidden Pallet Identity Conflicts

A first-class concept ported from the V0.3 handbook.

---

## The problem

When two cargo destinations share **identical pallet sizes** at the
same pickup station, the pallets look the same once they're on the
station's elevator/cargo manager. The pilot cannot visually
distinguish which pallet stack belongs to which destination, so
grabbing the "wrong" one results in a delivery that does not count
against the contract.

Example:
- Contract 1: Yellow Core → Everus, 55 SCU, max 8
  Pallets: 6×8, 1×4, 1×2, 1×1
- Contract 2: Yellow Core → Baijini, 27 SCU, max 8
  Pallets: 3×8, 1×2, 1×1

The 8-SCU stacks are unique by count (6 vs 3), but the **2 SCU and 1
SCU pallets are ambiguous** — visually identical between the two
destinations.

---

## The fix

Because the **player manually selects** which pallets ride the
elevator, the planner can isolate ambiguous pallets in **different
cargo zones onboard**. At the destination, the player only brings up
the zone(s) for that contract. The visual ambiguity on the elevator is
moot if only one contract's pallets are on it.

**Operational principle:** keep conflict-prone contracts in different
zones, then "test" one contract's ambiguous pallets at a time at each
delivery.

---

## Detection algorithm

Per the V0.3 handbook §17–19:

1. **Palletize** every cargo line (largest-first into legal SCU sizes).
2. **Build active groups** — group cargo lines by pickup station ×
   route phase × destination × commodity (commodity is sometimes
   useful, sometimes not — depends on whether the elevator UI shows
   commodity name).
3. **Compare pallet sizes** across active groups sharing a pickup.
4. **Identify unique vs ambiguous pallets:**
   - A pallet size is *unique* to a destination if no other active
     group at the same pickup has any pallet of that size.
   - A pallet size is *ambiguous* if two or more active groups at the
     same pickup contain it.
5. **Create a conflict group** with: ID, pickup, destinations involved,
   contracts involved, ambiguous pallet sizes, unique pallet sizes per
   destination, and a suggested handling zone per destination.
6. **Attach conflict notes** to the relevant contract lines, load
   stop, unload stops, and current cargo loadout snapshots.

---

## Conflict statuses

Used in display and the `pallet_conflicts.status` column:

| Status | Meaning |
|--------|---------|
| `active`     | Conflict identified, pallets onboard, none unloaded yet |
| `staged`     | Conflict pallets isolated in their assigned zones; ready to test |
| `resolving`  | Currently at the delivery stop, working through the conflict |
| `resolved`   | All this destination's pallets accepted by the elevator |
| `partially_resolved` | Some pallets accepted, some rejected |
| `cleared`    | Conflict gone — only one destination's pallets remain |
| `unresolved` | At end of route, ambiguous pallets still onboard |

---

## Zone assignment doctrine for conflict pallets

Per the handbook §22:

- Keep conflict pallets **separated by contract** whenever possible.
- Prefer to keep conflict candidates with the destination's "main"
  zone if the zone has spare capacity and won't be reused for future
  cargo.
- If the destination zone is tight, use a separate conflict-holding
  zone (e.g. R4) and record a reason: *"largest spare space"*,
  *"avoids future fill"*, *"keeps Baijini conflict away from Everus
  R1"*, etc.
- Never blend two pickup stations' conflicts into one holding zone
  unless absolutely unavoidable.

---

## Recovery — when the elevator rejects a pallet

Per §25:

1. Mark the pallet as **rejected** for the current destination.
2. Reload it to the assigned correct zone (or conflict-holding zone)
   for the *other* candidate destination.
3. If only two destinations were ambiguous, a rejected pallet at one
   becomes a known pallet for the other (by elimination).
4. Continue resolving the current contract's conflict before opening
   another conflict set.

---

## Display rules

The handbook is strict about visibility (§16, §21):

### Compact mode (default)

```
R1 → 55 SCU Tungsten → Everus
```

### Conflict mode (auto-applied when conflicts exist)

```
R1 → 55 SCU Tungsten → Everus | Unique: 6×8 + 1×4 | CONFLICT: 1×2 + 1×1
```

### Detailed mode (on demand)

```
R1 → 55 SCU Tungsten → Everus
Pallets: 6×8 SCU, 1×4 SCU, 1×2 SCU, 1×1 SCU
```

In the visual cargo grid:

- **Conflicted pallets are visually marked** — striped fill or red
  border. Hover/click reveals the conflict group ID and ambiguous
  sizes.
- The destination color of each zone reflects the **dominant
  destination**; conflicted pallets get a dual-color stripe or a
  warning icon.

---

## Stop-handling order

At a delivery stop with conflicts, follow the handbook's preferred
order (§24):

1. Unload non-conflicted cargo first.
2. Unload unique cargo for this destination.
3. Test one contract's ambiguous pallets.
4. Reload any rejected pallets to the other destination's zone.
5. Mark this contract's conflict as resolved or partially resolved.
6. Move to the next conflict set if any.
