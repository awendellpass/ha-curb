"""Meter refresh, destination search, parking plans, saved-spot validation."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import database, meters, sources, stpaul
from .const import (
    CITIES,
    DEFAULT_RADIUS_M,
    DOMAIN,
    FACILITY_DEDUPE_M,
    LOTS_CACHE_DAYS,
    MAX_LOTS,
    MAX_OPTIONS,
    MAX_RADIUS_M,
    MAX_STAY_MINUTES,
    METERS_MIN_KEEP_RATIO,
    METERS_REFRESH_DAYS,
    OVER_LIMIT_PENALTY,
    WALK_CACHE_HOURS,
    WALK_DETOUR,
    WALK_M_PER_MIN,
    WALK_VALUE_PER_MIN,
    WALKS_PER_PLAN,
)

_LOGGER = logging.getLogger(__name__)
_METER_LOCK = asyncio.Lock()


def _conf(hass: HomeAssistant) -> dict:
    return hass.data[DOMAIN]


def _run(hass: HomeAssistant, fn, *args):
    return hass.async_add_executor_job(fn, hass, *args)


def _key(hass: HomeAssistant) -> str:
    key = _conf(hass).get("google_api_key")
    if not key:
        raise sources.SourceError("Google key isn't configured (curb: google_api_key)")
    return key


# ── Meters ───────────────────────────────────────────────────────────────

def _stale(updated: str | None) -> bool:
    if not updated:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(updated) > timedelta(days=METERS_REFRESH_DAYS)
    except ValueError:
        return True


async def _load_city(hass: HomeAssistant, city: str) -> tuple[list[dict], list[dict] | None]:
    """(meter rows, listed ramps/lots or None) for one city."""
    session = async_get_clientsession(hass)
    if city == "stpaul":
        spaces, segments, facilities = await sources.fetch_stpaul(session)
        rows = await hass.async_add_executor_job(stpaul.group_spaces, spaces, segments)
        return rows, stpaul.parse_facilities(facilities)
    return await sources.fetch_meters(session), None


async def async_refresh_meters(hass: HomeAssistant, force: bool = False) -> dict:
    """Re-download each city's meters when stale (or forced). Never raises;
    one city failing leaves the other's data and refresh alone."""
    async with _METER_LOCK:
        for city in CITIES:
            have = await _run(hass, database.meter_count, city)
            updated = await _run(hass, database.get_meta, f"meters_updated:{city}")
            if not force and have and not _stale(updated):
                continue
            try:
                rows, facilities = await _load_city(hass, city)
                if not rows or (have and len(rows) < have * METERS_MIN_KEEP_RATIO):
                    raise sources.SourceError(
                        f"{CITIES[city]} meter map returned {len(rows)} meters (had {have}) — kept the old list")
                await _run(hass, database.replace_meters, city, rows, facilities)
                await _run(hass, database.set_meta, f"meters_error:{city}", None)
                _LOGGER.debug("Curb: loaded %s %s meter blocks", len(rows), city)
            except sources.SourceError as err:
                _LOGGER.error("Curb: %s meter refresh failed: %s", CITIES[city], err)
                await _run(hass, database.set_meta, f"meters_error:{city}", str(err))
    return await async_status(hass)


async def async_status(hass: HomeAssistant) -> dict:
    cities = []
    for city, label in CITIES.items():
        cities.append({
            "key": city, "label": label,
            "count": await _run(hass, database.meter_count, city),
            "updated": await _run(hass, database.get_meta, f"meters_updated:{city}"),
            "error": await _run(hass, database.get_meta, f"meters_error:{city}"),
        })
    return {
        "configured": bool(_conf(hass).get("google_api_key")),
        "meters": {"count": sum(c["count"] for c in cities), "cities": cities},
        "spots": await _run(hass, database.get_spots),
        "venues": [{"key": k, "label": v.label, "fixed": bool(v.fixed)} for k, v in meters.VENUES.items()],
        "home": {"lat": hass.config.latitude, "lon": hass.config.longitude},
        "default_radius": DEFAULT_RADIUS_M,
    }


# ── Search ───────────────────────────────────────────────────────────────

async def async_search(hass: HomeAssistant, query: str) -> list[dict]:
    query = (query or "").strip()
    if len(query) < 2:
        return []
    return await sources.search_places(
        async_get_clientsession(hass), _key(hass), query[:200],
        hass.config.latitude, hass.config.longitude)


# ── Plan ─────────────────────────────────────────────────────────────────

def _num(v, name: str, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
    return x


def clean_plan(body: dict) -> dict:
    lat = _num(body.get("lat"), "lat", -90, 90)
    lon = _num(body.get("lon"), "lon", -180, 180)
    try:
        arrive = datetime.fromisoformat(str(body.get("arrive", ""))).replace(tzinfo=None, second=0, microsecond=0)
    except ValueError:
        raise ValueError("arrive must look like 2026-10-28T18:00") from None
    minutes = int(round(_num(body.get("hours", 2), "hours", 0.25, MAX_STAY_MINUTES / 60) * 60))
    radius = int(_num(body.get("radius", DEFAULT_RADIUS_M), "radius", 100, MAX_RADIUS_M))
    event = None
    ev = body.get("event") or None
    if ev:
        venue = ev.get("venue")
        if venue not in meters.VENUES:
            raise ValueError("Unknown event venue")
        start = (0, 0)
        if not meters.VENUES[venue].fixed:
            try:
                hh, mm = (int(x) for x in str(ev.get("start", "")).split(":"))
                datetime(2000, 1, 1, hh, mm)
                start = (hh, mm)
            except ValueError:
                raise ValueError("Event start must look like 18:40") from None
        event = meters.Event.on(venue, arrive.date(), start)
    name = str(body.get("name") or "").strip()[:120] or "Destination"
    return {"lat": lat, "lon": lon, "name": name, "arrive": arrive, "minutes": minutes,
            "radius": radius, "event": event}


def _nav_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}&travelmode=driving"


def _estimate_walk(dist: float) -> tuple[int, float]:
    walk_m = dist * WALK_DETOUR
    return int(walk_m), walk_m / WALK_M_PER_MIN


def _score(o: dict) -> float:
    return o["cost"] + o["walk_min"] * WALK_VALUE_PER_MIN + (OVER_LIMIT_PENALTY if o["over_limit"] else 0)


def _meter_option(m: dict, plan: dict, dist: float) -> dict:
    a = meters.assess(m, plan["arrive"], plan["minutes"], plan["event"])
    walk_m, walk_min = _estimate_walk(dist)
    warnings = list(a["flags"])
    if a["over_limit"]:
        warnings.insert(0, f"{meters.fmt_limit(a['limit_minutes'])} — you'd have to move")
    name = (meters.pretty_address(m["address"]) if m.get("city") == "minneapolis" else m["address"])
    return {
        "key": f"m{m['id']}", "kind": "meter", "meter_id": m["id"], "city": m.get("city"),
        "block": m.get("block") or "", "block_name": m.get("block_name") or "", "side": m.get("side") or "",
        "name": name or "Meter",
        "lat": m["lat"], "lon": m["lon"],
        "cost": a["cost"], "note": a["note"], "rules": meters.describe_rules(m),
        "event_rate": a["event_rate"], "over_limit": a["over_limit"], "blocked": a["blocked"],
        "warnings": warnings, "dist_m": int(dist), "walk_m": walk_m, "walk_min": walk_min,
        "walk_source": "estimate", "nav_url": _nav_url(m["lat"], m["lon"]),
    }


def _spot_option(s: dict, plan: dict, dist: float) -> dict:
    walk_m, walk_min = _estimate_walk(dist)
    cost = round(s["rate"] * plan["minutes"] / 60, 2)
    return {
        "key": f"s{s['id']}", "kind": "spot", "spot_id": s["id"], "name": s["name"],
        "lat": s["lat"], "lon": s["lon"], "cost": cost,
        "note": "Free (your saved spot)" if not s["rate"] else f"{meters.fmt_money(s['rate'])}/hr (your saved spot)",
        "rules": s["notes"], "event_rate": None, "over_limit": False, "blocked": None,
        "warnings": [], "dist_m": int(dist), "walk_m": walk_m, "walk_min": walk_min,
        "walk_source": "estimate", "nav_url": _nav_url(s["lat"], s["lon"]),
    }


def _merge_sides(opts: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Fold block faces of the same block (Minneapolis hundred-block, St. Paul
    street segment) that would cost and read exactly the same for this stay,
    usually the two sides of one street, into one option at the closer face.
    Faces that differ in any way, e.g. one side has a tow window, stay
    separate. Returns (options, {merged-away key: kept key})."""
    groups: dict[tuple, list[dict]] = {}
    out: list[dict] = []
    for o in opts:
        if o["kind"] != "meter" or not o.get("block"):
            out.append(o)
            continue
        sig = (o["block"], o["cost"], o["note"], o["rules"], tuple(o["warnings"]), o["over_limit"])
        groups.setdefault(sig, []).append(o)

    key_map: dict[str, str] = {}
    for faces in groups.values():
        keep = min(faces, key=lambda f: f["dist_m"])
        if len(faces) > 1:
            both = len({f["side"] for f in faces}) > 1
            keep = {**keep, "name": f"{keep['block_name']}{' (both sides)' if both else ''}",
                    "faces": len(faces)}
            for f in faces:
                key_map[f["key"]] = keep["key"]
        out.append(keep)
    return out, key_map


async def _apply_walks(hass: HomeAssistant, dest: tuple[float, float], opts: list[dict]) -> str | None:
    """Swap estimates for Google walk times on opts (in place). Returns an
    error message if Google couldn't be used; estimates stay in that case."""
    cache = _conf(hass).setdefault("walk_cache", {})
    ttl = WALK_CACHE_HOURS * 3600
    now = time.monotonic()
    dkey = f"{dest[0]:.5f},{dest[1]:.5f}"
    need = []
    for o in opts:
        hit = cache.get(f"{dkey}|{o['lat']:.5f},{o['lon']:.5f}")
        if hit and now - hit[0] < ttl:
            o["walk_min"], o["walk_m"], o["walk_source"] = hit[1] / 60, hit[2], "google"
        else:
            need.append(o)
    if not need:
        return None
    try:
        res = await sources.walk_matrix(async_get_clientsession(hass), _key(hass),
                                        [(o["lat"], o["lon"]) for o in need], dest)
    except sources.SourceError as err:
        _LOGGER.warning("Curb: walk times unavailable, using estimates: %s", err)
        return str(err)
    for i, o in enumerate(need):
        if i in res:
            secs, dist = res[i]
            cache[f"{dkey}|{o['lat']:.5f},{o['lon']:.5f}"] = (now, secs, dist)
            o["walk_min"], o["walk_m"], o["walk_source"] = secs / 60, dist, "google"
    return None


def _lots_key(lat: float, lon: float, radius: int) -> str:
    return f"{lat:.3f},{lon:.3f},{radius}"


def _facility_lot(f: dict) -> dict:
    """A city-listed ramp/lot in the same shape as an OSM lot."""
    rates = [r for r in (f"{f['hourly']}/hr" if f["hourly"] else "",
                         f"{f['daily']}/day" if f["daily"] else "") if r]
    return {
        "id": f["id"], "name": f["name"], "kind": f["kind"], "fee": "paid" if rates else "",
        "operator": f["operator"], "hours": f["hours"], "capacity": "", "lat": f["lat"], "lon": f["lon"],
        "rate": " · ".join(rates), "rates_as_of": f["rates_as_of"], "website": f["website"],
        "height": f["height"], "source": "city",
    }


def _box(lat: float, lon: float, radius: int) -> tuple[float, float, float, float]:
    dlat = radius / 111_000
    dlon = radius / (111_000 * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


async def _lots_near(hass: HomeAssistant, lat: float, lon: float, radius: int,
                     osm: list[dict]) -> list[dict]:
    """City-listed ramps/lots (with rates) plus OSM lots, dropping an OSM lot
    that's the same place as a city-listed one; nearest first."""
    city = [_facility_lot(f) for f in await _run(hass, database.facilities_in_box, *_box(lat, lon, radius))
            if sources.distance_m(lat, lon, f["lat"], f["lon"]) <= radius]
    osm = [o for o in osm if not any(
        sources.distance_m(o["lat"], o["lon"], c["lat"], c["lon"]) < FACILITY_DEDUPE_M for c in city)]
    out = []
    for lot in city + osm:
        d = sources.distance_m(lat, lon, lot["lat"], lot["lon"])
        walk_m, walk_min = _estimate_walk(d)
        out.append({**lot, "walk_m": walk_m, "walk_min": walk_min, "nav_url": _nav_url(lot["lat"], lot["lon"])})
    out.sort(key=lambda x: x["walk_m"])
    return out[:MAX_LOTS]


async def _cached_osm(hass: HomeAssistant, lat: float, lon: float, radius: int) -> list[dict] | None:
    cached = await _run(hass, database.get_lots_cache, _lots_key(lat, lon, radius))
    if cached and datetime.now() - datetime.fromisoformat(cached[0]) < timedelta(days=LOTS_CACHE_DAYS):
        return cached[1]
    return None


async def async_lots(hass: HomeAssistant, query: dict) -> dict:
    """Lots around a point: city-listed ones plus OpenStreetMap (cached, else
    fetched, slow: ~30 s)."""
    lat = _num(query.get("lat"), "lat", -90, 90)
    lon = _num(query.get("lon"), "lon", -180, 180)
    radius = int(_num(query.get("radius", DEFAULT_RADIUS_M), "radius", 100, MAX_RADIUS_M))
    osm = await _cached_osm(hass, lat, lon, radius)
    error = None
    if osm is None:
        key = _lots_key(lat, lon, radius)
        try:
            osm = await sources.fetch_lots(async_get_clientsession(hass), lat, lon, radius)
            await _run(hass, database.set_lots_cache, key, osm)
        except sources.SourceError as err:
            _LOGGER.warning("Curb: OpenStreetMap lots unavailable: %s", err)
            stale = await _run(hass, database.get_lots_cache, key)
            osm, error = (stale[1] if stale else []), str(err)
    return {"lots": await _lots_near(hass, lat, lon, radius, osm), "error": error}


async def async_plan(hass: HomeAssistant, body: dict) -> dict:
    plan = clean_plan(body)
    lat, lon, radius = plan["lat"], plan["lon"], plan["radius"]
    rows = await _run(hass, database.meters_in_box, *_box(lat, lon, radius))
    near = []
    for m in rows:
        d = sources.distance_m(lat, lon, m["lat"], m["lon"])
        if d <= radius:
            near.append((m, d))

    opts = [_meter_option(m, plan, d) for m, d in near]
    for s in await _run(hass, database.get_spots):
        d = sources.distance_m(lat, lon, s["lat"], s["lon"])
        if d <= radius:
            opts.append(_spot_option(s, plan, d))

    usable, key_map = _merge_sides([o for o in opts if not o["blocked"]])
    usable.sort(key=_score)
    walk_error = None
    if usable and _conf(hass).get("google_api_key"):
        walk_error = await _apply_walks(hass, (lat, lon), usable[:WALKS_PER_PLAN])
    usable.sort(key=_score)

    # OSM lots come from the cache only; on a miss the panel calls GET /lots.
    osm = await _cached_osm(hass, lat, lon, radius)
    lots = await _lots_near(hass, lat, lon, radius, osm or [])

    meter_map = [
        {"key": key_map.get(o["key"], o["key"]), "ring": m["ring"], "shape": m.get("shape") or "poly",
         "cost": o["cost"], "blocked": bool(o["blocked"]), "over_limit": o["over_limit"]}
        for (m, _d), o in zip(near, opts)
    ]
    ev = plan["event"]
    ev_window = meters.event_rate_window(ev) if ev else None
    return {
        "destination": {"name": plan["name"], "lat": lat, "lon": lon},
        "arrive": plan["arrive"].isoformat(timespec="minutes"),
        "leave": (plan["arrive"] + timedelta(minutes=plan["minutes"])).isoformat(timespec="minutes"),
        "minutes": plan["minutes"],
        "radius": radius,
        "event": {
            "venue": ev.venue, "label": meters.VENUES[ev.venue].label,
            "start": ev.start.isoformat(timespec="minutes"),
            # None = the event rate runs all day
            "rate_from": ev_window[0].isoformat(timespec="minutes") if ev_window else None,
            "rate_to": ev_window[1].isoformat(timespec="minutes") if ev_window else None,
        } if ev else None,
        "options": usable[:MAX_OPTIONS],
        "counts": {
            "meters": len(near),
            "free": sum(1 for o in opts if o["kind"] == "meter" and not o["blocked"] and o["cost"] == 0),
            "blocked": sum(1 for o in opts if o["blocked"]),
        },
        "lots": lots,
        "lots_pending": osm is None,
        "walk_error": walk_error,
        "map": {"meters": meter_map},
        "cities": sorted({m.get("city") for m, _d in near if m.get("city")}),
    }


# ── Saved spots ──────────────────────────────────────────────────────────

def clean_spot(data: dict, partial: bool = False) -> dict:
    out = {}
    if not partial or "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("Give the spot a name")
        out["name"] = name[:80]
    if "notes" in data:
        out["notes"] = str(data.get("notes") or "").strip()[:500]
    if "rate" in data:
        out["rate"] = round(_num(data.get("rate") or 0, "Rate", 0, 50), 2)
    if not partial or "lat" in data or "lon" in data:
        out["lat"] = _num(data.get("lat"), "lat", -90, 90)
        out["lon"] = _num(data.get("lon"), "lon", -180, 180)
    return out
