"""Outside data: city meters, OpenStreetMap lots, Google Places + walk times.

Fetchers take an aiohttp session and raise SourceError with a message the
panel can show. Parsers are plain functions (no HA imports).
"""
from __future__ import annotations

import asyncio
import math
import re

import aiohttp

from . import curbs, meters, stpaul
from .const import (
    FETCH_TIMEOUT_SECONDS,
    LOTS_SKIP_ACCESS,
    METER_SKIP_STATUS,
    METERS_PAGE,
    METERS_URL,
    MPLS_CENTERLINE_URL,
    MPLS_PERMIT_URL,
    MPLS_STREET_CLASSES,
    OVERPASS_TIMEOUT_SECONDS,
    OVERPASS_URLS,
    PLACES_FIELDS,
    PLACES_SEARCH_URL,
    ROUTES_FIELDS,
    ROUTES_MATRIX_URL,
    STP_CENTERLINES_URL,
    STP_FACILITIES_URL,
    STP_SIGNS_URL,
    STP_SPACES_URL,
    STP_TICKETS_URL,
)

_TIMEOUT = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
_OSM_TIMEOUT = aiohttp.ClientTimeout(total=OVERPASS_TIMEOUT_SECONDS)
_UA = {"User-Agent": "HomeAssistant-Curb/0.1 (personal parking planner)"}


class SourceError(Exception):
    """An outside call failed; str() is safe to show in the panel."""


def _net(err: Exception, who: str) -> SourceError:
    if isinstance(err, asyncio.TimeoutError):
        return SourceError(f"{who} didn't answer in time — try again in a minute")
    return SourceError(f"Couldn't reach {who}: {err}")


async def _json(resp) -> object:
    try:
        return await resp.json(content_type=None)
    except (ValueError, aiohttp.ClientError):
        return None


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ── City meters ──────────────────────────────────────────────────────────

async def _arcgis(session: aiohttp.ClientSession, url: str, params: dict, who: str,
                  order_by: str = "OBJECTID") -> list[dict]:
    """Every feature an ArcGIS query returns, following its paging."""
    feats, offset = [], 0
    while True:
        page_params = {
            "where": "1=1", "outFields": "*", "outSR": "4326", "f": "json",
            "orderByFields": order_by, "resultOffset": str(offset),
            "resultRecordCount": str(METERS_PAGE), "geometryPrecision": "6", **params,
        }
        try:
            async with session.get(url, params=page_params, headers=_UA, timeout=_TIMEOUT) as resp:
                data = await _json(resp)
                status = resp.status
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            raise _net(err, who) from err
        if status != 200 or not isinstance(data, dict) or "features" not in data:
            msg = (data or {}).get("error", {}).get("message") if isinstance(data, dict) else None
            raise SourceError(f"{who} error {status}: {msg or 'unexpected reply'}")
        page = data["features"]
        feats.extend(page)
        offset += len(page)
        if not data.get("exceededTransferLimit") or not page:
            return feats


async def fetch_meters(session: aiohttp.ClientSession) -> list[dict]:
    return parse_meters(await _arcgis(session, METERS_URL, {}, "the Minneapolis meter map", "OBJECTID_12"))


async def fetch_stpaul(session: aiohttp.ClientSession) -> tuple[list[dict], list[dict], list[dict]]:
    """(spaces, street segments around them, ramps/lots) as raw features;
    stpaul.group_spaces / parse_facilities turn them into rows."""
    who = "the St. Paul meter map"
    spaces = await _arcgis(session, STP_SPACES_URL, {}, who)
    pts = [f["geometry"] for f in spaces if "x" in (f.get("geometry") or {})]
    if not pts:
        raise SourceError(f"{who} returned no meter spaces")
    pad = 0.003  # ~250 m around the outermost meters
    envelope = (f"{min(p['x'] for p in pts) - pad},{min(p['y'] for p in pts) - pad},"
                f"{max(p['x'] for p in pts) + pad},{max(p['y'] for p in pts) + pad}")
    segments = []
    for layer in stpaul.CENTERLINE_LAYERS:
        segments += await _arcgis(session, STP_CENTERLINES_URL.format(layer=layer), {
            "geometry": envelope, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "OBJECTID,ST_CONCAT,ST_NAME,F_XSTREET,T_XSTREET,ASSET_ID",
        }, "St. Paul street map")
    facilities = await _arcgis(session, STP_FACILITIES_URL, {}, "St. Paul ramp list")
    return spaces, segments, facilities


async def fetch_curbs(session: aiohttp.ClientSession, city: str,
                      box: tuple[float, float, float, float]) -> dict:
    """Streets and what's posted on them inside box (s, w, n, e), in the
    compact form curbs.build reads (and the cache stores)."""
    s, w, n, e = box
    area = {"geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects"}
    if city == "stpaul":
        who = "St. Paul street map"
        layers, signs, tickets = await asyncio.gather(
            asyncio.gather(*(_arcgis(session, STP_CENTERLINES_URL.format(layer=layer), {
                **area, "outFields": "OBJECTID,ASSET_ID,ST_CONCAT,ST_NAME,F_XSTREET,T_XSTREET,"
                                     "ROUTESPEED,CTU_NAME_L,CTU_NAME_R"}, who)
                for layer in stpaul.CENTERLINE_LAYERS)),
            _arcgis(session, STP_SIGNS_URL, {
                **area, "where": "STATUS = 'ACTIVE' AND (CODE LIKE 'R7%' OR CODE LIKE 'R8%')",
                "outFields": "OBJECTID,CODE,MESSAGE"}, "St. Paul sign inventory"),
            # Counted per location by the server: ~7,500 downtown tickets -> ~300 rows.
            _arcgis(session, STP_TICKETS_URL, {
                **area, "where": "DESCRIPTION LIKE '%NO PARKING%'", "returnGeometry": "false",
                "outStatistics": '[{"statisticType":"count","onStatisticField":"OBJECTID","outStatisticFieldName":"n"}]',
                "groupByFieldsForStatistics": "LATITUDE,LONGITUDE"}, "St. Paul ticket data", "LATITUDE"),
        )
        return {
            "segments": curbs.stpaul_segments(dict(zip(stpaul.CENTERLINE_LAYERS, layers))),
            "signs": curbs.stpaul_signs(signs),
            "tickets": curbs.stpaul_tickets(tickets),
            "has_signs": True,
        }
    classes = ",".join(f"'{c}'" for c in MPLS_STREET_CLASSES)
    streets, permits = await asyncio.gather(
        _arcgis(session, MPLS_CENTERLINE_URL, {
            **area, "where": f"F_CLASS IN ({classes})",
            "outFields": "OBJECTID,STREETALL,F_CLASS,F_XSTREET_1,T_XSTREET_1,SPEED_LIM,CITYLEFT,CITYRIGT"},
            "Minneapolis street map"),
        _arcgis(session, MPLS_PERMIT_URL, {
            **area, "outFields": "OBJECTID_1,Enforcement,HourLimit,Alt_Enforcement"},
            "Minneapolis permit parking map", "OBJECTID_1"),
    )
    return {
        "segments": curbs.mpls_segments(streets),
        "permit_areas": curbs.mpls_permit_areas(permits),
        "has_signs": False,
    }


_MPLS_ADDRESS = re.compile(r"^(\d+)\s+(.+)$")


def _mpls_block(address: str) -> tuple[str, str, str]:
    """'401 2ND ST N' -> ('mpls:400:2ND ST N', '2nd St N, 400 block', 'odd')."""
    m = _MPLS_ADDRESS.match(address)
    if not m:
        return f"mpls:{address.upper()}", meters.pretty_address(address), ""
    number = int(m.group(1))
    hundred = number // 100 * 100
    street = re.sub(r"\s+", " ", m.group(2).strip().upper())
    name = meters.pretty_address(f"{hundred} {street}").split(" ", 1)[1]
    return (f"mpls:{hundred}:{street}", f"{name}, {'1–99' if hundred == 0 else hundred} block",
            "odd" if number % 2 else "even")


def parse_meters(feats: list[dict]) -> list[dict]:
    rows = []
    for f in feats:
        a = f.get("attributes") or {}
        rings = (f.get("geometry") or {}).get("rings") or []
        status = (a.get("Space_Stat") or "").strip()
        if not rings or not rings[0] or status.lower() in METER_SKIP_STATUS:
            continue
        ring = [[round(y, 6), round(x, 6)] for x, y in rings[0]]  # ArcGIS is [lon, lat]
        pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
        address = (a.get("Street_Add") or "").strip()
        block, block_name, side = _mpls_block(address)
        rows.append({
            "id": a.get("OBJECTID_12") or a.get("OBJECTID"),
            "city": "minneapolis",
            "block": block, "block_name": block_name, "side": side,
            "shape": "poly", "extra": {},
            "station": str(a.get("Station_ID") or "").strip(),
            "address": address,
            "rate": (a.get("Hourly_rat") or "").strip(),
            "hours": (a.get("Hours_of_E") or "").strip(),
            "days": (a.get("Days_of_en") or "").strip(),
            "hour_limit": (a.get("Hour_Limit") or "").strip(),
            "split": (a.get("Split_rate") or "").strip(),
            "restriction": (a.get("Restrictio") or "").strip(),
            "event": (a.get("Event_rate") or "").strip(),
            "status": status,
            "lat": sum(p[0] for p in pts) / len(pts),
            "lon": sum(p[1] for p in pts) / len(pts),
            "ring": ring,
        })
    return [r for r in rows if r["id"] is not None]


# ── OpenStreetMap lots ───────────────────────────────────────────────────

async def fetch_lots(session: aiohttp.ClientSession, lat: float, lon: float, radius: int) -> list[dict]:
    query = (
        f'[out:json][timeout:20];nwr["amenity"="parking"](around:{radius},{lat},{lon});'
        "out center tags;"
    )
    last: SourceError | None = None
    for url in OVERPASS_URLS:
        try:
            async with session.post(url, data={"data": query}, headers=_UA, timeout=_OSM_TIMEOUT) as resp:
                data = await _json(resp)
                status = resp.status
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            last = _net(err, "OpenStreetMap")
            continue
        if status == 200 and isinstance(data, dict):
            return parse_lots(data.get("elements") or [])
        last = SourceError(f"OpenStreetMap error {status}" + (" (busy — try later)" if status in (429, 502, 504) else ""))
    raise last


_LOT_KIND = {
    "multi-storey": "Ramp", "underground": "Underground", "surface": "Surface lot",
    "rooftop": "Rooftop", "street_side": "Street-side lot", "lane": "Street parking",
}


_GARAGES = ("multi-storey", "underground", "rooftop")


def parse_lots(elements: list[dict]) -> list[dict]:
    """Lots worth listing. Most OSM lots downtown are bare, unnamed outlines
    (usually a business's own lot); those are dropped. A lot stays if it says
    something: a name, operator, fee tag, garage type, or public access."""
    lots = []
    for e in elements:
        t = e.get("tags") or {}
        c = e.get("center") or e
        if "lat" not in c or "lon" not in c:
            continue
        access = (t.get("access") or "").lower()
        if access in LOTS_SKIP_ACCESS:
            continue
        fee = (t.get("fee") or "").lower()
        parking = (t.get("parking") or "").lower()
        if not (t.get("name") or t.get("operator") or fee or parking in _GARAGES
                or access in ("yes", "public", "permissive")):
            continue
        kind = _LOT_KIND.get(parking, "Lot")
        lots.append({
            "id": f"{e.get('type', 'x')[0]}{e.get('id')}",
            "name": t.get("name") or kind,
            "kind": kind,
            "fee": "free" if fee == "no" else "paid" if fee in ("yes", "fee") else "",
            "operator": t.get("operator") or "",
            "hours": t.get("opening_hours") or "",
            "capacity": t.get("capacity") or "",
            "lat": c["lat"],
            "lon": c["lon"],
        })
    return lots


# ── Google Places (destination search) ──────────────────────────────────

def _google_error(data, status: int, who: str) -> SourceError:
    msg = None
    if isinstance(data, dict):
        msg = (data.get("error") or {}).get("message")
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        msg = (data[0].get("error") or {}).get("message")
    if status == 429:
        return SourceError(f"Google {who} limit reached — try again later")
    if status == 403:
        return SourceError(f"Google refused the key for {who} ({msg or 'check the key restrictions'})")
    return SourceError(f"Google {who} error {status}: {msg or 'unknown'}")


async def search_places(session: aiohttp.ClientSession, key: str, query: str,
                        near_lat: float, near_lon: float) -> list[dict]:
    body = {
        "textQuery": query,
        "pageSize": 6,
        "locationBias": {"circle": {
            "center": {"latitude": near_lat, "longitude": near_lon}, "radius": 50000.0,
        }},
    }
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": PLACES_FIELDS}
    try:
        async with session.post(PLACES_SEARCH_URL, json=body, headers=headers, timeout=_TIMEOUT) as resp:
            data = await _json(resp)
            status = resp.status
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        raise _net(err, "Google") from err
    if status != 200:
        raise _google_error(data, status, "Places")
    out = []
    for p in (data or {}).get("places") or []:
        loc = p.get("location") or {}
        if "latitude" not in loc:
            continue
        out.append({
            "place_id": p.get("id"),
            "name": (p.get("displayName") or {}).get("text", ""),
            "address": p.get("formattedAddress", ""),
            "lat": loc["latitude"],
            "lon": loc.get("longitude"),
        })
    return out


# ── Google Routes (walk times) ──────────────────────────────────────────

def _waypoint(lat: float, lon: float) -> dict:
    return {"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lon}}}}


async def walk_matrix(session: aiohttp.ClientSession, key: str,
                      origins: list[tuple[float, float]], dest: tuple[float, float]) -> dict[int, tuple[int, int]]:
    """{origin index: (seconds, meters)} walking from each origin to dest.
    Origins with no walking route are left out."""
    if not origins:
        return {}
    body = {
        "origins": [_waypoint(*o) for o in origins],
        "destinations": [_waypoint(*dest)],
        "travelMode": "WALK",
    }
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": ROUTES_FIELDS}
    try:
        async with session.post(ROUTES_MATRIX_URL, json=body, headers=headers, timeout=_TIMEOUT) as resp:
            data = await _json(resp)
            status = resp.status
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        raise _net(err, "Google") from err
    if status != 200 or not isinstance(data, list):
        raise _google_error(data, status, "Routes")
    return parse_matrix(data)


def parse_matrix(elements: list[dict]) -> dict[int, tuple[int, int]]:
    out = {}
    for el in elements:
        # Proto3 JSON drops zero values: index 0 arrives with no originIndex.
        if el.get("condition") != "ROUTE_EXISTS":
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(el.get("duration") or ""))
        if not m:
            continue
        out[int(el.get("originIndex", 0))] = (int(float(m.group(1))), int(el.get("distanceMeters", 0)))
    return out
