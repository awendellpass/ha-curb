"""St. Paul meters: individual spaces -> block faces, and their rules (no HA imports).

The city publishes one point per metered space (checked 2026-10-28, 1,882
spaces): day rate/limit, night rate/limit, rush/overnight/misc restrictions
in a regular "4PM-6PM M-F" format, and an event zone with its rate. Rules per
the city's meter page (stpaul.gov/parking-saint-paul/events-and-hourly):

- Downtown: Mon-Sat 8 AM-10 PM; day rate until 6 PM, night rate 6-10 PM.
- Outside downtown: Mon-Sat 8 AM-6 PM. These spaces have no night rate.
- Sundays and 11 city holidays are free.
- Event rates run at listed times, 6-10 PM for nearly every event.

Spaces carry no street address, so each is matched to the nearest city street
centerline (all matched within 30 m in testing, median 5 m) and grouped by
street segment + side + identical rules into a block face named like
"7th Pl E, Jackson St–Sibley St (north side)".
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache

from . import meters
from .meters import ALL_DAYS, H, NO_LIMIT, MON, SAT, EventRule, RateRule, Rules, Window

MON_SAT = frozenset(range(MON, SAT + 1))
DAY_END = 18 * H
ID_BASE = 1_000_000          # keeps St. Paul ids clear of Minneapolis OBJECTIDs
MAX_MATCH_M = 30             # a space farther than this from any street is dropped
ZONE_VENUES = {"XCEL": "grand_casino", "CHS FIELD": "chs_field"}
# Interstates (0) and access ramps (5) are left out so a space under an
# overpass isn't matched to the freeway above it.
CENTERLINE_LAYERS = (1, 2, 3, 4, 6)


# ── Holidays ─────────────────────────────────────────────────────────────

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _with_observed(d: date) -> set[date]:
    """The date plus its federally observed day (Sat -> Fri, Sun -> Mon)."""
    if d.weekday() == 5:
        return {d, d - timedelta(days=1)}
    if d.weekday() == 6:
        return {d, d + timedelta(days=1)}
    return {d}


@lru_cache(maxsize=8)
def holidays(year: int) -> frozenset[date]:
    days: set[date] = set()
    for fixed in (date(year, 1, 1), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)):
        days |= _with_observed(fixed)
    days |= {
        _nth_weekday(year, 1, 0, 3),    # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),    # Presidents' Day
        _last_weekday(year, 5, 0),      # Memorial Day
        date(year, 6, 19),              # Juneteenth (city lists the day itself)
        _nth_weekday(year, 9, 0, 1),    # Labor Day
        _nth_weekday(year, 10, 0, 2),   # Indigenous Peoples' Day
        _nth_weekday(year, 11, 3, 4),   # Thanksgiving
    }
    days |= _with_observed(date(year + 1, 1, 1))  # Dec 31 when New Year's is a Saturday
    return frozenset(days)


def is_holiday(d: date) -> bool:
    return d in holidays(d.year)


# ── Rules ────────────────────────────────────────────────────────────────

def _money(s) -> float | None:
    try:
        return float(str(s).strip().lstrip("$"))
    except ValueError:
        return None


def _limit(s) -> int | None:
    """'2' -> 120 min, '0.5' -> 30, 'NONE' -> NO_LIMIT, blank -> None."""
    s = str(s or "").strip().upper()
    if s == "NONE":
        return NO_LIMIT
    try:
        return int(float(s) * H)
    except ValueError:
        return None


_RESTRICT = re.compile(r"^(\d{1,2})(AM|PM)-(\d{1,2})(AM|PM)\s+(M-F|MWF|DAILY|M-SAT)$")
_RESTRICT_DAYS = {
    "M-F": meters.WEEKDAYS, "MWF": frozenset({0, 2, 4}), "DAILY": ALL_DAYS, "M-SAT": MON_SAT,
}


def _hour(n: str, ampm: str) -> int:
    return (int(n) % 12 + (12 if ampm == "PM" else 0)) * H


def parse_restriction(s: str) -> Window | None:
    m = _RESTRICT.match(re.sub(r"\s+", " ", s.strip().upper()))
    if not m:
        return None
    a, b = _hour(m.group(1), m.group(2)), _hour(m.group(3), m.group(4))
    return Window(_RESTRICT_DAYS[m.group(5)], a, b) if b > a else None


def rules(m: dict) -> Rules:
    extra = m.get("extra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra or "{}")
    return _rules(json.dumps(extra, sort_keys=True))


@lru_cache(maxsize=512)
def _rules(extra_json: str) -> Rules:
    x = json.loads(extra_json)
    flags: list[str] = []
    day_rate, night_rate = _money(x.get("day_rate")), _money(x.get("night_rate"))
    day_limit = _limit(x.get("day_limit"))
    base = Window(MON_SAT, 8 * H, 22 * H if night_rate is not None else DAY_END)
    split: tuple[RateRule, ...] = ()
    if night_rate is not None:
        night_limit = _limit(x.get("night_limit"))
        split = (RateRule(Window(MON_SAT, DAY_END, 22 * H), night_rate,
                          NO_LIMIT if night_limit is None else night_limit),)

    no_parking: list[Window] = []
    for r in x.get("restrict") or []:
        w = parse_restriction(r)
        if w:
            no_parking.append(w)
        else:
            flags.append(f"Posted: {r}")
    for note in x.get("notes") or []:
        n = note.upper()
        if n.startswith("VALET ZONE NP"):
            # Days aren't given; treat it as every day so it errs safe.
            no_parking.append(Window(ALL_DAYS, 17 * H, 23 * H))
            flags.append("Valet zone: no parking 5–11 PM")
        elif n == "NIGHTTIME PARKING ONLY":
            flags.append("Posted: nighttime parking only")
        elif n == "BACK-IN PARKING ONLY":
            flags.append("Back-in parking only")

    event = None
    venue = ZONE_VENUES.get((x.get("zone") or "").upper())
    zone_rate = _money(x.get("zone_rate"))
    if venue and zone_rate is not None:
        event = EventRule(zone_rate, frozenset({venue}), False)

    return Rules(base, day_rate, None if day_limit == NO_LIMIT else day_limit, split,
                 tuple(no_parking), event, tuple(flags), is_holiday)


def describe(m: dict) -> str:
    """'$2.25/hr 8 AM–6 PM, $1.25/hr 6–10 PM · Mon–Sat · 2 hr limit days'."""
    x = m.get("extra") or {}
    if isinstance(x, str):
        x = json.loads(x or "{}")
    day, night = _money(x.get("day_rate")), _money(x.get("night_rate"))
    parts = []
    if day is not None:
        parts.append(f"${day:.2f}/hr 8 AM–6 PM" + (f", ${night:.2f}/hr 6–10 PM" if night is not None else ""))
    parts.append("Mon–Sat")
    lim = _limit(x.get("day_limit"))
    if lim:
        parts.append(meters.fmt_limit(lim) + (" (days)" if night is not None else ""))
    if x.get("zone"):
        label = meters.VENUES[ZONE_VENUES[x["zone"].upper()]].label.replace(" (St. Paul)", "")
        parts.append(f"${_money(x.get('zone_rate')):.2f}/hr {label} events")
    return " · ".join(parts)


# ── Spaces -> block faces ────────────────────────────────────────────────

_ABBR = {
    "STREET": "St", "AVENUE": "Ave", "BOULEVARD": "Blvd", "PARKWAY": "Pkwy", "DRIVE": "Dr",
    "PLACE": "Pl", "ROAD": "Rd", "COURT": "Ct", "LANE": "Ln", "TERRACE": "Ter",
    "EAST": "E", "WEST": "W", "NORTH": "N", "SOUTH": "S",
}


def street_name(s: str | None) -> str:
    """'PLATO BOULEVARD EAST' -> 'Plato Blvd E', '7TH PLACE EAST' -> '7th Pl E'."""
    words = re.sub(r"\s+", " ", (s or "").strip().upper()).split()
    out = []
    for w in words:
        if w in _ABBR:
            out.append(_ABBR[w])
        elif re.fullmatch(r"\d+(ST|ND|RD|TH)", w):
            out.append(w.lower())
        elif len(w) <= 2 and w in ("N", "S", "E", "W", "NE", "NW", "SE", "SW"):
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)


def _cardinal(dx: float, dy: float, side: int) -> str:
    """Which side of a street segment (direction dx, dy) a point is on."""
    nx, ny = (-dy, dx) if side > 0 else (dy, -dx)
    ang = math.degrees(math.atan2(ny, nx)) % 360
    return ("east", "north", "west", "south")[int(((ang + 45) % 360) // 90)]


def group_spaces(spaces: list[dict], segments: list[dict]) -> list[dict]:
    """ArcGIS features -> meters rows (one per block face, see database.py)."""
    lat0 = 44.95
    kx, ky = 111_000 * math.cos(math.radians(lat0)), 111_000.0

    def xy(lon: float, lat: float) -> tuple[float, float]:
        return lon * kx, lat * ky

    cell = 60.0
    parts: list[tuple] = []
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for si, seg in enumerate(segments):
        for path in (seg.get("geometry") or {}).get("paths") or []:
            for (x1, y1), (x2, y2) in zip(path, path[1:]):
                a, b = xy(x1, y1), xy(x2, y2)
                pi = len(parts)
                parts.append((si, *a, *b))
                for cx in range(int(min(a[0], b[0]) // cell) - 1, int(max(a[0], b[0]) // cell) + 2):
                    for cy in range(int(min(a[1], b[1]) // cell) - 1, int(max(a[1], b[1]) // cell) + 2):
                        grid[(cx, cy)].append(pi)

    def nearest(lon: float, lat: float):
        p = xy(lon, lat)
        best = None
        for pi in set(grid.get((int(p[0] // cell), int(p[1] // cell)), ())):
            si, ax, ay, bx, by = parts[pi]
            dx, dy = bx - ax, by - ay
            seg_len2 = dx * dx + dy * dy or 1e-9
            t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / seg_len2))
            d = math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))
            if best is None or d < best[0]:
                side = 1 if dx * (p[1] - ay) - dy * (p[0] - ax) > 0 else -1
                best = (d, si, side, dx, dy, (p[0] - ax) * dx + (p[1] - ay) * dy)
        return best

    groups: dict[tuple, list] = defaultdict(list)
    for f in spaces:
        a = f.get("attributes") or {}
        g = f.get("geometry") or {}
        notes = [n.strip() for n in (a.get("Notes") or "").split(";") if n.strip()]
        if (a.get("Status") or "").strip().lower() != "active" or "x" not in g:
            continue
        if any(n.upper() == "MOTORCYCLE" for n in notes) or _money(a.get("StdDayRate")) is None:
            continue
        hit = nearest(g["x"], g["y"])
        if not hit or hit[0] > MAX_MATCH_M:
            continue
        _d, si, side, dx, dy, along = hit
        keep_notes = tuple(n for n in notes if n.upper().startswith(("VALET", "NIGHTTIME", "BACK-IN")))
        restrict = tuple(r.strip() for r in (a.get("Restrict_R"), a.get("Restrict_S"), a.get("Restrict_M"))
                         if (r or "").strip())
        sig = (
            (a.get("StdDayRate") or "").strip(), (a.get("StdDayLimi") or "").strip(),
            (a.get("StdNightRa") or "").strip(), (a.get("StdNightLi") or "").strip(),
            restrict, (a.get("Zone") or "").strip(), (a.get("Zone_Rate") or "").strip(), keep_notes,
        )
        groups[(si, side, sig)].append((along, g["y"], g["x"], dx, dy))

    rows = []
    ordered = sorted(groups.items(), key=lambda kv: (
        str((segments[kv[0][0]].get("attributes") or {}).get("ASSET_ID")), kv[0][1], kv[0][2]))
    for i, ((si, side, sig), pts) in enumerate(ordered):
        seg = segments[si].get("attributes") or {}
        street = street_name(seg.get("ST_CONCAT") or seg.get("ST_NAME"))
        # A curving street can list itself as a cross street; drop that end.
        cross = [x for x in (street_name(seg.get("F_XSTREET")), street_name(seg.get("T_XSTREET")))
                 if x and x != street]
        block_name = (f"{street}, {cross[0]}–{cross[1]}" if len(cross) == 2
                      else f"{street} near {cross[0]}" if cross else street)
        pts.sort()
        facing = _cardinal(pts[0][3], pts[0][4], side)
        day_rate, day_limit, night_rate, night_limit, restrict, zone, zone_rate, notes = sig
        extra = {
            "day_rate": day_rate, "day_limit": day_limit,
            "night_rate": night_rate or None, "night_limit": night_limit or None,
            "restrict": list(restrict), "zone": zone or None, "zone_rate": zone_rate or None,
            "notes": list(notes), "spaces": len(pts),
        }
        rows.append({
            "id": ID_BASE + i,
            "city": "stpaul",
            "station": "",
            "address": f"{block_name} ({facing} side)",
            "block": f"stp:{seg.get('ASSET_ID') or seg.get('OBJECTID')}:{si}",
            "block_name": block_name,
            "side": facing,
            "rate": f"${float(day_rate):.2f}",
            "hours": "8am-10pm" if night_rate else "8am-6pm",
            "days": "Mon-Sat",
            "hour_limit": day_limit,
            "split": "", "restriction": "", "event": "",
            "status": "Active",
            "lat": sum(p[1] for p in pts) / len(pts),
            "lon": sum(p[2] for p in pts) / len(pts),
            "ring": [[round(p[1], 6), round(p[2], 6)] for p in pts],
            "shape": "line",
            "extra": extra,
        })
    return rows


# ── City ramps and lots ──────────────────────────────────────────────────

def _hours_text(s: str) -> str:
    """'Mon: 6:00 AM - 7:00 PM<br/>Tue: ...' -> 'Mon–Fri 6:00 AM–7:00 PM' when uniform."""
    s = (s or "").strip()
    if not s or "<br" not in s:
        return s
    rows = [r.strip() for r in re.split(r"<br\s*/?>", s) if r.strip()]
    by_day = [r.split(":", 1) for r in rows if ":" in r]
    times = {t.strip() for _d, t in by_day}
    if len(times) == 1:
        span = times.pop().replace(" - ", "–")
        days = [d.strip() for d, _t in by_day]
        label = "Daily" if len(days) == 7 else f"{days[0]}–{days[-1]}"
        return f"{label} {span}"
    return "; ".join(r.replace(" - ", "–") for r in rows)


def parse_facilities(features: list[dict]) -> list[dict]:
    out = []
    for f in features:
        a = f.get("attributes") or {}
        try:
            lat, lon = float(a.get("Lat")), float(a.get("Long_"))
        except (TypeError, ValueError):
            g = f.get("geometry") or {}
            if "y" not in g:
                continue
            lat, lon = g["y"], g["x"]
        updated = a.get("LastUpdated")
        try:
            updated = datetime.utcfromtimestamp(int(updated) / 1000).date().isoformat()
        except (TypeError, ValueError):
            updated = ""
        site = a.get("RateContract_Info") or a.get("ContactWebsite") or ""
        out.append({
            "id": f"stp-{a.get('OBJECTID')}",
            "city": "stpaul",
            "name": (a.get("PEDName") or a.get("SpotHeroName") or "Parking").strip(),
            "kind": (a.get("FacilityType") or "Lot").strip(),
            "operator": (a.get("Operator") or "").strip(),
            "address": (a.get("StreetAddress") or "").strip(),
            "hourly": (a.get("HourlyRate") or "").strip(),
            "daily": (a.get("DailyRate") or "").strip(),
            "hours": _hours_text(a.get("HoursOfOperation") or ""),
            "website": site if str(site).startswith("http") else "",
            "height": (a.get("MaximumVehicleHeight") or "").strip() if a.get("MaximumVehicleHeight") not in (None, "None") else "",
            "rates_as_of": updated,
            "lat": lat,
            "lon": lon,
        })
    return out
