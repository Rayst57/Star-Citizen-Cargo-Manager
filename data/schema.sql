-- Star Citizen Cargo Manager — SQLite schema
-- All tables; reference seed loaded by the app on first launch.

PRAGMA foreign_keys = ON;

-- =========================================================
-- Reference data
-- =========================================================

CREATE TABLE systems (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    is_active   INTEGER NOT NULL DEFAULT 1,
    notes       TEXT
);

CREATE TABLE stations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    system_id       INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    parent_body     TEXT,                 -- e.g. 'Crusader', 'Hurston', 'CRU-L1'
    station_type    TEXT,                 -- 'orbital', 'lagrange', 'gateway', 'surface', 'rest_stop'
    is_gateway      INTEGER NOT NULL DEFAULT 0,
    color_hex       TEXT,                 -- assigned color for visualization, e.g. '#00498f'
    sort_order      INTEGER,
    is_active       INTEGER NOT NULL DEFAULT 1,
    notes           TEXT,
    UNIQUE (system_id, name)
);

CREATE INDEX idx_stations_system ON stations(system_id);
CREATE INDEX idx_stations_gateway ON stations(is_gateway) WHERE is_gateway = 1;

CREATE TABLE station_aliases (
    station_id  INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    alias       TEXT    NOT NULL,
    PRIMARY KEY (station_id, alias)
);

CREATE INDEX idx_station_aliases_alias ON station_aliases(alias);

-- Distances within a single system. Both endpoints must share a system_id;
-- enforced by triggers below.
CREATE TABLE station_distances (
    from_station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    to_station_id   INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    distance_km     REAL,
    notes           TEXT,
    PRIMARY KEY (from_station_id, to_station_id),
    CHECK (from_station_id <> to_station_id)
);

-- Trigger: enforce both endpoints are in the same system
CREATE TRIGGER trg_station_distances_same_system
BEFORE INSERT ON station_distances
FOR EACH ROW
WHEN (SELECT system_id FROM stations WHERE id = NEW.from_station_id)
  <> (SELECT system_id FROM stations WHERE id = NEW.to_station_id)
BEGIN
    SELECT RAISE(ABORT, 'station_distances: both endpoints must share a system_id');
END;

-- Cross-system jumps via gateways. Both endpoints must be gateway stations.
CREATE TABLE jump_gates (
    from_gateway_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    to_gateway_id   INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    distance_km     REAL,
    notes           TEXT,
    PRIMARY KEY (from_gateway_id, to_gateway_id),
    CHECK (from_gateway_id <> to_gateway_id)
);

CREATE TRIGGER trg_jump_gates_must_be_gateway
BEFORE INSERT ON jump_gates
FOR EACH ROW
WHEN (SELECT is_gateway FROM stations WHERE id = NEW.from_gateway_id) = 0
  OR (SELECT is_gateway FROM stations WHERE id = NEW.to_gateway_id) = 0
BEGIN
    SELECT RAISE(ABORT, 'jump_gates: both endpoints must be gateway stations (is_gateway = 1)');
END;

CREATE TABLE commodities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    category    TEXT,
    legality    TEXT,                     -- 'Legal', 'Illegal'
    is_active   INTEGER NOT NULL DEFAULT 1,
    notes       TEXT
);

CREATE TABLE commodity_aliases (
    commodity_id    INTEGER NOT NULL REFERENCES commodities(id) ON DELETE CASCADE,
    alias           TEXT    NOT NULL,
    PRIMARY KEY (commodity_id, alias)
);

CREATE INDEX idx_commodity_aliases_alias ON commodity_aliases(alias);

CREATE TABLE ships (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    manufacturer    TEXT,
    total_scu       INTEGER NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    notes           TEXT
);

CREATE TABLE ship_zones (
    -- Operational column definitions for cockpit instructions.
    -- For C2: F1/F2/F3 (forward bay) + R1/R2/R3/R4 (rear bay).
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ship_id             INTEGER NOT NULL REFERENCES ships(id) ON DELETE CASCADE,
    zone_label          TEXT    NOT NULL,
    bay_label           TEXT    NOT NULL,                -- 'forward', 'rear'
    zone_type           TEXT    NOT NULL DEFAULT 'STRUCTURED',
    width_units         INTEGER NOT NULL,                -- in 1.25m cubes
    length_units        INTEGER NOT NULL,
    height_units        INTEGER NOT NULL,
    cube_offset_x       INTEGER NOT NULL,                -- starting x in the bay (0 = port edge)
    cube_offset_y       INTEGER NOT NULL DEFAULT 0,      -- starting y in the bay (0 = ramp edge)
    scu_capacity        INTEGER NOT NULL,
    load_order          INTEGER,
    unload_priority     INTEGER,
    left_zone_label     TEXT,
    right_zone_label    TEXT,
    -- Front/back adjacency: for ships where two zones share an open
    -- bay (no bulkhead between them, so pallets could physically slide
    -- between them or the renderer should draw them seamlessly).
    -- Example: RSI Hermes has F1<->R1 as one continuous column.
    -- NULL means a structural separation (C2, Starlancer).
    front_zone_label    TEXT,
    back_zone_label     TEXT,
    -- Which Y end of THIS zone's bay is the loading ramp.
    -- 'low_y'  = ramp at Y=0 (current C2 default for both bays)
    -- 'high_y' = ramp at Y=length-1 (e.g. nose-only loaders)
    -- 'none'   = sealed bay; loaded indirectly
    -- Metadata-only — the planner already treats Y=0 as ramp; this column
    -- exists so future ship layouts (nose-only, side-loaders, etc.) can
    -- declare ramp geometry without code changes.
    ramp_side           TEXT    NOT NULL DEFAULT 'low_y',
    -- Which local-Y direction points toward the SHIP's forward (nose) end.
    -- Renderers always show ship-forward at the top of the screen
    -- (rear-view, top-down — the standard aircraft diagram convention),
    -- so this drives whether a bay is drawn flipped vertically.
    --   'high'  = high local-Y is forward (e.g. C2 R-bay: Y=0 at the
    --             rear ramp, length-1 toward cockpit). NOT flipped.
    --   'low'   = low local-Y is forward (e.g. C2 F-bay: Y=0 at the
    --             nose ramp, length-1 toward ship interior). FLIPPED.
    ship_forward_y      TEXT    NOT NULL DEFAULT 'high',
    notes               TEXT,
    UNIQUE (ship_id, zone_label)
);

-- =========================================================
-- Workday / runtime state
-- =========================================================

CREATE TABLE workdays (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at                  TEXT    NOT NULL,
    ended_at                    TEXT,
    ship_id                     INTEGER NOT NULL REFERENCES ships(id),
    origin_station_id           INTEGER NOT NULL REFERENCES stations(id),
    final_destination_station_id INTEGER REFERENCES stations(id),  -- nullable; round_robin overrides
    round_robin                 INTEGER NOT NULL DEFAULT 0,
    plan_dirty                  INTEGER NOT NULL DEFAULT 0,        -- 1 = recompute required
    last_computed_at            TEXT,
    notes                       TEXT
);

CREATE INDEX idx_workdays_active ON workdays(ended_at) WHERE ended_at IS NULL;

CREATE TABLE contracts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    workday_id          INTEGER NOT NULL REFERENCES workdays(id) ON DELETE CASCADE,
    contract_number     INTEGER NOT NULL,                    -- user-facing 1, 2, 3...
    pickup_station_id   INTEGER NOT NULL REFERENCES stations(id),
    max_pallet_size     INTEGER NOT NULL CHECK (max_pallet_size IN (1, 2, 4, 8, 16, 24, 32)),
    parsed_confidence   REAL,
    status              TEXT    NOT NULL DEFAULT 'pending',  -- 'pending', 'in_progress', 'complete'
    notes               TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (workday_id, contract_number)
);

CREATE TABLE cargo_lines (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id             INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    line_number             INTEGER NOT NULL,
    delivery_station_id     INTEGER NOT NULL REFERENCES stations(id),
    commodity_id            INTEGER NOT NULL REFERENCES commodities(id),
    scu_amount              INTEGER NOT NULL CHECK (scu_amount > 0),
    notes                   TEXT,
    UNIQUE (contract_id, line_number)
);

-- Computed plan output

CREATE TABLE route_stops (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workday_id      INTEGER NOT NULL REFERENCES workdays(id) ON DELETE CASCADE,
    stop_number     INTEGER NOT NULL,
    station_id      INTEGER NOT NULL REFERENCES stations(id),
    action          TEXT    NOT NULL,           -- 'Depart', 'Arrive', 'Load', 'Unload', 'Final unload', 'Refuel', 'Salvage pickup', 'Emergency reroute'
    notes           TEXT,
    UNIQUE (workday_id, stop_number)
);

CREATE TABLE zone_assignments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    workday_id          INTEGER NOT NULL REFERENCES workdays(id) ON DELETE CASCADE,
    cargo_line_id       INTEGER NOT NULL REFERENCES cargo_lines(id) ON DELETE CASCADE,
    primary_zone_label  TEXT    NOT NULL,        -- 'R1', 'F2', etc.
    spans_zones         TEXT,                    -- JSON array if pallet spans columns: ["R1","R2"]
    pallet_breakdown    TEXT,                    -- e.g. '8 + 8 + 4 + 1'
    cube_x              INTEGER,                 -- placement origin (port edge)
    cube_y              INTEGER,                 -- placement origin (ramp edge)
    cube_z              INTEGER,                 -- placement origin (floor)
    is_manual_override  INTEGER NOT NULL DEFAULT 0,
    notes               TEXT
);

CREATE TABLE pallet_conflicts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    workday_id          INTEGER NOT NULL REFERENCES workdays(id) ON DELETE CASCADE,
    conflict_group_id   INTEGER NOT NULL,        -- groups conflict participants
    cargo_line_id       INTEGER NOT NULL REFERENCES cargo_lines(id) ON DELETE CASCADE,
    pickup_station_id   INTEGER NOT NULL REFERENCES stations(id),
    ambiguous_sizes     TEXT,                    -- JSON: pallet sizes shared between groups, e.g. [2, 1]
    unique_sizes        TEXT,                    -- JSON: pallet sizes unique to this destination
    status              TEXT NOT NULL DEFAULT 'active',  -- 'active', 'staged', 'resolving', 'resolved', 'cleared', 'unresolved'
    handling_zone_label TEXT,
    notes               TEXT
);

CREATE TABLE validation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workday_id      INTEGER REFERENCES workdays(id) ON DELETE CASCADE,
    timestamp       TEXT    NOT NULL,
    severity        TEXT    NOT NULL CHECK (severity IN ('INFO', 'WARN', 'ERROR')),
    source          TEXT,                       -- 'parser', 'planner', 'palletizer', etc.
    message         TEXT    NOT NULL,
    suggested_fix   TEXT,
    cargo_line_id   INTEGER REFERENCES cargo_lines(id) ON DELETE SET NULL
);

-- =========================================================
-- Settings (key/value)
-- =========================================================

CREATE TABLE app_settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
