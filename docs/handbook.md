# SC Cargo Operations Handbook

This is the operating doctrine for the planner. Adapted from the V0.3
handbook the user authored for the Google Sheets / GPT version. When
designs in other docs conflict with this handbook, the handbook wins
unless explicitly superseded here.

---

## 1. Mission

Make multi-contract Star Citizen cargo runs manageable during live
flight operations on the C2 Hercules.

The planner is a cargo operations assistant, not just a route sorter.
At every stop, it answers four questions:

1. What do I unload here?
2. What do I load here?
3. What cargo is still onboard after this stop?
4. Are there any pallet identity conflicts I need to handle carefully?

---

## 2. Design philosophy

Operational clarity beats theoretical route neatness. A slightly
longer route is preferred when it avoids major cargo reshuffling or
prevents a hidden pallet conflict from escalating into a full cargo
unload.

The pilot is busy and possibly in flight — output must be direct.

### 2.1 Priority order

Use this order for all planning decisions:

1. Minimize unload complexity, including pallet identity conflicts.
2. Minimize station count.
3. Minimize reshuffling.
4. Minimize wasted SCU.
5. Minimize travel time.

If travel time conflicts with unload sanity, **unload sanity wins.**

### 2.2 Operating style

Do:
- Compact cockpit-readable output.
- Consistent zone names.
- Consistent action labels.
- Keep contract identity visible when it matters.
- Keep conflict information visible where the pilot needs it.

Don't:
- Vague cargo instructions.
- Mix destinations without clear labels.
- Hide conflicts in a paragraph.
- Tell the pilot to unload everything unless unavoidable.
- Invent zones not defined in the database.
- Preserve stale route data after a workday reset.

---

## 3. Source of truth order

When information conflicts, use this order:

1. **The database** (ship layouts, station list, distances, etc.)
2. **Active workday contracts** (user input)
3. **This handbook**
4. **LLM assumptions**

If the database defines ship layout data, it wins for: zone names,
zone dimensions, zone capacity, zone type, load order, unload
priority, left/right adjacency, bulk/overflow zones.

If the database does not yet implement a feature described here, the
planner may still provide operational guidance, but should not pretend
the backend computed it.

---

## 4. Approved cockpit action labels

These are the **only** labels the planner emits for stop actions:

- `Depart`
- `Arrive`
- `Load`
- `Unload`
- `Final unload`
- `Refuel`
- `Salvage pickup`
- `Emergency reroute`

Internal labels like `START`, `FINAL_RETURN`, `PICKUP`, `DELIVERY`,
`ARRIVAL`, `DONE` may be used inside the engine but are normalized to
the approved labels at output.

---

## 5. Standard stop template

Every stop in the route output follows this structure:

```
Stop #: [Station] — [Action]

Current Contracts at Play:
  - [Contract IDs / destinations]

Unload:
  - [Zone] → [Cargo Summary]
  - None

Load:
  - [Zone] → [Cargo Summary]
  - None

Conflict Notes:
  - [Conflict status / instruction]
  - None

Current Cargo Loadout:
  - [Zone]: [Cargo currently onboard after this stop]
  - Empty
```

### Good vs bad summary format

**Good:**
```
F1 → 23 SCU Tungsten + 21 SCU Aluminum → Seraphim
```

Identifies zone, SCU amount, commodity, destination.

**Bad:**
```
Deliver Seraphim cargo
```

No zone, no SCU, no commodity, no contract context, no conflict
indication.

---

## 6. Zero-contract rule

If zero contracts are active, route output is **completely blank**:
no stops, no zones, no checklist tasks, no phantom return-to-origin.

---

## 7. Hard reset rule

Starting a new workday is a hard reset. After it:

- Old contracts are gone.
- Old route output is gone.
- Old zone assignments are gone.
- Old conflict notes are gone.
- Old loadout snapshots are gone.

This is enforced at the database layer by ending the previous workday
and creating a new one (see `workday.md`).

---

## 8. Palletization

Use largest-first against valid SCU pallet sizes: **32, 24, 16, 8, 4,
2, 1**. Use largest pallet ≤ contract max pallet size and ≤ remaining
SCU; repeat until SCU = 0.

### 8.1 Examples

| Total | Max | Breakdown |
|-------|-----|-----------|
| 55 SCU, max 8 | 8 | 6×8 + 1×4 + 1×2 + 1×1 |
| 27 SCU, max 8 | 8 | 3×8 + 1×2 + 1×1 |
| 45 SCU, max 16 | 16 | 2×16 + 1×8 + 1×4 + 1×1 |

### 8.2 Pallet display rules

- **Default mode:** hide pallet breakdowns.
- **Show breakdowns when:** user asks for detail mode, a conflict
  exists, recovery requires identifying a rejected pallet size, or
  zone fit validation depends on dimensions.

---

## 9. Cargo grouping

Always group cargo by zone, destination, commodity, contract, and
conflict group when applicable.

- Allowed: same zone, multiple destinations (must be explicit).
- Preferred: same destination, multiple commodities combined.
- For same-destination multiple-contract loads with no conflict,
  combine cautiously and label all contract IDs.
- When conflict exists, **split by contract** and never merge.

---

## 10. Loading philosophy

- Earliest unload cargo should be easiest to access.
- Last unload cargo can be deeper.
- Cargo for the same destination stays together when practical.
- Cargo for the same contract stays together when conflict exists.
- Conflict pallets must be staged so they can be tested without
  disturbing unrelated contracts.
- Don't fill a zone with future cargo if that zone is needed for
  conflict recovery.
- Don't create avoidable reshuffle.

---

## 11. Hidden pallet identity conflicts

First-class concept. See `conflicts.md` for the detection algorithm,
status labels, zone assignment doctrine, and recovery rules.

---

## 12. Stop work order

At each stop, do work in this order:

1. Arrival / station action
2. Non-conflict unloads
3. Unique unloads
4. Contract-specific conflict resolution
5. Reload unresolved conflict pallets
6. New loads
7. Verify current cargo loadout

If a stop has both unload and load work, resolve unload conflicts
before loading new conflict cargo when practical. Don't mix new cargo
into unresolved old conflict zones.

---

## 13. No-op stop cleanup

A stop should appear in the route only if it has at least one of:

- Initial origin / departure
- Final return / final unload
- Load activity
- Unload activity
- Refuel activity
- Salvage pickup
- Emergency reroute

If origin would otherwise repeat as a final return with no cargo and
no operational action, omit it unless `round_robin` is on.

---

## 14. Validation and warnings

The planner emits warnings to the validation log for:

- Missing zone assignment
- Unknown pickup or delivery station
- Unknown commodity
- Invalid SCU amount or max pallet size
- Invalid pallet fit
- Over-capacity zone
- Undefined zone
- Bulk zone with manual fit assumption
- Conflicting route data
- No delivery stop after pickup
- Unresolved conflict pallets at end of route
- Conflict zones being future-filled
- Conflict sets mixed together

Warning style: **concise and actionable**. *"R5 is not a default C2
zone. Add R5 to the ship layout or reassign cargo."* — not *"there
might be some issue with the zone layout, maybe."*

---

## 15. Voice readout style

Voice readouts are shorter than written rundowns:

- Use stop number.
- Use short station name if clear.
- Lead with action.
- Mention conflict only when active.
- Avoid long commodity lists unless needed.

Example:

```
Stop 3. Baijini. Unload R4 unique cargo. Resolve Contract 2
conflict, 2 SCU and 1 SCU only. Reload rejected pallets to R1.
```

---

## 16. Planner backlog

### 16.1 Transload consolidation (PLANNED — not yet implemented)

**What:** A mid-route rebalancing pass. At each stop, after the
stop's unloads have been applied but BEFORE new cargo is loaded, the
planner checks whether any destination's remaining cargo can be
consolidated into fewer zones — and emits explicit transload
instructions if so.

**Why:** The initial allocation (Stop 1 departure) sometimes has to
split a destination across two zones because of overflow at the
start. Once part of the ship empties at an intermediate stop, a
better zone arrangement often becomes possible — and doing it before
new cargo lands keeps the loadout tidy and predictable for the rest
of the route.

**Example:**

- Baijini was split across zones 1 and 3 at Stop 1 (capacity
  overflow forced the split).
- Everus was in zones 2 and 1.
- At Stop 2, Everus's zone-1 cargo unloads — zone 1 now has room.
- **Transload:** move Baijini from zone 3 into zone 1 so all of
  Baijini's remaining cargo lives in one place.
- Then load whatever new cargo this stop picks up.

**Order of operations at each stop:**

```
1. Unload     — cargo for this destination leaves.
2. Transload  — consolidate remaining cargo across zones (new).
3. Upload     — load new cargo for downstream destinations.
```

This supersedes the conflict-era 7-step §12 flow for the
post-CIG-fix planner; §12 is still the spec when
`strict_pallet_conflict_mode` is on.

**What the planner needs to do:**

- At each stop, after applying scheduled unloads to the in-memory
  zone state, scan for destinations whose remaining cargo lives in
  more than one zone.
- For each split destination, check whether a single zone now has
  enough remaining capacity to hold the whole load. Prefer the zone
  the destination already occupies (so the transload is one-way).
- Generate `TRANSLOAD` rows in the per-stop plan: *"move N SCU of
  Baijini cargo from R3 to R1."* These are surfaced in the detailed
  plan and in voice readouts, not in `zone_assignments` (which
  records the final state).
- Re-check the narrow-conflict rule before moving cargo into a zone
  that other contracts share — don't create a new conflict to fix
  an old split.
- Don't generate gratuitous transloads. Skip when the destination
  already fits in its primary zone, or when the move wouldn't
  actually free a contiguous block worth using.

**Out of scope for v1:** automatic detection of *physical* transload
cost (the player has to manually move pallets); the planner just
states the move. The player can ignore it if it's not worth the
time.

### 16.2 Late-binding cargo pickup (PLANNED — not yet implemented)

**What:** When the route visits a pickup station more than once
*before* the cargo's delivery, defer the load to the **latest**
such visit — not the earliest. Keeps the ship lighter through the
intermediate stops by not carrying cargo that doesn't need to be
on board yet.

**Example** (real route the user spotted):

```
Stop 1  Riker Memorial Spaceport    — Depart  Load: 8 SCU Al → Baijini [#1]
Stop 2  Seraphim Station            — Arrive  Load: 8 SCU Al → Baijini [#3],
                                              8 SCU Al → Riker   [#3]   ← #3 here today
Stop 3  Baijini Point               — Arrive  Unload [#1], [#3]; Load Cargo → Seraphim [#2]
Stop 4  Seraphim Station            — Unload [#2]                        ← #3 should load here
Stop 5  Riker Memorial Spaceport    — Final unload [#3]
```

Contract #3's Riker pallet was loaded at Stop 2 and sat on the ship
through Stop 3 and Stop 4 even though the route comes back to
Seraphim at Stop 4. Loading at Stop 4 instead frees a zone for the
Stop 2→3 leg.

**Trade-off with `strict_pallet_conflict_mode`:** this optimization
is **not safe** when pallet identity is ambiguous. Loading early
gives the planner flexibility to deconflict in any zone over the
full route; late-binding could trap the pilot with an unresolvable
mix at the late pickup. So:

- Default (post-CIG-fix) mode: late-bind freely.
- `strict_pallet_conflict_mode` ON: keep the current eagerly-load
  behavior — the conflict spec assumes everything is on board when
  resolution happens.

**Where the code lives:** `src/planner/route.py` line ~189 —
`station_map[c["pickup_station_id"]]["loads"].append(ref)` attaches
every load to the pickup station unconditionally. The fix is to
walk the visit list per cargo line and pick the latest visit whose
position is still earlier than the delivery's first visit.

### 16.3 Manual stop reorder override (PLANNED — not yet implemented)

**Why:** The route builder orders stops by `stations.sort_order`,
which is a coarse planet-position proxy with no notion of travel
distance. Stations that are physically close can end up on opposite
sides of the route. Until a real distance chart exists, the user
needs a manual override.

**Example** (the user spotted on a real route):

```
Stop 1  Seraphim Station      — Depart   Load #3 cargo for Baijini & Riker
Stop 2  Baijini Point         — Arrive   Unload #3, load #4
Stop 3  Orison                — Arrive   Load #2 for Baijini
Stop 4  Riker Memorial …      — Arrive   Unload, load #1
Stop 5  Baijini Point         — Unload   #2
```

Seraphim and Orison are spatially adjacent; bouncing Seraphim →
Baijini → Orison → Baijini is bad even without distance numbers.
Reordering "no, Stop 2 is actually Orison" produces a better route
that sort_order alone can't reach.

**Proposed UX:**

- Each StopCard gains a small "Stop #" dropdown (1..N).
- Changing one stop's number swaps with whatever stop currently has
  that number. ("Don't try to auto-compute the stop. Force the user
  to manually select what stop Baijini would be after that.")
- Origin (Stop 1) and Final destination (Stop N) are LOCKED — only
  the interior stops are reorderable.
- Changing a number marks the plan dirty. The Recompute button
  (existing) commits the new order; until then the visual order is
  pending.
- A small "Reset stop order" link on the Route header wipes the
  override and lets sort_order drive again.
- Manual order doesn't survive a workday switch — it's per-workday.

**Persistence:** a new column on `workdays` —
`manual_route_order TEXT` holding a JSON list of station IDs in the
user's preferred order. NULL = use sort_order (current behavior).
Stations that appear in the contract set but not in the JSON list
get appended at the end by sort_order. Stale station IDs (no longer
in the contract set) are silently dropped on read.

**Where the code lives:** `src/planner/route.py` `_sort_key()` at
line ~193 is the single point that decides ordering. The fix is to
read `workday["manual_route_order"]`, use that list's position when
it has the station, and fall back to sort_order otherwise.

**Trade-off with `strict_pallet_conflict_mode`:** the manual order
itself is mode-independent — but combine it with the strict path
carefully, since reordering can change which loads/unloads collide
in time. Probably worth showing a hint in the UI when both are on.
