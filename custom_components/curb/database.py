"""SQLite storage for Curb.

meters     — the city's metered block faces, replaced wholesale on each
             refresh. Rule fields are the city's raw strings; meters.py reads
             them at plan time, so rule fixes never need a migration.
spots      — saved parking spots, shared by everyone in the household.
             User-owned: only the panel writes them.
lots_cache — OpenStreetMap lots around a searched point, keyed by rounded
             lat/lon + radius.
meta       — key/value: schema_version, meters_updated, meters_error
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

from homeassistant.core import HomeAssistant

from .const import DB_NAME

SCHEMA_VERSION = 1

METER_FIELDS = ("id", "station", "address", "rate", "hours", "days", "hour_limit",
                "split", "restriction", "event", "status", "lat", "lon", "ring")
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
                id           INTEGER PRIMARY KEY,   -- city OBJECTID_12 (Station_ID isn't unique)
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


# ── meta ─────────────────────────────────────────────────────────────────

def get_meta(hass: HomeAssistant, key: str) -> str | None:
    with _connect(hass) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(hass: HomeAssistant, key: str, value: str | None) -> None:
    with _connect(hass) as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


# ── meters ───────────────────────────────────────────────────────────────

def meter_count(hass: HomeAssistant) -> int:
    with _connect(hass) as conn:
        return conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0]


def replace_meters(hass: HomeAssistant, rows: list[dict]) -> None:
    with _connect(hass) as conn:
        conn.execute("DELETE FROM meters")
        conn.executemany(
            f"INSERT INTO meters ({', '.join(METER_FIELDS)}) "
            f"VALUES ({', '.join('?' for _ in METER_FIELDS)})",
            [tuple(json.dumps(r[f]) if f == "ring" else r[f] for f in METER_FIELDS) for r in rows],
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('meters_updated', ?)", (_now(),)
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
        d["ring"] = json.loads(d["ring"])
        out.append(d)
    return out


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
