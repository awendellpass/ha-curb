"""Unmetered curbs: block sides with no meter, and what's posted there (no HA imports).

Neither city's meter map covers free unmetered streets, so Curb builds them
from street centerlines minus the metered sides, then reads whatever each
city publishes about the curb:

- St. Paul: its sign inventory (72k signs with their text, edited 2026-08)
  and 2025 parking tickets. Parking signs within a few meters of a side set
  its rules; lots of no-parking tickets nearby earns a warning.
- Minneapolis: its Critical Parking Area (permit) block faces only. Its sign
  inventory uses numeric codes with no published legend.

A sign is a point, and "here to corner" or "begins/ends" depends on which way
it faces, so a side's rules are a strong hint, not a guarantee. The panel
says "check signs" on every unmetered option.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import date
from functools import lru_cache

from .meters import ALL_DAYS, H, WEEKDAYS, RateRule, Rules, Window, _days
from .stpaul import _cardinal, street_name

MIN_SEGMENT_M = 30     # shorter pieces are intersection stubs
METER_MATCH_M = 25     # a meter this close to a side makes it metered (Mpls block centroids sit ~15 m out)
SIGN_MATCH_M = 20
TICKET_MATCH_M = 25
PERMIT_MATCH_M = 25
END_CLEAR_M = 8        # a meter this close to a block end belongs to the cross street
FAST_MPH = 40          # Shepard Rd and the like: no curb parking
OFFSET_M = 5           # sides are drawn this far off the centerline
# Divided roads are two one-way centerlines; the sides facing each other
# are the median. Probed this far out from the middle of the side.
MEDIAN_PROBE_M = 14
MEDIAN_MATCH_M = 10
BARE_SIGNS_BAN = 2     # this many bare "NO PARKING" signs on one side = no parking there
ALL_DAY = [sorted(ALL_DAYS), 0, 24 * H]
_NO_END = ("CUL-DE-SAC", "DEAD END", "")


# ── Sign text -> rules ───────────────────────────────────────────────────

_SKIP = ("HANDICAP", "DISABLED", "STATE FAIR", "ELECTRIC VEHICLE", "CHARGING", "SNOW")
# Clearances at corners, drives and hydrants: on nearly every block, and
# they don't stop anyone parking mid-block, so they're ignored.
_CLEARANCE = ("CORNER", "FEET", " FT", "WITHIN", "DRIVE", "ALLEY", "HYDRANT", "CROSSWALK")
# Zones that take a stretch of the side.
_STRETCH = ("BUS", "LOADING", "TAXI", "VALET")
_MONTH = r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?"
_SEASON = re.compile(rf"\b{_MONTH}\s*(\d{{1,2}})\s*(?:-|TO|THRU|THROUGH)\s*{_MONTH}\s*(\d{{1,2}})\b")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
_T = r"(?:\d{1,2}(?::\d\d)?\s*(?:AM|PM)?|NOON|MIDNIGHT)"
_RANGE = re.compile(rf"({_T})\s*(?:-|TO)\s*({_T})")
_DAY = r"(MON|TUES?|WED|THU(?:RS?)?|FRI|SAT|SUN|M|F)"
_DAY_RANGE = re.compile(rf"\b{_DAY}[A-Z]*\s*-\s*{_DAY}[A-Z]*\b")
_DAY_NUM = {"MON": 0, "M": 0, "TUE": 1, "TUES": 1, "WED": 2, "THU": 3, "THUR": 3, "THURS": 3,
            "FRI": 4, "F": 4, "SAT": 5, "SUN": 6}
_PLURAL_DAY = re.compile(r"\b(MON|TUES|WEDNES|THURS|FRI|SATUR|SUN)DAYS?\b")
_PLURAL_NUM = {"MON": 0, "TUES": 1, "WEDNES": 2, "THURS": 3, "FRI": 4, "SATUR": 5, "SUN": 6}
_LIMIT = re.compile(r"\b(\d+(?:\.\d+)?|ONE|TWO|THREE|FOUR|HALF)\s*-?\s*(HOURS?|HRS?|MINUTES?|MINS?)\b")
_LIMIT_WORDS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "HALF": 0.5}


def _clock(tok: str, hint: str | None, end: bool) -> int | None:
    tok = tok.replace(" ", "")
    if tok == "NOON":
        return 12 * H
    if tok == "MIDNIGHT":
        return 24 * H if end else 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d\d))?(AM|PM)?", tok)
    ampm = (m.group(3) if m else None) or hint
    if not m or not ampm:
        return None
    t = (int(m.group(1)) % 12 + (12 if ampm == "PM" else 0)) * H + int(m.group(2) or 0)
    return 24 * H if end and t == 0 else t


def _sign_days(t: str) -> frozenset[int]:
    if "SCHOOL DAY" in t or "WEEKDAYS" in t:
        return WEEKDAYS  # school calendars aren't published; errs safe
    if re.search(r"\b(MWF|M,\s*W,\s*F|MON-WED-FRI)\b", t):
        return frozenset({0, 2, 4})
    m = _DAY_RANGE.search(t)
    if m:
        return _days(_DAY_NUM[m.group(1)], _DAY_NUM[m.group(2)])
    plural = {_PLURAL_NUM[d] for d in _PLURAL_DAY.findall(t)}
    return frozenset(plural) if plural else ALL_DAYS


def _windows(t: str) -> list[list]:
    """Time windows on a sign as [[days], start, end] (minutes; overnight
    spans split at midnight). [] when no times are posted."""
    days = _sign_days(t)
    if "24 HOURS" in t or "ANYTIME" in t:
        return [[sorted(days), 0, 24 * H]]
    out = []
    for a, b in _RANGE.findall(t):
        a, b = a.replace(" ", ""), b.replace(" ", "")
        hint_b = re.search(r"(AM|PM)$", b)
        if not hint_b and not re.search(r"AM|PM|NOON|MIDNIGHT", a + b):
            continue  # "1-2": not a time range
        start = _clock(a, hint_b.group(1) if hint_b else None, False)
        end = _clock(b, None, True)
        if start is None or end is None or start == end:
            continue
        if end > start:
            out.append([sorted(days), start, end])
        else:
            out.append([sorted(days), start, 24 * H])
            out.append([sorted((d + 1) % 7 for d in days), 0, end])
    return out


def _pretty(t: str) -> str:
    t = re.sub(r"\bM+ON-FRI\b|\bM-F\b", "Mon–Fri", t.replace("2HR ", "2 HR "))
    s = t.strip().capitalize()
    s = re.sub(r"\b(\d{1,2}(?::\d\d)?)\s*(am|pm)\b", lambda m: f"{m.group(1)} {m.group(2).upper()}", s)
    return re.sub(r"\b(mon|tues?|wed|thurs?|fri|sat|sun)\b", lambda m: m.group(1).capitalize(), s)


@lru_cache(maxsize=4096)
def parse_sign(text: str | None, code: str = "") -> dict | None:
    """One parking sign -> a rule, or None if it doesn't affect a regular car.

    {"kind": "no_parking" | "limit", "windows": [[days], start, end],
     "minutes": limit, "permit": bool, "season": [m1, d1, m2, d2] | None,
     "text": readable sign text}
    or {"kind": "partial", "text": ...} for signs that cover part of a side
    (corners, drives, bus loading, a bare no-parking symbol)."""
    t = re.sub(r"\(GRAPHIC\)|^R\d[\w-]*\s+-\s+|\bW/\s*[A-Z/ ]*?ARROWS?\b", " ", (text or "").upper())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ({"kind": "partial", "text": "No parking", "bare": True}
                if code.upper().startswith("R8-3") else None)
    if any(w in t for w in _SKIP):
        return None
    season, timed = None, t
    m = _SEASON.search(t)
    if m:
        season = [_MONTHS[m.group(1)], int(m.group(2)), _MONTHS[m.group(3)], int(m.group(4))]
        timed = t[:m.start()] + " " + t[m.end():]
    windows = _windows(timed)
    permit = "PERMIT" in t
    base = {"permit": permit, "season": season, "text": _pretty(t)}
    no_parking = "NO PARKING" in t or "NO STOPPING" in t or "TOW" in t

    lim = _LIMIT.search(t)
    if lim and not no_parking:
        n = _LIMIT_WORDS.get(lim.group(1)) or float(lim.group(1))
        minutes = int(n * H) if lim.group(2).startswith("H") else int(n)
        return {"kind": "limit", "minutes": minutes, "windows": windows or [ALL_DAY], **base}
    if not no_parking:
        return None
    if not permit and any(w in t for w in _CLEARANCE):
        return None
    if not permit and any(w in t for w in _STRETCH):
        return {"kind": "partial", "text": base["text"]}
    if not windows:
        if not (permit or "THIS SIDE" in t or "ANYTIME" in t or "THIS STREET" in t):
            # A bare "NO PARKING": one could be a hydrant; a row of them is the whole side.
            return {"kind": "partial", "text": base["text"], "bare": t == "NO PARKING"}
        windows = [ALL_DAY]
    return {"kind": "no_parking", "windows": windows, **base}


def permit_area_rules(enforcement: str | None, limit: str | None, alt: str | None) -> list[dict]:
    """A Minneapolis Critical Parking Area face ('2 HR Except By Permit',
    '8am-6pm Mon-Fri', alt '1pm-11pm Sat-Sun') -> rules, read like a sign."""
    out = []
    for when in (enforcement, alt):
        if not when or not re.search(r"\d", when):
            continue
        r = parse_sign(f"{limit or ''} {when}")
        if r and r["kind"] != "partial":
            out.append(r)
    return out


def _in_season(d: date, season: list[int]) -> bool:
    m1, d1, m2, d2 = season
    a, b, x = (m1, d1), (m2, d2), (d.month, d.day)
    return a <= x <= b if a <= b else x >= a or x <= b


def rules(m: dict, day: date) -> Rules:
    """Rules for an unmetered side on one day (seasonal signs come and go)."""
    x = m.get("extra") or {}
    return _rules(json.dumps(x.get("rules") or [], sort_keys=True), day.isoformat())


@lru_cache(maxsize=2048)
def _rules(rules_json: str, day_iso: str) -> Rules:
    d = date.fromisoformat(day_iso)
    no_parking: list[Window] = []
    limits: list[RateRule] = []
    for r in json.loads(rules_json):
        if r.get("season") and not _in_season(d, r["season"]):
            continue
        for days, a, b in r["windows"]:
            w = Window(frozenset(days), a, b)
            if r["kind"] == "no_parking":
                no_parking.append(w)
            elif r["kind"] == "limit":
                limits.append(RateRule(w, 0.0, r["minutes"]))
    # Free around the clock; limits count only inside their windows.
    return Rules(Window(ALL_DAYS, 0, 24 * H), 0.0, None, tuple(limits), tuple(no_parking), None, ())


def describe(m: dict) -> str:
    x = m.get("extra") or {}
    parts = ["No meter"]
    posted = [r["text"] for r in x.get("rules") or []]
    if posted:
        parts += posted[:3]
    elif x.get("signs"):
        parts.append("no parking signs on record")
    else:
        parts.append("no sign data for this city")
    parts.append("check posted signs")
    return " · ".join(parts)


# ── City data -> compact cache form ──────────────────────────────────────

def _paths(feature: dict) -> list[list[list[float]]]:
    return [[[round(y, 6), round(x, 6)] for x, y in p]
            for p in (feature.get("geometry") or {}).get("paths") or [] if len(p) > 1]


# A block ending at a freeway ramp ("Nb I35e To Kellogg Blvd W") is a ramp approach.
_RAMP = re.compile(r"\bI-?\s?\d|\bTO\b|\bRAMP\b")


def _fast(speed) -> bool:
    try:
        return int(speed or 0) >= FAST_MPH
    except (TypeError, ValueError):
        return False


def _segments(f: dict, sid, street: str, frm: str, to: str, major: bool) -> list[dict]:
    frm, to = frm.strip().upper(), to.strip().upper()
    if _RAMP.search(frm) or _RAMP.search(to):
        return []
    return [{"id": f"{sid}-{i}", "street": street_name(street),
             "from": "" if frm in _NO_END else street_name(frm),
             "to": "" if to in _NO_END else street_name(to), "major": major, "path": path}
            for i, path in enumerate(_paths(f))]


def stpaul_segments(by_layer: dict[int, list[dict]]) -> list[dict]:
    """Layers 1-3 (US/state highways, major streets) count as major."""
    out = []
    for layer, features in by_layer.items():
        for f in features:
            a = f.get("attributes") or {}
            if ("SAINT PAUL" not in f"{a.get('CTU_NAME_L')} {a.get('CTU_NAME_R')}".upper()
                    or _fast(a.get("ROUTESPEED"))):
                continue
            out += _segments(f, a.get("ASSET_ID") or a.get("OBJECTID"), a.get("ST_CONCAT") or a.get("ST_NAME") or "",
                             a.get("F_XSTREET") or "", a.get("T_XSTREET") or "", layer <= 3)
    return out


def mpls_segments(features: list[dict]) -> list[dict]:
    """F_CLASS A30 (arterials) counts as major."""
    out = []
    for f in features:
        a = f.get("attributes") or {}
        if "MINNEAPOLIS" not in f"{a.get('CITYLEFT')} {a.get('CITYRIGT')}".upper() or _fast(a.get("SPEED_LIM")):
            continue
        out += _segments(f, a.get("OBJECTID"), a.get("STREETALL") or "",
                         a.get("F_XSTREET_1") or "", a.get("T_XSTREET_1") or "", a.get("F_CLASS") == "A30")
    return out


def stpaul_signs(features: list[dict]) -> list[dict]:
    out = []
    for f in features:
        a, g = f.get("attributes") or {}, f.get("geometry") or {}
        if "x" in g and parse_sign(a.get("MESSAGE"), a.get("CODE") or ""):
            out.append({"lat": round(g["y"], 6), "lon": round(g["x"], 6),
                        "code": a.get("CODE") or "", "text": a.get("MESSAGE") or ""})
    return out


def stpaul_tickets(features: list[dict]) -> list[list]:
    """Grouped-count rows -> [[lat, lon, tickets]]."""
    out = []
    for f in features:
        a = f.get("attributes") or {}
        if a.get("LATITUDE") and a.get("LONGITUDE"):
            out.append([round(a["LATITUDE"], 6), round(a["LONGITUDE"], 6), int(a.get("n") or 0)])
    return out


def mpls_permit_areas(features: list[dict]) -> list[dict]:
    out = []
    for f in features:
        a = f.get("attributes") or {}
        ring = ((f.get("geometry") or {}).get("rings") or [[]])[0]
        found = permit_area_rules(a.get("Enforcement"), a.get("HourLimit"), a.get("Alt_Enforcement"))
        if ring and found:
            out.append({"lat": round(sum(p[1] for p in ring) / len(ring), 6),
                        "lon": round(sum(p[0] for p in ring) / len(ring), 6), "rules": found})
    return out


# ── Streets -> unmetered sides ───────────────────────────────────────────

class _Streets:
    """Street segments on a local flat plane, with a grid for 'which
    segments are near this point'."""
    CELL = 50.0

    def __init__(self, segments: list[dict]):
        lat0 = segments[0]["path"][0][0]
        self.kx = 111_320 * math.cos(math.radians(lat0))
        self.ky = 110_540.0
        self.parts: list[tuple] = []
        self.grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.length: list[float] = []
        for si, s in enumerate(segments):
            pts = [self.xy(lat, lon) for lat, lon in s["path"]]
            along = 0.0
            for a, b in zip(pts, pts[1:]):
                n = math.dist(a, b)
                pi = len(self.parts)
                self.parts.append((si, a, b, along, n))
                along += n
                for cx in range(int(min(a[0], b[0]) // self.CELL) - 1, int(max(a[0], b[0]) // self.CELL) + 2):
                    for cy in range(int(min(a[1], b[1]) // self.CELL) - 1, int(max(a[1], b[1]) // self.CELL) + 2):
                        self.grid[(cx, cy)].append(pi)
            self.length.append(along)

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return lon * self.kx, lat * self.ky

    def latlon(self, x: float, y: float) -> list[float]:
        return [round(y / self.ky, 6), round(x / self.kx, 6)]

    def near(self, lat: float, lon: float, max_m: float) -> list[tuple]:
        """[(meters, segment, side +1 left / -1 right, meters along)] for each
        segment within max_m, closest first."""
        p = self.xy(lat, lon)
        best: dict[int, tuple] = {}
        for pi in set(self.grid.get((int(p[0] // self.CELL), int(p[1] // self.CELL)), ())):
            si, a, b, along, n = self.parts[pi]
            dx, dy = b[0] - a[0], b[1] - a[1]
            t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy or 1e-9)))
            d = math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)
            if d <= max_m and (si not in best or d < best[si][0]):
                side = 1 if dx * (p[1] - a[1]) - dy * (p[0] - a[0]) > 0 else -1
                best[si] = (d, si, side, along + t * n)
        return sorted(best.values())

    def faces_median(self, si: int, seg: dict, side: int, segments: list[dict]) -> bool:
        """Whether another centerline of the same street (the other half of a
        divided road, or a duplicate) runs alongside this side."""
        pts = [self.xy(lat, lon) for lat, lon in seg["path"]]
        for f in (0.25, 0.5, 0.75):
            target, walked = self.length[si] * f, 0.0
            for a, b in zip(pts, pts[1:]):
                n = math.dist(a, b)
                if walked + n >= target and n:
                    t = (target - walked) / n
                    x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                    px = x - (b[1] - a[1]) / n * MEDIAN_PROBE_M * side
                    py = y + (b[0] - a[0]) / n * MEDIAN_PROBE_M * side
                    lat, lon = py / self.ky, px / self.kx
                    if any(o != si and segments[o]["street"] == seg["street"]
                           for _d, o, _s, _a in self.near(lat, lon, MEDIAN_MATCH_M)):
                        return True
                    break
                walked += n
        return False

    def side_line(self, path: list, side: int) -> tuple[list, list]:
        """(the side drawn OFFSET_M off the centerline, its midpoint)."""
        pts = [self.xy(lat, lon) for lat, lon in path]
        out = []
        for i, (x, y) in enumerate(pts):
            (ax, ay), (bx, by) = pts[max(i - 1, 0)], pts[min(i + 1, len(pts) - 1)]
            n = math.hypot(bx - ax, by - ay) or 1.0
            out.append((x - (by - ay) / n * OFFSET_M * side, y + (bx - ax) / n * OFFSET_M * side))
        half, walked = sum(math.dist(a, b) for a, b in zip(out, out[1:])) / 2, 0.0
        mid = out[0]
        for a, b in zip(out, out[1:]):
            n = math.dist(a, b)
            if walked + n >= half:
                f = (half - walked) / (n or 1.0)
                mid = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
                break
            walked += n
        return [self.latlon(*p) for p in out], self.latlon(*mid)


def build(city: str, data: dict, meter_points: list[tuple[float, float]]) -> list[dict]:
    """Cached city data (segments, signs, tickets, permit areas) + nearby
    meter locations -> one row per unmetered block side, shaped like a
    meters row (kind 'curb') so plans can rank them alongside meters."""
    segments = [s for s in data.get("segments") or [] if len(s["path"]) > 1]
    if not segments:
        return []
    streets = _Streets(segments)

    metered: set[tuple[int, int]] = set()
    for lat, lon in meter_points:
        for _d, si, side, along in streets.near(lat, lon, METER_MATCH_M)[:1]:
            if END_CLEAR_M < along < streets.length[si] - END_CLEAR_M:
                metered.add((si, side))

    side_rules: dict[tuple, dict[str, dict]] = defaultdict(dict)
    partial: dict[tuple, set[str]] = defaultdict(set)
    bare: dict[tuple, int] = defaultdict(int)
    for s in data.get("signs") or []:
        r = parse_sign(s["text"], s["code"])
        hit = streets.near(s["lat"], s["lon"], SIGN_MATCH_M)[:1]
        if not r or not hit:
            continue
        key = hit[0][1], hit[0][2]
        if r.get("bare"):
            bare[key] += 1
        elif r["kind"] == "partial":
            partial[key].add(r["text"])
        else:
            side_rules[key][r["text"]] = r
    for key, n in bare.items():
        # Where parking is posted ("2 HR PARKING"), bare signs mark stretches, not the side.
        if n >= BARE_SIGNS_BAN and not any(r["kind"] == "limit" for r in side_rules[key].values()):
            text = f"No parking ({n} signs along this side)"
            side_rules[key][text] = {"kind": "no_parking", "windows": [ALL_DAY], "permit": False,
                                     "season": None, "text": text}
        else:
            partial[key].add("No parking")
    for p in data.get("permit_areas") or []:
        for _d, si, side, _along in streets.near(p["lat"], p["lon"], PERMIT_MATCH_M)[:1]:
            for r in p["rules"]:
                side_rules[(si, side)][r["text"]] = r

    tickets: dict[int, int] = defaultdict(int)
    for lat, lon, n in data.get("tickets") or []:
        for _d, si, _side, _along in streets.near(lat, lon, TICKET_MATCH_M):
            tickets[si] += n

    signs = bool(data.get("has_signs"))
    rows = []
    for si, s in enumerate(segments):
        if streets.length[si] < MIN_SEGMENT_M:
            continue
        cross = [c for c in (s["from"], s["to"]) if c and c != s["street"]]
        name = (f"{s['street']}, {cross[0]}–{cross[1]}" if len(cross) == 2
                else f"{s['street']} near {cross[0]}" if cross else s["street"])
        (x1, y1), (x2, y2) = streets.xy(*s["path"][0]), streets.xy(*s["path"][-1])
        for side in (1, -1):
            if (si, side) in metered or streets.faces_median(si, s, side, segments):
                continue
            ring, mid = streets.side_line(s["path"], side)
            facing = _cardinal(x2 - x1, y2 - y1, side)
            rows.append({
                "id": f"{city}:{s['id']}:{'L' if side > 0 else 'R'}",
                "kind": "curb", "city": city,
                "block": f"curb:{city}:{s['id']}", "block_name": name, "side": facing,
                "address": f"{name} ({facing} side)",
                "lat": mid[0], "lon": mid[1], "ring": ring, "shape": "line",
                "extra": {
                    "rules": sorted(side_rules[(si, side)].values(), key=lambda r: r["text"]),
                    "partial": sorted(partial[(si, side)])[:3],
                    "tickets": tickets.get(si, 0),
                    "signs": signs,
                    "major": bool(s.get("major")),
                },
            })
    return rows
