# Workday Flow

A **workday** is one work session — one ship, one origin, optionally
one final destination, and a set of contracts. The route and cargo
plan are scoped to a workday; ending a workday freezes the plan.

---

## App start screen

When the app launches, the user sees one of two states:

### State A — There's an active workday in the database

The most recent `workdays` row with a NULL `ended_at` is "active". The
start screen shows:

```
╔══════════════════════════════════════════════╗
║                                              ║
║   Resume Workday                             ║
║   ───────────                                ║
║   Started: 2026-05-09 14:32                  ║
║   Ship: C2 Hercules                          ║
║   Origin: Seraphim Station                   ║
║   Contracts: 7                               ║
║   [ Resume ]    [ End and Start New ]        ║
║                                              ║
╚══════════════════════════════════════════════╝
```

### State B — No active workday

```
╔══════════════════════════════════════════════╗
║                                              ║
║   Start New Workday                          ║
║   ─────────────────                          ║
║                                              ║
║   Ship           [ C2 Hercules     ▼ ]       ║
║   Origin         [ Seraphim Station ▼ ]      ║
║   Final dest.    [ Seraphim Station ▼ ]      ║
║   ☑ Round robin (return to origin)           ║
║                                              ║
║                  [ Start Workday ]           ║
║                                              ║
╚══════════════════════════════════════════════╝
```

---

## Round robin

When checked:
- Final destination is forced to equal origin
- Route ends back at origin even if no cargo unloads there

When unchecked:
- Final destination is whatever the user picked (defaults to origin)
- If no contracts deliver to the final destination, the route still
  ends there as a deliberate stop (refuel, RTB, etc.)

---

## Lifecycle

```
[Start workday]
    │
    ├─► User adds/edits contracts (voice or manual)
    │       │
    │       └─► plan_dirty = 1; "Recompute required" banner appears
    │
    ├─► User clicks Recompute
    │       │
    │       └─► Planner runs, route_stops + zone_assignments populated,
    │           plan_dirty = 0
    │
    ├─► Visual cargo manipulation (drag pallets between zones)
    │       │
    │       └─► plan_dirty = 1 (zone overrides recorded);
    │           recompute will respect manual overrides for downstream legs
    │
    └─► [End workday]
            │
            └─► ended_at set, plan frozen, app returns to State B next launch
```

A workday cannot be deleted from the UI in normal use — only ended.
This preserves history. (DB cleanup is a separate user action.)

---

## What ending a workday means

- The active workday's `ended_at` is set.
- All contracts, cargo lines, route stops, and zone assignments stay
  in the database — they remain queryable for "show me my last run"
  history features later.
- The next app launch shows State B (no active workday).

---

## What starting a *new* workday means

- A fresh `workdays` row is created.
- No contracts carry over.
- No route data carries over.
- The previous workday's ended_at is set if it wasn't already.

This is the equivalent of the original Apps Script's hard-reset
`replaceContracts` behavior.

---

## Open questions

1. **Zero-contract workday?** Should "Start Workday" be allowed before
   any contracts are entered, or block until at least one contract
   exists?
2. **Per-contract pickup vs single-origin model?** Right now origin is
   set per workday; contracts have their own pickup stations (which
   may differ from origin). The route engine handles both — origin is
   just where the user *starts* the day. Confirm this matches your
   workflow.
