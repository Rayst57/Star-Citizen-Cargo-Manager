# UI Component Specification — PySide6

This document is the implementation spec for the Windows desktop UI.
It translates `ui_interactions.md` (design intent) into an exact PySide6
widget tree, rendering strategy, signal/slot map, and module layout.

---

## Technology decisions

| Concern | Choice | Reason |
|---------|--------|--------|
| UI framework | PySide6 (Qt6) | Native Windows rendering, mature drag-drop, QPainter canvas |
| Bay visualization | Custom `QWidget` + `QPainter` | Full pixel control; no QGraphicsScene overhead |
| Styling | QSS stylesheet generated from `theme/colors.json` | Single source of truth for colors |
| Threading | `QThread` for recompute + voice | Keeps UI responsive during long ops |
| App/planner bridge | `AppController` class | UI widgets never call planner directly |

---

## Module file structure

```
src/
├── app_controller.py        # Business logic bridge (UI ↔ planner ↔ DB)
└── ui/
    ├── __init__.py
    ├── app.py               # QApplication setup + launch sequence
    ├── main_window.py       # QMainWindow — layout and signal routing hub
    ├── workday_screen.py    # Start/Resume dialog shown before main window
    ├── style.py             # Loads theme/colors.json → QSS string
    ├── panels/
    │   ├── __init__.py
    │   ├── contracts_panel.py   # Left pane
    │   ├── bay_canvas.py        # Center — top-down C2 visualization
    │   └── route_panel.py       # Right pane
    ├── dialogs/
    │   ├── __init__.py
    │   ├── add_contract.py      # Add / Edit contract dialog
    │   ├── load_view.py         # Per-stop "View Load" modal
    │   └── settings_dialog.py   # Settings dialog
    └── widgets/
        ├── __init__.py
        ├── contract_card.py     # Single contract card (used in contracts_panel)
        ├── stop_card.py         # Single route stop card (used in route_panel)
        ├── recompute_banner.py  # Dirty-flag warning bar
        └── status_bar.py        # Custom status bar
```

---

## Launch sequence

```
main.py
  └─ QApplication()
  └─ db.init_db.initialize_database()        # create/open DB
  └─ AppController(conn)                     # load settings, check workday
  └─ WorkdayScreen.exec()                    # modal: Resume or New Day
       if accepted:
  └─ MainWindow(controller).show()
```

`WorkdayScreen` is a `QDialog`. It blocks until the user picks Resume or
Start New. On accept, `AppController` has an active `workday_id` and the
main window can open.

---

## WorkdayScreen (QDialog)

```
WorkdayScreen (QDialog, modal)
├── QLabel  — app title "Star Citizen Cargo Manager"
├── QLabel  — subtitle "Giant Loadmaster"
├── QFrame  — card: Resume Workday
│   ├── QLabel  — last workday date + origin
│   └── QPushButton  "Resume Workday"   [visible only if open workday exists]
├── QFrame  — card: Start New Workday
│   ├── QComboBox   origin_station      (searchable; all active stations)
│   ├── QComboBox   final_destination   (optional; "(none)" default)
│   ├── QCheckBox   round_robin
│   └── QPushButton "Start"
└── QPushButton  "Settings"             (opens SettingsDialog)
```

Signals:
- `resume_clicked`  → `AppController.resume_workday()`
- `start_clicked(origin_id, final_id, round_robin)` → `AppController.start_workday(...)`

---

## MainWindow (QMainWindow)

```
MainWindow
├── menuBar()        — File | Help (minimal; most actions are in-panel)
├── centralWidget()  — QWidget with QHBoxLayout(spacing=4)
│   ├── ContractsPanel    (fixed width ~280px)
│   ├── BayCanvas         (stretch=2)
│   └── RoutePanel        (fixed width ~320px)
├── RecomputeBanner       — QWidget docked at bottom of central area
│                           hidden when plan_dirty=0
└── statusBar()      — replaced by StatusBarWidget
```

`MainWindow` owns the `AppController` reference and wires all inter-panel
signals. Panels never talk to each other directly.

---

## ContractsPanel (QWidget)

```
ContractsPanel
└── QVBoxLayout
    ├── QHBoxLayout  — header row
    │   ├── QLabel  "CONTRACTS"
    │   └── QPushButton  "+ Add"
    ├── QScrollArea
    │   └── QWidget  (contracts_list_widget)
    │       └── QVBoxLayout
    │           ├── ContractCard  (one per contract, dynamically added)
    │           └── … (stretch at bottom)
    └── QLabel  scu_summary  "357 / 696 SCU"
```

### ContractCard (QFrame)

```
ContractCard
└── QHBoxLayout
    ├── QVBoxLayout  — left: contract info
    │   ├── QLabel  "#1  Yellow Core → Everus + Baijini"
    │   └── QLabel  "55 SCU Tungsten, max 8"  (muted style)
    ├── QPushButton  "✎"   edit button
    └── QPushButton  "✕"   remove button
```

Background color changes when this contract has an active conflict:
light amber tint.

Signals emitted by `ContractsPanel`:
- `add_requested` → opens `AddContractDialog`
- `edit_requested(contract_id)` → opens `AddContractDialog` pre-filled
- `remove_requested(contract_id)` → `AppController.remove_contract()`
- `contract_selected(contract_id)` → `BayCanvas.highlight_contract()`

---

## AddContractDialog (QDialog)

```
AddContractDialog
└── QFormLayout
    ├── pickup_station   QComboBox  (filterable; all active stations)
    ├── ── cargo lines section ──
    │   ├── delivery_station  QComboBox
    │   ├── commodity         QComboBox  (filterable)
    │   ├── scu_amount        QSpinBox   (1–696)
    │   ├── max_pallet_size   QComboBox  (1,2,4,8,16,24,32)
    │   └── QPushButton "+ Add line"   (adds another delivery row)
    ├── parsed_text      QLabel  (shows voice transcript if voice-triggered)
    └── QDialogButtonBox  OK / Cancel
```

One contract can have multiple cargo lines (multiple deliveries from the
same pickup). "+ Add line" appends another delivery_station / commodity /
scu_amount / max_pallet_size row.

On OK: `AppController.add_contract(data)` → sets `plan_dirty=1` → banner
shows.

---

## BayCanvas (QWidget)

The cargo bay visualization. Uses `QPainter` in `paintEvent()`.
No QGraphicsScene.

### Layout

```
BayCanvas
└── QVBoxLayout
    ├── QHBoxLayout  — zone dropdowns (F1 F2 F3 | spacer | R1 R2 R3 R4)
    │   └── QComboBox × 7  (one per zone; items: "(auto)" + active destinations)
    ├── BayCanvasViewport (custom QWidget — owns paintEvent + drag/drop)
    └── QHBoxLayout  — SCU totals
        ├── QLabel  "Forward: 216 / 216 SCU"
        └── QLabel  "Rear: 141 / 480 SCU"
```

### BayCanvasViewport — rendering

**Coordinate system:**
- Each 1.25 m cube = one "cell unit" in canvas space.
- Scale factor: `cell_px` (e.g. 12 px per cell unit), computed from
  available widget size at resize.
- Forward bay: 6 cells wide × 9 cells deep, drawn left side of viewport.
- Rear bay: 8 cells wide × 15 cells deep, drawn right side.
- A margin gap of ~16 px separates the two bays.

**`paintEvent()` drawing order:**
1. Background fill (deep navy `#212e67`).
2. Bay outlines (white 1px rectangle).
3. Column divider dotted lines (every 2 cells = one zone boundary).
4. Zone labels (F1/F2/F3 above forward bay; R1-R4 above rear bay).
5. Pallet rectangles — width/length in cell units, colored by
   `stations.color_hex` of the delivery destination.
6. Conflicted pallets: red border (2px) + diagonal stripe pattern
   (drawn with `QPen` + `QBrush` hatch).
7. Ramp direction arrow (▶) at bottom of rear bay.
8. Drag preview if a drag is in progress (translucent pallet rectangle
   following the cursor).

**Pallet placement data:**
`BayCanvasViewport` receives a list of `PalletRect` objects from the
controller after each recompute:

```python
@dataclass
class PalletRect:
    cargo_line_id: int
    zone_label: str       # 'R1', 'F2', etc.
    bay: str              # 'forward' | 'rear'
    cell_x: int           # origin within bay (0 = port edge)
    cell_y: int           # origin within bay (0 = ramp/forward edge)
    cell_w: int           # pallet width in cells
    cell_l: int           # pallet length in cells
    cell_h: int           # pallet height in cells (1 or 2)
    color: str            # hex color of delivery destination
    is_conflicted: bool
    label: str            # short label shown inside pallet rectangle
```

**Drag and drop:**
- `mousePressEvent` — hit-test which `PalletRect` was clicked.
- `mouseMoveEvent` — if dragging, update preview position; highlight
  valid drop zones green, invalid zones red.
- `mouseReleaseEvent` — on valid drop: call
  `AppController.move_cargo(cargo_line_id, target_zone)` which sets
  `is_manual_override=1` in `zone_assignments` and sets `plan_dirty=1`.
- Drag is only within the same bay for v1 (no forward↔rear drag).

**Zone dropdown interaction:**
When a zone's `QComboBox` changes from "(auto)" to a destination:
`AppController.set_zone_destination(zone_label, station_id)` → `plan_dirty=1`.

---

## RoutePanel (QWidget)

```
RoutePanel
└── QVBoxLayout
    ├── QHBoxLayout  — header
    │   ├── QLabel  "ROUTE"
    │   └── QPushButton  "↻ Recompute"
    ├── QScrollArea
    │   └── QWidget  route_list_widget
    │       └── QVBoxLayout
    │           ├── StopCard  (one per route stop, dynamically populated)
    │           └── … (stretch)
    └── (empty when no contracts)
```

### StopCard (QFrame)

```
StopCard
└── QVBoxLayout
    ├── QHBoxLayout  — header row
    │   ├── QLabel  "Stop 3"              (bold, accent color)
    │   ├── QLabel  "Beautiful Glen"
    │   ├── QLabel  "— Unload"
    │   └── QPushButton  "View Load →"
    ├── QLabel  unload_summary  "R1: 55 SCU Tungsten → Everus"
    ├── QLabel  load_summary    "None"
    └── QLabel  conflict_note   "⚠ CONFLICT: …"  (hidden when no conflict)
```

Clicking "View Load" opens `LoadViewModal` for this stop number.

---

## LoadViewModal (QDialog)

```
LoadViewModal (QDialog, non-modal — stays open while user navigates)
├── QHBoxLayout  — header
│   ├── QLabel  "Stop 3 — Beautiful Glen — Unload"
│   └── QPushButton  "✕" close
├── LegendWidget  — color key + zone labels
│   └── QHBoxLayout
│       ├── zone label chips (F1, F2, F3, R1–R4 colored boxes)
│       └── destination color swatches (auto-populated from active destinations)
├── BayCanvasViewport (read-only, same widget class as main bay canvas)
│   shows cargo state AT this stop (after unload, before load)
├── QHBoxLayout  — navigation
│   ├── QPushButton  "← Prev"
│   ├── QLabel  "Stop 3 of 11"
│   └── QPushButton  "Next →"
└── (no prose in body — visual only per ui_interactions.md)
```

`LoadViewModal` is given a reference to the `Snapshot` dict from the last
recompute. Prev/Next updates the canvas data without closing the dialog.

---

## RecomputeBanner (QWidget)

```
RecomputeBanner
└── QHBoxLayout
    ├── QLabel  "⚠  Modifications made — recompute required"
    └── QPushButton  "Recompute"
```

Styling: accent yellow (`#ffbe20`) text on deep navy (`#212e67`) background.
Full-width, docked between the central widget and the status bar.

`setVisible(plan_dirty)` — toggled by `AppController` whenever `plan_dirty`
changes.

When `QPushButton` clicked: `AppController.recompute()` → runs in `QThread`
→ emits `recompute_done(result)` → all panels refresh.

---

## StatusBarWidget (QWidget used as status bar replacement)

```
StatusBarWidget
└── QHBoxLayout
    ├── QLabel  mic_icon    "🎤 listening" / "🎤 muted" / "🎤 cancelled"
    ├── QFrame  vertical separator
    ├── QLabel  stop_progress  "Stop 3 of 11"
    ├── QFrame  vertical separator
    ├── QLabel  scu_usage       "357 / 696 SCU"
    ├── (stretch)
    ├── QLabel  api_health      "●" (green = OpenAI reachable, red = not)
    └── QPushButton  "⚙"        opens SettingsDialog
```

---

## SettingsDialog (QDialog)

Tabbed dialog. Tabs match `docs/settings.md` sections.

```
SettingsDialog (QDialog)
└── QTabWidget
    ├── Tab "Voice Input"
    │   ├── QComboBox  listening_mode  (Wake word / PTT / Toggle / Hybrid)
    │   ├── PTT bind section
    │   │   └── QPushButton  "Press input to bind"  (capture dialog)
    │   └── QComboBox  stt_engine  (OpenAI / Windows Speech)
    ├── Tab "Microphone"
    │   ├── QComboBox  input_device
    │   ├── QSlider    input_gain
    │   └── QCheckBox  noise_suppression
    ├── Tab "Wake Word"
    │   └── QSlider    sensitivity
    ├── Tab "OpenAI"
    │   ├── QLineEdit  api_key  (password echo mode; saves to Windows Credential Manager)
    │   ├── QComboBox  model
    │   └── QCheckBox  use_realtime_api
    ├── Tab "Appearance"
    │   ├── QSlider    window_opacity
    │   └── QCheckBox  always_on_top
    └── QDialogButtonBox  Save / Cancel
```

---

## AppController

The single business-logic bridge. UI widgets hold a reference to it but
never import from `src/planner/` directly.

```python
class AppController(QObject):
    # ── signals (emitted to update UI) ───────────────────────────────────
    contracts_changed   = Signal()          # refresh ContractsPanel
    route_changed       = Signal()          # refresh RoutePanel + BayCanvas
    plan_dirty_changed  = Signal(bool)      # show/hide RecomputeBanner
    recompute_started   = Signal()          # show spinner
    recompute_done      = Signal(object)    # RecomputeResult → update everything
    stop_progress       = Signal(int, int)  # current_stop, total_stops
    scu_usage           = Signal(int, int)  # used_scu, total_scu
    mic_state_changed   = Signal(str)       # 'listening'|'muted'|'cancelled'
    api_health_changed  = Signal(bool)      # True = OpenAI reachable

    # ── public methods (called by UI widgets) ────────────────────────────
    def resume_workday(self) -> None: …
    def start_workday(self, origin_id, final_id, round_robin) -> None: …
    def end_workday(self) -> None: …

    def add_contract(self, data: dict) -> int: …       # returns contract_id
    def edit_contract(self, contract_id, data) -> None: …
    def remove_contract(self, contract_id) -> None: …
    def list_contracts(self) -> list[dict]: …

    def set_zone_destination(self, zone_label, station_id) -> None: …
    def move_cargo(self, cargo_line_id, target_zone) -> None: …

    def recompute(self) -> None: …             # spawns RecomputeThread
    def get_pallet_rects(self) -> list: …      # current BayCanvas data
    def get_snapshot(self, stop_number) -> list: …  # for LoadViewModal

    def complete_stop(self) -> None: …
    def skip_stop(self, station_id) -> None: …

    def save_settings(self, data: dict) -> None: …
    def load_settings(self) -> dict: …
```

All planner functions (`recompute.recompute`, `build_simple_route`, etc.)
are called only from within `AppController`.

---

## Threading model

| Thread | Work | Communication |
|--------|------|---------------|
| Main (Qt) | All UI updates | — |
| `RecomputeThread(QThread)` | `recompute.recompute()` | emits `recompute_done(result)` |
| `VoiceThread(QThread)` | Mic capture, wake word, STT | emits `transcript_ready(text)` |
| `LLMThread(QThread)` | OpenAI API call + tool dispatch | emits `tool_called(name, args)` |

All cross-thread communication is via Qt signals (thread-safe). No shared
mutable state between threads.

---

## Styling

`src/ui/style.py` generates a QSS string from `theme/colors.json`:

```python
COLORS = {
    "background": "#212e67",
    "primary":    "#00498f",
    "accent":     "#ffbe20",
    "secondary":  "#deb447",
    "text":       "#ffffff",
    "text_muted": "#deb447",
}

QSS = f"""
QMainWindow, QDialog, QWidget {{
    background-color: {COLORS['background']};
    color: {COLORS['text']};
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QPushButton {{
    background-color: {COLORS['primary']};
    color: {COLORS['text']};
    border: none;
    border-radius: 4px;
    padding: 6px 14px;
}}
QPushButton:hover {{
    background-color: {COLORS['accent']};
    color: {COLORS['background']};
}}
RecomputeBanner {{
    background-color: {COLORS['background']};
    border-top: 2px solid {COLORS['accent']};
}}
RecomputeBanner QLabel {{
    color: {COLORS['accent']};
    font-weight: bold;
}}
…
"""
```

---

## Key design constraints (carry forward to implementation)

1. **`BayCanvasViewport` is always read-only in `LoadViewModal`** — the same
   class is instantiated with `interactive=False` to disable drag/drop.

2. **`plan_dirty=1` is set by `AppController`**, never by widgets directly.
   Widgets only call controller methods; the controller decides if something
   is dirty.

3. **Recompute is never automatic** — only triggered by the user clicking
   the button or saying "Hey Giant, recompute." The banner is the only
   notification.

4. **No data shown in `LoadViewModal` body** — only the canvas and legend.
   Per `ui_interactions.md`.

5. **Zone dropdowns revert to "(auto)" on hard reset** (new workday).
   `AppController.start_workday()` clears all manual zone overrides.

6. **`AddContractDialog` supports voice pre-fill** — when opened from a
   voice command, `parsed_transcript` is passed in and fields are
   pre-populated. User can correct before confirming.
