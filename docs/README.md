# Documentation Index

Design and operational documentation for the Star Citizen Cargo
Manager. Read in this order if you're new:

| # | Doc | What it covers |
|---|-----|----------------|
| 1 | [handbook.md](handbook.md) | Operating doctrine — mission, priorities, output format, work order. The canonical reference; other docs defer to it. |
| 2 | [cargo_grid.md](cargo_grid.md) | C2 Hercules bay topology, mental column dividers (F1-F3 / R1-R4), pallet footprints, packing approach. |
| 3 | [conflicts.md](conflicts.md) | Hidden pallet identity conflicts — detection, statuses, zone staging, recovery. |
| 4 | [database.md](database.md) | SQLite schema overview — reference data, workday state, distance model, recompute flow. |
| 5 | [workday.md](workday.md) | Workday lifecycle — start screen, resume vs new, round robin, recompute. |
| 6 | [ui_interactions.md](ui_interactions.md) | Main window layout, top-down ship view, zone dropdowns, drag-and-drop, View Load modal. |
| 6b | [ui_spec.md](ui_spec.md) | PySide6 implementation spec — full widget tree, AppController API, threading model, module layout. |
| 7 | [manual_entry.md](manual_entry.md) | Forms for adding/editing contracts and stops without voice. |
| 8 | [giant_commands.md](giant_commands.md) | Voice command list for the "Giant" Loadmaster. |
| 8b | [llm_tools.md](llm_tools.md) | OpenAI tool schemas, system prompt template, context snapshot format, and dispatch flow. |
| 9 | [settings.md](settings.md) | All user-configurable options — STT engine, PTT binds, mic, plugins, appearance. |

## Reference data files

| Path | Contents |
|------|----------|
| `data/schema.sql` | SQLite DDL for all tables |
| `data/seed_stations.json` | Stanton + Pyro placeholder stations and gateway entries |
| `data/seed_commodities.json` | Initial commodity list |
| `data/seed_c2.json` | C2 Hercules ship + zone definitions |
| `data/scu_boxes.json` | SCU pallet footprints (1, 2, 4, 8, 16, 24, 32) |
| `data/ships.json` | Raw cargo grid data for 88 ships (reference only; only C2 is wired up) |
| `theme/colors.json` | Brand color palette and role assignments |

## Out of scope (for v1)

- Multi-user / shared trip sessions (single-PC only)
- Cloud sync
- Live game state ingestion (no public RSI API for that)
- Ships other than the C2 Hercules
- Pyro stations beyond the gateway placeholder
