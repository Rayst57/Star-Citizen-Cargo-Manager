"""
Seed data loader — populates reference tables on first launch.

Reads data/seed_stations.json, data/seed_commodities.json, and the
per-ship seed files (seed_c2.json, seed_starlancer.json, ...) then
inserts into the DB via parameterised queries.

Idempotent: INSERT OR IGNORE keeps re-runs safe if called more than once.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_DATA_DIR = _REPO_ROOT / "data"


def _load_json(filename: str) -> dict:
    return json.loads((_DATA_DIR / filename).read_text(encoding="utf-8"))


# ── Stations ──────────────────────────────────────────────────────────────

def load_stations(conn: sqlite3.Connection) -> None:
    data = _load_json("seed_stations.json")

    # Systems
    system_id: dict[str, int] = {}
    for sys in data["systems"]:
        cur = conn.execute(
            "INSERT OR IGNORE INTO systems (name, is_active, notes) VALUES (?, ?, ?)",
            (sys["name"], sys.get("is_active", 1), sys.get("notes")),
        )
        row = conn.execute(
            "SELECT id FROM systems WHERE name = ?", (sys["name"],)
        ).fetchone()
        system_id[sys["name"]] = row["id"]

    # Stations
    station_id: dict[tuple[str, str], int] = {}  # (system_name, station_name) → id
    for st in data["stations"]:
        sys_name = st["system"]
        sid = system_id[sys_name]
        conn.execute(
            """
            INSERT OR IGNORE INTO stations
                (system_id, name, parent_body, station_type,
                 is_gateway, sort_order, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                sid,
                st["name"],
                st.get("parent_body"),
                st.get("station_type"),
                st.get("is_gateway", 0),
                st.get("sort_order"),
            ),
        )
        row = conn.execute(
            "SELECT id FROM stations WHERE system_id = ? AND name = ?",
            (sid, st["name"]),
        ).fetchone()
        station_id[(sys_name, st["name"])] = row["id"]

        # Aliases
        for alias in st.get("aliases", []):
            conn.execute(
                "INSERT OR IGNORE INTO station_aliases (station_id, alias) VALUES (?, ?)",
                (row["id"], alias),
            )

    # Jump gates
    for jg in data.get("jump_gates", []):
        fg = jg["from_gateway"]
        tg = jg["to_gateway"]
        from_id = station_id.get((fg["system"], fg["name"]))
        to_id = station_id.get((tg["system"], tg["name"]))
        if from_id and to_id:
            conn.execute(
                """
                INSERT OR IGNORE INTO jump_gates
                    (from_gateway_id, to_gateway_id, distance_km, notes)
                VALUES (?, ?, ?, ?)
                """,
                (from_id, to_id, jg.get("distance_km"), jg.get("notes")),
            )

    conn.commit()


# ── Commodities ───────────────────────────────────────────────────────────

def load_commodities(conn: sqlite3.Connection) -> None:
    data = _load_json("seed_commodities.json")

    for c in data["commodities"]:
        conn.execute(
            """
            INSERT OR IGNORE INTO commodities
                (name, category, legality, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (c["name"], c.get("category"), c.get("legality", "Legal")),
        )
        row = conn.execute(
            "SELECT id FROM commodities WHERE name = ?", (c["name"],)
        ).fetchone()
        for alias in c.get("aliases", []):
            conn.execute(
                "INSERT OR IGNORE INTO commodity_aliases (commodity_id, alias) VALUES (?, ?)",
                (row["id"], alias),
            )

    conn.commit()


# ── Ships + zones ──────────────────────────────────────────────────────────

def load_ship(conn: sqlite3.Connection, filename: str) -> None:
    """Load a single ship definition (ship row + its zones) from a
    seed JSON file. Idempotent — INSERT OR IGNORE keeps re-runs safe."""
    data = _load_json(filename)
    ship = data["ship"]

    conn.execute(
        """
        INSERT OR IGNORE INTO ships
            (name, manufacturer, total_scu, is_active)
        VALUES (?, ?, ?, ?)
        """,
        (ship["name"], ship.get("manufacturer"), ship["total_scu"], ship.get("is_active", 1)),
    )
    row = conn.execute(
        "SELECT id FROM ships WHERE name = ?", (ship["name"],)
    ).fetchone()
    ship_id = row["id"]

    for z in data["zones"]:
        conn.execute(
            """
            INSERT OR IGNORE INTO ship_zones
                (ship_id, zone_label, bay_label, zone_type,
                 width_units, length_units, height_units,
                 cube_offset_x, cube_offset_y,
                 scu_capacity, load_order, unload_priority,
                 left_zone_label, right_zone_label,
                 ramp_side, ship_forward_y, notes)
            VALUES (?, ?, ?, 'STRUCTURED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ship_id,
                z["zone_label"],
                z["bay_label"],
                z["width_units"],
                z["length_units"],
                z["height_units"],
                z["cube_offset_x"],
                z.get("cube_offset_y", 0),
                z["scu_capacity"],
                z.get("load_order"),
                z.get("unload_priority"),
                z.get("left_zone_label"),
                z.get("right_zone_label"),
                z.get("ramp_side", "low_y"),
                z.get("ship_forward_y", "high"),
                z.get("notes"),
            ),
        )

    conn.commit()


# ── Default app settings ──────────────────────────────────────────────────

def load_default_settings(conn: sqlite3.Connection) -> None:
    defaults = {
        "stt_engine": "openai",
        "listening_mode": "wake_word",
        "wake_phrase": "Hey Giant",
        "tts_enabled": "0",
        "theme": "default",
        "window_opacity": "1.0",
        "always_on_top": "0",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


# ── Entry point ───────────────────────────────────────────────────────────

_SHIP_SEEDS = ("seed_c2.json", "seed_starlancer.json")


def load_all_seeds(conn: sqlite3.Connection) -> None:
    load_stations(conn)
    load_commodities(conn)
    for ship_file in _SHIP_SEEDS:
        load_ship(conn, ship_file)
    load_default_settings(conn)
