# Manual Entry

Voice is one of two input paths. Manual entry through the UI is fully
supported and produces the **same data structures** as voice — they are
interchangeable within a single local session.

---

## Forms / panels

### New / Edit Contract

Modal dialog launched from:
- Contracts panel "+" button
- "Edit" on an existing contract row
- After voice dictation, to confirm parsed result before commit

Fields:

| Field | Required | Notes |
|-------|----------|-------|
| Origin station | yes | Autocomplete against known Stanton/Pyro stations |
| Max pallet size | yes | Dropdown: 1, 2, 4, 8, 16, 24, 32 SCU |
| Cargo items[] | ≥1 | Sub-form, see below |

### Cargo Item sub-form

Repeating row inside the Contract dialog.

| Field | Required | Notes |
|-------|----------|-------|
| Commodity | yes | Autocomplete against UEX commodity list |
| SCU | yes | Numeric input |
| Destination | yes | Autocomplete against known stations |

`+ Add Item` button appends another row. Items can be reordered and
removed.

### Add Stop (manual route stop)

For when the user wants to insert a non-contract stop (refuel,
repair, sightseeing). Launched from the Route panel.

| Field | Required | Notes |
|-------|----------|-------|
| Station | yes | Autocomplete |
| Action | yes | Multi-select: load / unload / refuel / repair / other |
| Note | no | Free text shown on the stop card |
| Position | yes | Insert before / after existing stop, or auto-place |

### Per-Stop Manual Edits

Each stop card on the route panel has:
- **Edit unloads** — override which contracts unload here (drag/drop
  cargo items between stops)
- **Edit loads** — same, but for loads
- **Skip stop** — remove without removing the contracts that referenced it

These edits override the auto-planner's choices. The planner won't
overwrite a manual edit unless the user says "re-plan everything".

---

## Voice ↔ manual parity

Every voice command has a UI equivalent. Every UI action emits the
same internal event the voice handler would emit, so:

- The "Giant" Loadmaster chat transcript shows manual edits too (e.g.
  *"Contract added: Seraphim → Ambitious Dream, 5 SCU Processed Food"*)
- Voice and manual can be used interchangeably at any point

---

## Local persistence

All entries are saved automatically to a local SQLite database. Closing
and reopening the app shows the last unfinished plan with a "Resume"
prompt.

---

## Open questions

1. **Commodity autocomplete source** — pull from UEX live, or bundle a
   snapshot and refresh weekly?
2. **Station autocomplete coverage** — Stanton + Pyro only, or include
   future systems as CIG releases them?
3. **Manual edit persistence on re-plan** — should manual cargo
   placement overrides survive a route re-plan, or get reset?
