# Database

The desktop app uses **SQLite** as its sole datastore — a single
`cargo_planner.db` file in the user's app data folder. No server, no
network dependency, no per-user accounts.

The schema mirrors the original Google Sheets workbook (Active
Contracts, Stations, Commodities, Ship Layouts, etc.) but normalizes
into proper relational tables.

DDL lives in `data/schema.sql`. Seed data for stations and commodities
ships with the app and is loaded on first launch.

---

## Tables

### Reference data (slow-changing, ships with the app)

| Table | Purpose |
|-------|---------|
| `systems` | Star systems (Stanton, Pyro, Nyx, ...) |
| `stations` | Stations and surface outposts in each system |
| `station_aliases` | Alternate names / typos that resolve to a station |
| `station_distances` | Distances between stations *within* a system |
| `jump_gates` | Gateway-to-gateway links *between* systems |
| `commodities` | Cargo commodities |
| `commodity_aliases` | Alternate names for commodities |
| `ships` | Supported ships (only C2 active for now) |
| `ship_zones` | Operational column definitions per ship (F1, R3, etc.) |

### Workday / runtime state (mutable)

| Table | Purpose |
|-------|---------|
| `workdays` | One row per work session — origin, final destination, round-robin flag |
| `contracts` | Contracts in the current workday |
| `cargo_lines` | Individual cargo items inside a contract (one delivery + commodity each) |
| `route_stops` | Computed stop sequence for the workday |
| `zone_assignments` | Which pallets sit in which zone, computed by the planner |
| `pallet_conflicts` | Detected hidden pallet identity conflicts |
| `validation_log` | Warnings and errors generated during planning |
| `app_settings` | Key/value app settings (mic device, STT engine, etc.) |

---

## Distance model

Distances are stored as a normalized table per system, plus a separate
table for cross-system gateway hops:

```
station_distances:
  from_station_id  →  to_station_id  →  distance_km
```

Both endpoints must be in the same system (enforced via the database
schema and a check constraint on `system_id` equality).

For travel between two systems:

```
A (in Stanton)  →  Stanton Gateway   (station_distances row)
                ↓
                jump_gates row       (Stanton Gateway → Pyro Gateway)
                ↓
Pyro Gateway   →  B (in Pyro)        (station_distances row)
```

The route engine sums these three legs to get total cross-system
distance. Each system needs at least one gateway station marked with
`is_gateway = 1`.

Distances start blank — the schema is in place but most values are
unknown until the user provides them or we import from a community
source. The route engine treats missing distances as "use sort_order"
fallback (matches the current Apps Script behavior).

---

## Color assignment for stations

Each station gets an assigned hex color used in the cargo visualization
(zones colored by destination). Colors are auto-assigned on first
appearance from a curated palette and persisted to the
`stations.color` column so the same destination always gets the same
color across runs (Baijini is always blue, etc.).

Palette uses the brand colors from `theme/colors.json` plus a wider
auxiliary set for when more than four destinations are active in one
workday.

---

## Workday lifecycle

- **Start a new day:** `INSERT INTO workdays`, all prior workday data
  remains in the DB but is no longer "active". The user picks origin
  and final destination, optionally checks "round robin" (return to
  origin at end).
- **Resume:** the most recent open workday becomes active again. All
  existing contracts and computed plan are re-loaded.
- **End a day:** mark `ended_at`, route is frozen. Future loads reset
  to a new workday.

Only one workday is "active" at a time, identified by the row with the
latest `started_at` and a NULL `ended_at`.

---

## Recompute model

The planner output (route stops, zone assignments, conflicts) is **not
auto-recomputed on contract edits**. Instead:

1. Any mutation to contracts or cargo lines flips a `dirty` flag in
   the active workday.
2. The UI shows a prominent "Modifications made — recompute required"
   banner.
3. The user clicks **Recompute** to regenerate the plan.

This matches the user-facing expectation: changes accumulate, then the
pilot decides when to re-plan rather than the route shuffling on every
keystroke.

---

## Validation log

The `validation_log` table records issues from each planning run:
unknown stations, over-capacity zones, unfit pallets, missing
distances, etc. The UI surfaces these as the equivalent of the Sheet's
"Validation Log" tab, so users can diagnose why a plan failed or was
sub-optimal.

Severity levels: `INFO`, `WARN`, `ERROR`.

---

## Out of scope (for now)

- **Multi-user / shared trips** — single-PC only; no networking layer.
- **Cloud sync / backup** — local file only. User can copy
  `cargo_planner.db` to back up.
- **Migration tools for older spreadsheets** — the original sample
  data is hand-imported as part of the seed.
