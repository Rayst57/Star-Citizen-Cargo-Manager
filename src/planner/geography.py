"""
Stanton system geography — body coordinates and station distances.

Every station in the seed carries a ``parent_body`` (planet, moon,
Lagrange point, or jump point). This module maps each body to an
approximate position on the Stanton system map so the route builder
can order stops by actual travel distance instead of the static
``sort_order`` column.

Coordinate system: kilometers in the Stanton orbital plane, star at
the origin, Z ignored (the system is essentially flat). Planet
positions are the well-known starmap values; Lagrange points are
derived geometrically from their parent planet's position:

    L1 — 85% of the way from the star to the planet
    L2 — 15% past the planet, on the same star→planet line
    L3 — the planet's orbit radius on the OPPOSITE side of the star
    L4 — 60 degrees ahead of the planet on its orbit
    L5 — 60 degrees behind the planet on its orbit

Moons are treated as co-located with their parent planet — lunar
orbit radii (tens of thousands of km) are negligible against
inter-planet distances (tens of millions of km), and stations around
the same body should sort by ``sort_order`` anyway.

Jump-point positions are rough lore-based approximations; they only
matter for gateway runs and are flagged as such.
"""

from __future__ import annotations

import logging
import math
import sqlite3

_log = logging.getLogger("cargo_manager")


# ── Planets (km, starmap values) ─────────────────────────────────────────

_PLANETS: dict[str, tuple[float, float]] = {
    "Hurston":   (12_850_457.0, 0.0),
    "Crusader":  (-18_962_176.0, -2_664_960.0),
    "ArcCorp":   (18_587_665.0, -22_151_917.0),
    "microTech": (22_462_016.0, 37_185_626.0),
}

# Moons → parent planet.
_MOONS: dict[str, str] = {
    # Hurston
    "Aberdeen": "Hurston",
    "Arial": "Hurston",
    "Ita": "Hurston",
    "Magda": "Hurston",
    # Crusader
    "Cellin": "Crusader",
    "Daymar": "Crusader",
    "Yela": "Crusader",
    # ArcCorp
    "Lyria": "ArcCorp",
    "Wala": "ArcCorp",
    # microTech
    "Calliope": "microTech",
    "Clio": "microTech",
    "Euterpe": "microTech",
}

# Lagrange-point prefixes → planet.
_LAGRANGE_PLANET: dict[str, str] = {
    "HUR": "Hurston",
    "CRU": "Crusader",
    "ARC": "ArcCorp",
    "MIC": "microTech",
}

# Jump points — approximate lore positions, only used for gateway runs.
_JUMP_POINTS: dict[str, tuple[float, float]] = {
    "Pyro Jump Point": (-25_000_000.0, 8_000_000.0),
    "Terra Jump Point": (25_000_000.0, -30_000_000.0),
    "Nyx Jump Point": (35_000_000.0, 45_000_000.0),
}


def _lagrange_position(body: str) -> tuple[float, float] | None:
    """Position for a Lagrange-point body label like ``"CRU-L1"``."""
    if len(body) < 6 or body[3] != "-" or body[4] != "L":
        return None
    planet = _LAGRANGE_PLANET.get(body[:3])
    if planet is None:
        return None
    try:
        n = int(body[5:])
    except ValueError:
        return None
    px, py = _PLANETS[planet]
    r = math.hypot(px, py)
    theta = math.atan2(py, px)
    if n == 1:
        return (px * 0.85, py * 0.85)
    if n == 2:
        return (px * 1.15, py * 1.15)
    if n == 3:
        return (-px, -py)
    if n == 4:
        t = theta + math.radians(60)
        return (r * math.cos(t), r * math.sin(t))
    if n == 5:
        t = theta - math.radians(60)
        return (r * math.cos(t), r * math.sin(t))
    return None


def body_position(body: str | None) -> tuple[float, float] | None:
    """Approximate (x, y) km position of *body* on the Stanton map,
    or None when the body is unknown (e.g. 'Other', Pyro bodies)."""
    if not body:
        return None
    if body in _PLANETS:
        return _PLANETS[body]
    if body in _MOONS:
        return _PLANETS[_MOONS[body]]
    if body in _JUMP_POINTS:
        return _JUMP_POINTS[body]
    return _lagrange_position(body)


def station_positions(conn: sqlite3.Connection) -> dict[int, tuple[float, float]]:
    """station_id → (x, y) km for every station with a mappable
    parent_body. Stations whose body is unknown are absent — callers
    fall back to sort_order ordering for those."""
    out: dict[int, tuple[float, float]] = {}
    for r in conn.execute("SELECT id, parent_body FROM stations"):
        pos = body_position(r["parent_body"])
        if pos is not None:
            out[r["id"]] = pos
    return out


def distance_km(
    a: tuple[float, float] | None, b: tuple[float, float] | None,
) -> float | None:
    """Euclidean distance in km, or None when either side is unknown."""
    if a is None or b is None:
        return None
    return math.hypot(a[0] - b[0], a[1] - b[1])
