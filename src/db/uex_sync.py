"""
UEX station-registry sync.

On startup the app calls ``sync_stations_from_uex(conn)``, which pulls
the live location catalog from the UEX Corp API (https://uexcorp.space),
diffs it against the local ``stations`` table, and upserts anything new
— so a fresh game patch's stations show up without shipping a new app
build. The whole catalog is four GET requests totalling ~1.5 s; offline
or slow networks fail soft (the app continues on the local DB).

Mapped UEX endpoints:
    /star_systems    → systems            (is_available == 1 only)
    /space_stations  → stations           (orbital / lagrange / gateway)
    /cities          → stations           (major_location)
    /outposts        → stations           (outpost)

Matching: a UEX location matches a local station when the name equals
the station's name OR one of its aliases (within the same system).
Matched stations get their parent_body backfilled and the UEX nickname
added as an alias; unmatched ones are inserted with an auto-assigned
sort_order at the end of their system's band.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request

_log = logging.getLogger("cargo_manager")

_API_BASE = "https://api.uexcorp.space/2.0"
_TIMEOUT_S = 4.0


# ── fetch ────────────────────────────────────────────────────────────────

def _fetch_json(endpoint: str, timeout: float = _TIMEOUT_S) -> list | None:
    """GET one UEX endpoint; returns the ``data`` list or None on any
    failure (offline, HTTP error, bad JSON). Never raises."""
    url = f"{_API_BASE}/{endpoint}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "SC-Cargo-Manager"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                            # noqa: BLE001
        _log.info("uex_sync: fetch %s failed: %s", endpoint, exc)
        return None
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        _log.info("uex_sync: %s returned unexpected shape", endpoint)
        return None
    return data


def fetch_uex_catalog(timeout: float = _TIMEOUT_S) -> dict | None:
    """Fetch the full location catalog. Returns
    ``{"systems": [...], "locations": [...]}`` (locations normalized
    via :func:`normalize_location`) or None when the API is
    unreachable."""
    systems = _fetch_json("star_systems", timeout)
    if systems is None:
        return None
    live_systems = {
        s["name"] for s in systems if s.get("is_available") == 1
    }

    locations: list[dict] = []
    for endpoint, category in (
        ("space_stations", "space_station"),
        ("cities", "city"),
        ("outposts", "outpost"),
    ):
        rows = _fetch_json(endpoint, timeout)
        if rows is None:
            # Partial outage — abort rather than half-sync.
            return None
        for r in rows:
            loc = normalize_location(r, category)
            if loc is not None and loc["system"] in live_systems:
                locations.append(loc)

    return {"systems": sorted(live_systems), "locations": locations}


# ── normalization ────────────────────────────────────────────────────────

def normalize_location(row: dict, category: str) -> dict | None:
    """UEX API row → seed-shaped station dict, or None if the location
    isn't live (decommissioned / not yet available)."""
    if row.get("is_available") != 1:
        return None
    if row.get("is_decommissioned") == 1:
        return None
    name = (row.get("name") or "").strip()
    system = row.get("star_system_name")
    if not name or not system:
        return None

    parent = (
        row.get("moon_name")
        or row.get("planet_name")
        or row.get("orbit_name")
        or None
    )

    if category == "city":
        station_type = "major_location"
        is_gateway = 0
    elif category == "outpost":
        station_type = "outpost"
        is_gateway = 0
    else:  # space_station
        if row.get("is_jump_point") == 1:
            station_type = "gateway"
            is_gateway = 1
        elif row.get("is_lagrange") == 1:
            station_type = "lagrange"
            is_gateway = 0
        else:
            station_type = "orbital"
            is_gateway = 0

    aliases: list[str] = []
    nickname = (row.get("nickname") or "").strip()
    if nickname and nickname != name:
        aliases.append(nickname)

    return {
        "system": system,
        "name": name,
        "parent_body": parent,
        "station_type": station_type,
        "is_gateway": is_gateway,
        "aliases": aliases,
    }


# ── DB sync ──────────────────────────────────────────────────────────────

def sync_stations_from_uex(
    conn: sqlite3.Connection,
    catalog: dict | None = None,
    timeout: float = _TIMEOUT_S,
) -> dict:
    """Compare the local stations table to the UEX catalog and upsert.

    *catalog* may be injected (tests); by default it's fetched live.
    Returns a summary dict::

        {"ok": bool, "added": int, "updated": int, "total_remote": int}

    ``ok`` False means the API was unreachable — the DB is untouched
    and the app should continue on local data.
    """
    if catalog is None:
        catalog = fetch_uex_catalog(timeout)
    if catalog is None:
        return {"ok": False, "added": 0, "updated": 0, "total_remote": 0}

    # Ensure every live system exists locally.
    system_id: dict[str, int] = {}
    for sys_name in catalog["systems"]:
        conn.execute(
            "INSERT OR IGNORE INTO systems (name, is_active) VALUES (?, 1)",
            (sys_name,),
        )
        row = conn.execute(
            "SELECT id FROM systems WHERE name = ?", (sys_name,)
        ).fetchone()
        system_id[sys_name] = row["id"]

    # Build the local name/alias index per system.
    local_by_key: dict[tuple[int, str], int] = {}
    for r in conn.execute("SELECT id, system_id, name FROM stations"):
        local_by_key[(r["system_id"], r["name"].lower())] = r["id"]
    for r in conn.execute(
        "SELECT a.alias, s.system_id, s.id AS station_id "
        "FROM station_aliases a JOIN stations s ON s.id = a.station_id"
    ):
        local_by_key.setdefault(
            (r["system_id"], r["alias"].lower()), r["station_id"]
        )

    # Auto-sort bands: new stations append after their system's max.
    next_sort: dict[int, int] = {}
    for sid in system_id.values():
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM stations "
            "WHERE system_id = ?",
            (sid,),
        ).fetchone()
        next_sort[sid] = (row["m"] or 0) + 10

    added = updated = 0
    for loc in catalog["locations"]:
        sid = system_id.get(loc["system"])
        if sid is None:
            continue
        match_id = local_by_key.get((sid, loc["name"].lower()))
        if match_id is None:
            for alias in loc["aliases"]:
                match_id = local_by_key.get((sid, alias.lower()))
                if match_id is not None:
                    break

        if match_id is not None:
            # Backfill parent_body when we know better than the local
            # row; never blank out an existing value.
            cur = conn.execute(
                "UPDATE stations SET parent_body = ? "
                "WHERE id = ? AND parent_body IS NULL AND ? IS NOT NULL",
                (loc["parent_body"], match_id, loc["parent_body"]),
            )
            if cur.rowcount:
                updated += 1
            for alias in loc["aliases"]:
                conn.execute(
                    "INSERT OR IGNORE INTO station_aliases "
                    "(station_id, alias) VALUES (?, ?)",
                    (match_id, alias),
                )
            continue

        cur = conn.execute(
            """
            INSERT INTO stations
                (system_id, name, parent_body, station_type,
                 is_gateway, sort_order, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                sid, loc["name"], loc["parent_body"],
                loc["station_type"], loc["is_gateway"], next_sort[sid],
            ),
        )
        new_id = cur.lastrowid
        next_sort[sid] += 10
        local_by_key[(sid, loc["name"].lower())] = new_id
        for alias in loc["aliases"]:
            conn.execute(
                "INSERT OR IGNORE INTO station_aliases "
                "(station_id, alias) VALUES (?, ?)",
                (new_id, alias),
            )
        added += 1

    conn.commit()
    summary = {
        "ok": True,
        "added": added,
        "updated": updated,
        "total_remote": len(catalog["locations"]),
    }
    _log.info(
        "uex_sync: %d remote locations, %d added, %d backfilled",
        summary["total_remote"], added, updated,
    )
    return summary
