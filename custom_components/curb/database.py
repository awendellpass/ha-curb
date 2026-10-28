"""SQLite storage for Curb.

meters     — metered block faces for every city, replaced per city on each
             refresh. Minneapolis rule fields are the city's raw strings and
             St. Paul's sit in `extra`; meters.py / stpaul.py read them at
             plan time, so rule fixes never need a migration.
facilities — city-listed ramps and lots with rates (St. Paul), replaced per
             city on refresh.
spots      — saved parking spots, shared by everyone in the household.
             User-owned: only the panel writes them.
lots_cache — OpenStreetMap lots around a searched point, keyed by rounded
             lat/lon + radius.
meta       — key/value: schema_version, meters_updated:<city>, meters_error:<city>
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

from homeassistant.core import HomeAssistant

from .const import DB_NAME

SCHEMA_VERSION = 2

METER_FIELDS = ("id", "city", "block", "block_name", "side", "shape", "extra",
                "station", "address", "rate", "hours", "days", "hour_limit",
                "split", "restriction", "event", "status", "lat", "lon", "ring")
_JSON_FIELDS = ("ring", "extra")
FACILITY_FIELDS = ("id", "city", "name", "kind", "operator", "address", "hourly", "daily",
                   "hours", "website", "height", "rates_as_of", "lat", "lon")
# Added in schema 2 (St. Paul): column -> definition
_V2_METER_COLUMNS = {
    "city": "TEXT NOT NULL DEFAULT 'minneapolis'",
    "block": "TEXT NOT NULL DEFAULT ''",
    "block_name": "TEXT NOT NULL DEFAULT ''",
    "side": "TEXT NOT NULL DEFAULT ''",
    "shape": "TEXT NOT NULL DEFAULT 'poly'",
    "extra": "TEXT NOT NULL DEFAULT '{}'",
}
SPOT_FIELDS = ("name", "notes", "rate", "lat", "lon")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect(hass: HomeAssistant) -> sqlite3.Connection:
    conn = sqlite3.connect(os.path.join(hass.config.config_dir, DB_NAME))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(hass: HomeAssistant) -> None:
    with _connect(hass) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meters (
                id           INTEGER PRIMARY KEY,   -- Mpls OBJECTID_12; St. Paul 1,000,000+
                city         TEXT NOT NULL DEFAULT 'minneapolis',
                block        TEXT NOT NULL DEFAULT '',  -- faces sharing it may merge
                block_name   TEXT NOT NULL DEFAULT '',  -- '2nd St N, 400 block'
                side         TEXT NOT NULL DEFAULT '',  -- odd/even, or north/south/...
                shape        TEXT NOT NULL DEFAULT 'poly',  -- poly (Mpls) | line (St. Paul)
                extra        TEXT NOT NULL DEFAULT '{}',    -- city-specific parsed rules
                station      TEXT NOT NULL DEFAULT '',
                address      TEXT NOT NULL DEFAULT '',
                rate         TEXT NOT NULL DEFAULT '',
                hours        TEXT NOT NULL DEFAULT '',
                days         TEXT NOT NULL DEFAULT '',
                hour_limit   TEXT NOT NULL DEFAULT '',
                split        TEXT NOT NULL DEFAULT '',
                restriction  TEXT NOT NULL DEFAULT '',
                event        TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT '',
                lat          REAL NOT NULL,         -- polygon centroid
                lon          REAL NOT NULL,
                ring         TEXT NOT NULL DEFAULT '[]'  -- [[lat, lon], ...]
            );
            CREATE TABLE IF NOT EXISTS spots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                notes       TEXT NOT NULL DEFAULT '',
                rate        REAL NOT NULL DEFAULT 0,  -- $/hr; 0 = free
                lat         REAL NOT NULL,
                lon         REAL NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS facilities (
                id           TEXT PRIMARY KEY,
                city         TEXT NOT NULL,
                name         TEXT NOT NULL,
                kind         TEXT NOT NULL DEFAULT '',
                operator     TEXT NOT NULL DEFAULT '',
                address      TEXT NOT NULL DEFAULT '',
                hourly       TEXT NOT NULL DEFAULT '',
                daily        TEXT NOT NULL DEFAULT '',
                hours        TEXT NOT NULL DEFAULT '',
                website      TEXT NOT NULL DEFAULT '',
                height       TEXT NOT NULL DEFAULT '',
                rates_as_of  TEXT NOT NULL DEFAULT '',
                lat          REAL NOT NULL,
                lon          REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lots_cache (
                key         TEXT PRIMARY KEY,
                fetched_at  TEXT NOT NULL,
                data        TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key    TEXT PRIMARY KEY,
                value  TEXT
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
    if version < 2:
        have = {r[1] for r in conn.execute("PRAGMA table_info(meters)")}
        for col, definition in _V2_METER_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE meters ADD COLUMN {col} {definition}")
        # Old rows lack block info: drop them so the next refresh reloads both cities.
        conn.execute("DELETE FROM meters")
        conn.execute("DELETE FROM meta WHERE key IN ('meters_updated', 'meters_error')")
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")


# ── meta ─────────────────────────────────────────────────────────────────

def get_meta(hass: HomeAssistant, key: str) -> str | None:
    with _connect(hass) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(hass: HomeAssistant, key: str, value: str | None) -> None:
    with _connect(hass) as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


# ── meters ───────────────────────────────────────────────────────────────

def meter_count(hass: HomeAssistant, city: str | None = None) -> int:
    with _connect(hass) as conn:
        if city:
            return conn.execute("SELECT COUNT(*) FROM meters WHERE city = ?", (city,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0]


def replace_meters(hass: HomeAssistant, city: str, rows: list[dict],
                   facilities: list[dict] | None = None) -> None:
    """Swap in one city's meters (and its listed ramps/lots, when given)."""
    with _connect(hass) as conn:
        conn.execute("DELETE FROM meters WHERE city = ?", (city,))
        conn.executemany(
            f"INSERT INTO meters ({', '.join(METER_FIELDS)}) "
            f"VALUES ({', '.join('?' for _ in METER_FIELDS)})",
            [tuple(json.dumps(r[f]) if f in _JSON_FIELDS else r[f] for f in METER_FIELDS) for r in rows],
        )
        if facilities is not None:
            conn.execute("DELETE FROM facilities WHERE city = ?", (city,))
            conn.executemany(
                f"INSERT OR REPLACE INTO facilities ({', '.join(FACILITY_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in FACILITY_FIELDS)})",
                [tuple(f[k] for k in FACILITY_FIELDS) for f in facilities],
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (f"meters_updated:{city}", _now())
        )


def meters_in_box(hass: HomeAssistant, s: float, w: float, n: float, e: float) -> list[dict]:
    with _connect(hass) as conn:
        rows = conn.execute(
            "SELECT * FROM meters WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (s, n, w, e),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for f in _JSON_FIELDS:
            d[f] = json.loads(d[f] or "null")
        out.append(d)
    return out


def facilities_in_box(hass: HomeAssistant, s: float, w: float, n: float, e: float) -> list[dict]:
    with _connect(hass) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM facilities WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (s, n, w, e),
        )]


# ── spots ────────────────────────────────────────────────────────────────

def get_spots(hass: HomeAssistant) -> list[dict]:
    with _connect(hass) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM spots ORDER BY name COLLATE NOCASE")]


def get_spot(hass: HomeAssistant, spot_id: int) -> dict | None:
    with _connect(hass) as conn:
        row = conn.execute("SELECT * FROM spots WHERE id = ?", (spot_id,)).fetchone()
    return dict(row) if row else None


def add_spot(hass: HomeAssistant, data: dict) -> int:
    now = _now()
    with _connect(hass) as conn:
        cur = conn.execute(
            "INSERT INTO spots (name, notes, rate, lat, lon, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (data["name"], data.get("notes", ""), data.get("rate", 0), data["lat"], data["lon"], now, now),
        )
        return cur.lastrowid


def update_spot(hass: HomeAssistant, spot_id: int, data: dict) -> bool:
    fields = [f for f in SPOT_FIELDS if f in data]
    if not fields:
        return get_spot(hass, spot_id) is not None
    with _connect(hass) as conn:
        cur = conn.execute(
            f"UPDATE spots SET {', '.join(f'{f} = ?' for f in fields)}, updated_at = ? WHERE id = ?",
            (*[data[f] for f in fields], _now(), spot_id),
        )
        return cur.rowcount > 0


def delete_spot(hass: HomeAssistant, spot_id: int) -> bool:
    with _connect(hass) as conn:
        return conn.execute("DELETE FROM spots WHERE id = ?", (spot_id,)).rowcount > 0


# ── lots cache ───────────────────────────────────────────────────────────

def get_lots_cache(hass: HomeAssistant, key: str) -> tuple[str, list] | None:
    with _connect(hass) as conn:
        row = conn.execute("SELECT fetched_at, data FROM lots_cache WHERE key = ?", (key,)).fetchone()
    return (row["fetched_at"], json.loads(row["data"])) if row else None


def set_lots_cache(hass: HomeAssistant, key: str, lots: list) -> None:
    with _connect(hass) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO lots_cache (key, fetched_at, data) VALUES (?, ?, ?)",
            (key, _now(), json.dumps(lots)),
        )
