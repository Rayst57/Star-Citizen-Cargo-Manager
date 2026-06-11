#!/usr/bin/env python
"""Regenerate data/seed_stations.json from the live UEX catalog.

Build-time companion to src/db/uex_sync.py (which handles RUNTIME
sync on app startup): pulls the same catalog and merges any unknown
locations into the seed JSON so a fresh install gets the full list
even without network access on first boot.

Existing seed entries are never removed or renamed — they're matched
by name/alias and left alone (parent_body backfilled when missing).

Usage:  python scripts/refresh_seed_stations.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from src.db.uex_sync import fetch_uex_catalog  # noqa: E402

SEED = _REPO / "data" / "seed_stations.json"


def main() -> int:
    catalog = fetch_uex_catalog(timeout=15.0)
    if catalog is None:
        print("UEX API unreachable — seed left untouched.")
        return 1

    seed = json.loads(SEED.read_text(encoding="utf-8"))

    # Systems: ensure every live UEX system is present.
    known_systems = {s["name"] for s in seed["systems"]}
    for sys_name in catalog["systems"]:
        if sys_name not in known_systems:
            seed["systems"].append({"name": sys_name, "is_active": 1})
            print(f"+ system {sys_name}")
    # Drop the stale Pyro placeholder note if it's there.
    for s in seed["systems"]:
        if s["name"] == "Pyro" and "placeholder" in (s.get("notes") or ""):
            s.pop("notes", None)

    # Index existing stations by (system, lowercased name-or-alias).
    index: dict[tuple[str, str], dict] = {}
    for st in seed["stations"]:
        index[(st["system"], st["name"].lower())] = st
        for a in st.get("aliases", []):
            index.setdefault((st["system"], a.lower()), st)

    # Sort bands per system for appended stations.
    next_sort: dict[str, int] = {}
    for st in seed["stations"]:
        sysname = st["system"]
        next_sort[sysname] = max(
            next_sort.get(sysname, 0), (st.get("sort_order") or 0)
        )
    for sysname in catalog["systems"]:
        next_sort[sysname] = next_sort.get(sysname, 0) + 10

    added = backfilled = 0
    for loc in catalog["locations"]:
        key = (loc["system"], loc["name"].lower())
        match = index.get(key)
        if match is None:
            for a in loc["aliases"]:
                match = index.get((loc["system"], a.lower()))
                if match is not None:
                    break
        if match is not None:
            if not match.get("parent_body") and loc["parent_body"]:
                match["parent_body"] = loc["parent_body"]
                backfilled += 1
            for a in loc["aliases"]:
                match.setdefault("aliases", [])
                if a not in match["aliases"]:
                    match["aliases"].append(a)
            continue

        entry = {
            "system": loc["system"],
            "name": loc["name"],
            "parent_body": loc["parent_body"],
            "station_type": loc["station_type"],
            "is_gateway": loc["is_gateway"],
            "sort_order": next_sort[loc["system"]],
        }
        if loc["aliases"]:
            entry["aliases"] = loc["aliases"]
        next_sort[loc["system"]] += 10
        seed["stations"].append(entry)
        index[key] = entry
        added += 1
        print(f"+ [{loc['system']}] {loc['name']}  (parent={loc['parent_body']})")

    seed["_meta"]["as_of"] = f"UEX sync {date.today().isoformat()}"
    seed["_meta"]["station_count"] = len(seed["stations"])
    seed["_meta"]["scope"] = (
        "Stanton + Pyro + Nyx, merged from UEX API; "
        "runtime startup sync keeps existing DBs current."
    )

    SEED.write_text(
        json.dumps(seed, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"\nSeed updated: {added} added, {backfilled} parent_body "
        f"backfilled, {len(seed['stations'])} total stations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
