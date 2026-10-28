"""Minneapolis meter rules -> what parking costs for one stay (no HA imports).

Every field comes from the city's meter layer as free text. The base rule is
Hourly_rat / Hours_of_E / Days_of_en / Hour_Limit. On top of that:

- Split_rate: the rate changes by day/time. Only 8 distinct strings exist
  citywide (checked 2026-10-27, 191 blocks), all on an 8am-10pm or
  8am-midnight daily base, so each is spelled out in SPLIT_RULES rather than
  parsed. They only change the rate/limit inside the base enforcement hours.
- Restrictio: tow/no-stopping/valet windows, spelled out in NO_PARKING.
- Event_rate: higher rate around an event at a named venue. Parsed (rate +
  venue names), since the format is regular.

A string missing from the tables isn't guessed at: the stay gets a "check the
meter" flag instead of a wrong price.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
ALL_DAYS = frozenset(range(7))
WEEKDAYS = frozenset({MON, TUE, WED, THU, FRI})
WEEKEND = frozenset({SAT, SUN})


def _days(a: int, b: int) -> frozenset[int]:
    """Inclusive day range, wrapping past Sunday (Sun-Wed = 6..2)."""
    out, d = set(), a
    while True:
        out.add(d)
        if d == b:
            return frozenset(out)
        d = (d + 1) % 7


H = 60  # minutes per hour, for readable tables


@dataclass(frozen=True)
class Window:
    days: frozenset[int]
    start: int  # minutes after midnight
    end: int    # exclusive; 1440 = midnight

    def covers(self, dow: int, mod: int) -> bool:
        return dow in self.days and self.start <= mod < self.end


NO_LIMIT = 0  # a RateRule limit meaning "no time limit in this window"


@dataclass(frozen=True)
class RateRule:
    window: Window
    rate: float
    limit: int | None  # minutes; None = keep the base limit; NO_LIMIT = none


# Keyed by _norm(Split_rate). First matching rule wins; outside all of them
# the base rate applies.
SPLIT_RULES: dict[str, tuple[RateRule, ...]] = {
    "OFF PEAK RATE $0.50 MON-FRI AFTER 6PM; ALL DAY SAT-SUN": (
        RateRule(Window(WEEKDAYS, 18 * H, 24 * H), 0.50, None),
        RateRule(Window(WEEKEND, 0, 24 * H), 0.50, None),
    ),
    "$2/HR 8A-MIDNIGHT SUN-WED, 8A-4PM THURS-SAT; $3/HR 4PM-MIDNIGHT THURS-SAT": (
        RateRule(Window(_days(SUN, WED), 8 * H, 24 * H), 2.00, None),
        RateRule(Window(_days(THU, SAT), 8 * H, 16 * H), 2.00, None),
        RateRule(Window(_days(THU, SAT), 16 * H, 24 * H), 3.00, None),
    ),
    "$2/HR 8A-10PM SUN-WED, 8A-4PM THURS-SAT; $3/HR 4PM-10PM THURS-SAT": (
        RateRule(Window(_days(SUN, WED), 8 * H, 22 * H), 2.00, None),
        RateRule(Window(_days(THU, SAT), 8 * H, 16 * H), 2.00, None),
        RateRule(Window(_days(THU, SAT), 16 * H, 22 * H), 3.00, None),
    ),
    # Weekday 8a-5p isn't covered by the text: base rate, base limit ("split").
    "$.50/HR 5HR 5P-10P M-F, $.50/HR 2HR 8A-2P SAT-S. $.50/HR 8HR 2P-10P SAT-SUN": (
        RateRule(Window(WEEKDAYS, 17 * H, 22 * H), 0.50, 5 * H),
        RateRule(Window(WEEKEND, 8 * H, 14 * H), 0.50, 2 * H),
        RateRule(Window(WEEKEND, 14 * H, 22 * H), 0.50, 8 * H),
    ),
    "$.50/HR 2HR 8A-2P S-SUN, $.50/HR 10HR 8A-10P M-F, $.50/HR 8HR 2P-10P S-SUN": (
        RateRule(Window(WEEKEND, 8 * H, 14 * H), 0.50, 2 * H),
        RateRule(Window(WEEKDAYS, 8 * H, 22 * H), 0.50, 10 * H),
        RateRule(Window(WEEKEND, 14 * H, 22 * H), 0.50, 8 * H),
    ),
    "$.50/HR 10HR 8A-10P M-F, $.50/HR 2HR 8A-2P SAT-S. $.50/HR 8HR 2P-10P SAT-SUN": (
        RateRule(Window(WEEKDAYS, 8 * H, 22 * H), 0.50, 10 * H),
        RateRule(Window(WEEKEND, 8 * H, 14 * H), 0.50, 2 * H),
        RateRule(Window(WEEKEND, 14 * H, 22 * H), 0.50, 8 * H),
    ),
    "$3.00/HR 2HR 8A-MIDNIGHT, $2/HR 15MIN 4P-8P M-F": (
        RateRule(Window(WEEKDAYS, 16 * H, 20 * H), 2.00, 15),
        RateRule(Window(ALL_DAYS, 8 * H, 24 * H), 3.00, 2 * H),
    ),
}

# Keyed by _norm(Restrictio). Parking during any window = tow/ticket risk.
_TOW_AM_PM = (Window(WEEKDAYS, 7 * H, 9 * H), Window(WEEKDAYS, 16 * H, 18 * H))
NO_PARKING: dict[str, tuple[Window, ...]] = {
    "TOW ZONE 7AM-9AM; BUS ZONE 2PM-6PM MON-FRI": (
        Window(WEEKDAYS, 7 * H, 9 * H), Window(WEEKDAYS, 14 * H, 18 * H),
    ),
    "NO PARKING 7AM-4PM MON-FRI": (Window(WEEKDAYS, 7 * H, 16 * H),),
    "NO STOPPING MON-FRI: 7AM-9AM & 4PM-6PM": _TOW_AM_PM,
    "TOW AWAY ZONE, 7AM-9AM; 4PM-6PM MON-FRI": _TOW_AM_PM,
    "TOW AWAY ZONE, 7AM-9AM AND 4PM-6PM; MON-FRI": _TOW_AM_PM,
    "NO STOPPING 4PM-6PM MON-FRI": (Window(WEEKDAYS, 16 * H, 18 * H),),
    "VALET 6PM-2AM FRIDAY AND SATURDAY": (
        Window(frozenset({FRI, SAT}), 18 * H, 24 * H), Window(frozenset({SAT, SUN}), 0, 2 * H),
    ),
    "VALET: 8PM-2AM THURS-SAT; 11AM-4PM SUNDAY": (
        Window(_days(THU, SAT), 20 * H, 24 * H), Window(_days(FRI, SUN), 0, 2 * H),
        Window(frozenset({SUN}), 11 * H, 16 * H),
    ),
}
NO_RESTRICTION = "ALWAYS CHECK FOR POSTED RESTRICTIONS ON STREET"

@dataclass(frozen=True)
class Venue:
    label: str
    before: int | None = None   # Minneapolis: event rate runs `before` min before
    after: int | None = None    # the start to `after` min after; None = all day
    fixed: tuple[int, int] | None = None  # St. Paul: set clock hours, start ignored


# Minneapolis windows are from the city's event-zone layer. St. Paul's city
# page lists event-rate hours per event, 6-10 PM for nearly all of them (one
# CHS Field Saturday game was 4-8 PM).
VENUES: dict[str, Venue] = {
    "target_field": Venue("Target Field", 180, 60),
    "target_center": Venue("Target Center", 180, 60),
    "us_bank": Venue("U.S. Bank Stadium", 180, 60),
    "huntington": Venue("Huntington Bank Stadium", 180, 60),
    "convention": Venue("Convention Center"),
    "grand_casino": Venue("Grand Casino Arena (St. Paul)", fixed=(18 * H, 22 * H)),
    "chs_field": Venue("CHS Field (St. Paul)", fixed=(18 * H, 22 * H)),
}
_VENUE_TEXT = {
    "TARGET FIELD": "target_field",
    "TARGET CENTER": "target_center",
    "BANK STADIUM": None,  # resolved below: U.S. Bank vs Huntington Bank
    "CONVENTION CENTER": "convention",
}
EVENT_DAY_HOURS = Window(ALL_DAYS, 9 * H, 22 * H)  # "Event Days 9a-10p Daily"


# ── Parsing ──────────────────────────────────────────────────────────────

def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().upper()).rstrip(".")


def _time(tok: str) -> int | None:
    tok = tok.strip().lower()
    if tok in ("midnight", "12am"):
        return 24 * H
    if tok == "noon":
        return 12 * H
    m = re.fullmatch(r"(\d{1,2})(?::(\d\d))?\s*([ap])m?", tok)
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
    return h * H + int(m.group(2) or 0)


_DAY_TOKENS = {"MON": MON, "TUE": TUE, "WED": WED, "THU": THU, "FRI": FRI, "SAT": SAT, "SUN": SUN}


def _day(tok: str) -> int | None:
    return _DAY_TOKENS.get(tok.strip().upper()[:3])


@lru_cache(maxsize=256)
def parse_days(s: str) -> frozenset[int] | None:
    s = _norm(s)
    if s in ("DAILY", "EVERY DAY"):
        return ALL_DAYS
    parts = s.split("-")
    if len(parts) == 2 and _day(parts[0]) is not None and _day(parts[1]) is not None:
        return _days(_day(parts[0]), _day(parts[1]))
    return None


@lru_cache(maxsize=256)
def parse_hours(s: str) -> tuple[int, int] | None:
    s = _norm(s)
    if s == "EVERY HOUR":
        return 0, 24 * H
    parts = s.split("-")
    if len(parts) != 2:
        return None
    a, b = _time(parts[0]), _time(parts[1])
    if a is None or b is None or b <= a:
        return None
    return a, b


def parse_money(s: str | None) -> float | None:
    m = re.search(r"\$\s*(\d*\.?\d+)", s or "")
    return float(m.group(1)) if m else None


@lru_cache(maxsize=256)
def parse_limit(s: str) -> int | None:
    """Minutes. '6 and 2' -> the stricter 2 hr. 'split'/blank -> None."""
    s = (s or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*min", s)
    if m:
        return int(m.group(1))
    nums = [float(x) for x in re.findall(r"\d*\.?\d+", s)]
    return int(min(nums) * H) if nums else None


@dataclass(frozen=True)
class EventRule:
    rate: float
    venues: frozenset[str]
    event_day_hours: bool  # "Event Days 9a-10p Daily": enforced 9-10 on event days


@lru_cache(maxsize=64)
def parse_event(s: str) -> EventRule | None:
    n = _norm(s)
    if not n:
        return None
    rate = parse_money(n)
    venues = set()
    for text, key in _VENUE_TEXT.items():
        if text in n:
            if key is None:
                key = "huntington" if "HUNTINGTON" in n else "us_bank"
            venues.add(key)
    if rate is None or not venues:
        return None
    return EventRule(rate, frozenset(venues), "EVENT DAYS 9A-10P" in n)


# ── One meter block ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Rules:
    base: Window | None        # None = couldn't read hours/days
    base_rate: float | None    # None = "split" with no base, or unreadable
    base_limit: int | None
    split: tuple[RateRule, ...]
    no_parking: tuple[Window, ...]
    event: EventRule | None
    flags: tuple[str, ...]     # things the stay result should warn about
    holiday: Callable[[date], bool] | None = None  # meters free on these days


def rules_for(m: dict) -> Rules:
    """m = a meters row. Minneapolis rows carry the city's raw strings;
    St. Paul rows carry parsed fields in `extra` (see stpaul.py)."""
    if m.get("city") == "stpaul":
        from . import stpaul
        return stpaul.rules(m)
    return _rules(m.get("rate") or "", m.get("hours") or "", m.get("days") or "",
                  m.get("hour_limit") or "", m.get("split") or "",
                  m.get("restriction") or "", m.get("event") or "")


@lru_cache(maxsize=512)
def _rules(rate, hours, days, limit, split, restriction, event) -> Rules:
    flags = []
    hrs, dys = parse_hours(hours), parse_days(days)
    base = Window(dys, *hrs) if hrs and dys else None
    if base is None:
        flags.append(f"Couldn't read the meter hours ({hours or '?'}, {days or '?'}) — check the meter")

    split_rules: tuple[RateRule, ...] = ()
    if _norm(split):
        split_rules = SPLIT_RULES.get(_norm(split), ())
        if not split_rules:
            flags.append(f"Rate varies: {split.strip()} — check the meter")

    no_parking: tuple[Window, ...] = ()
    r = _norm(restriction)
    if r and r != _norm(NO_RESTRICTION):
        no_parking = NO_PARKING.get(r, ())
        if not no_parking:
            flags.append(f"Posted: {restriction.strip()}")

    ev = parse_event(event)
    if _norm(event) and ev is None:
        flags.append(f"Event rate: {event.strip()}")

    return Rules(base, parse_money(rate), parse_limit(limit), split_rules, no_parking, ev, tuple(flags))


@dataclass
class Event:
    venue: str        # VENUES key
    start: datetime
    end: datetime | None = None  # explicit event-rate end (St. Paul); else from VENUES

    @classmethod
    def on(cls, venue: str, day: date, start_hm: tuple[int, int]) -> "Event":
        """The event on `day`: fixed-window venues ignore the start time."""
        v = VENUES[venue]
        midnight = datetime(day.year, day.month, day.day)
        if v.fixed:
            return cls(venue, midnight + timedelta(minutes=v.fixed[0]),
                       midnight + timedelta(minutes=v.fixed[1]))
        return cls(venue, midnight + timedelta(hours=start_hm[0], minutes=start_hm[1]))


def event_rate_window(ev: Event) -> tuple[datetime, datetime] | None:
    """When the event rate runs; None = all day of the event."""
    if ev.end:
        return ev.start, ev.end
    v = VENUES[ev.venue]
    if v.before is None:
        return None
    return ev.start - timedelta(minutes=v.before), ev.start + timedelta(minutes=v.after)


def fmt_time(t: datetime) -> str:
    h = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    if t.hour == 0 and t.minute == 0:
        return "midnight"
    return f"{h} {ampm}" if t.minute == 0 else f"{h}:{t.minute:02d} {ampm}"


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def assess(m: dict, arrive: datetime, minutes: int,
           events: Event | list[Event] | None = None) -> dict:
    """Cost and rules for parking at meter block m from arrive for minutes.

    Walks the stay minute by minute (a stay is at most a few hundred minutes,
    so this is cheap and handles midnight, day changes and event windows
    without special cases)."""
    rules = rules_for(m)
    if isinstance(events, Event):
        events = [events]
    mine = [e for e in events or [] if rules.event and e.venue in rules.event.venues]
    windows = [(e.start.date(), event_rate_window(e)) for e in mine]

    flags = list(rules.flags)
    cost = 0.0
    segs: list[list] = []          # [start, end, rate]; rate 0 = free
    blocked: list[datetime] = []
    # Time limits apply per rule: 2 hr under a daytime limit followed by an
    # unlimited evening is fine, so the run resets when the limit changes.
    run, run_limit = 0, None
    over: int | None = None        # the limit that was exceeded
    unknown_rate = holiday = event_hit = False

    for i in range(minutes):
        t = arrive + timedelta(minutes=i)
        dow, mod = t.weekday(), t.hour * H + t.minute

        if any(w.covers(dow, mod) for w in rules.no_parking):
            blocked.append(t)

        event_day = any(d == t.date() for d, _w in windows)
        in_event = any(d == t.date() and (w is None or w[0] <= t < w[1]) for d, w in windows)
        enforced = bool(rules.base and rules.base.covers(dow, mod)) or (
            event_day and rules.event.event_day_hours and EVENT_DAY_HOURS.covers(dow, mod))
        if enforced and rules.holiday and rules.holiday(t.date()):
            enforced, holiday = False, True

        rate, limit = 0.0, None
        if enforced:
            limit = rules.base_limit
            rate = rules.base_rate
            for rr in rules.split:
                if rr.window.covers(dow, mod):
                    rate = rr.rate
                    limit = rr.limit if rr.limit is not None else rules.base_limit
                    break
            if in_event:
                rate, event_hit = rules.event.rate, True
            if rate is None:
                # "split" base with no matching split rule: use the highest
                # rate this block charges so the estimate errs high.
                unknown_rate = True
                rate = max([rr.rate for rr in rules.split] or [0.0])
            cost += rate / 60
        if limit:  # None or NO_LIMIT = nothing to count
            if limit != run_limit:
                run, run_limit = 0, limit
            run += 1
            if run > limit and over is None:
                over = limit
        else:
            run, run_limit = 0, None

        if segs and segs[-1][2] == rate:
            segs[-1][1] = t + timedelta(minutes=1)
        else:
            segs.append([t, t + timedelta(minutes=1), rate])

    if unknown_rate:
        flags.append("Base rate not listed — estimated from the highest posted rate")
    note = _describe(segs)
    if holiday:
        note = f"{note} (city holiday: meters free)"
    return {
        "cost": round(cost + 1e-9, 2),
        "paid_minutes": sum(int((s[1] - s[0]).total_seconds() // 60) for s in segs if s[2]),
        "note": note,
        "over_limit": over is not None,
        "limit_minutes": over,
        "blocked": _describe_blocked(blocked) if blocked else None,
        "event_rate": rules.event.rate if event_hit else None,
        "flags": flags,
    }


def _describe(segs: list[list]) -> str:
    if not segs:
        return ""
    if len(segs) == 1:
        return "Free the whole time" if not segs[0][2] else f"{fmt_money(segs[0][2])}/hr the whole time"
    parts = []
    for i, (_s, end, rate) in enumerate(segs):
        last = i == len(segs) - 1
        what = "free" if not rate else f"{fmt_money(rate)}/hr"
        parts.append(f"then {what}" if last else f"{what} until {fmt_time(end)}")
    text = ", ".join(parts)
    return text[0].upper() + text[1:]


def _describe_blocked(times: list[datetime]) -> str:
    """Consecutive minutes -> 'No parking 7 AM–9 AM, 4 PM–6 PM'."""
    spans = [[times[0], times[0]]]
    for t in times[1:]:
        if t - spans[-1][1] == timedelta(minutes=1):
            spans[-1][1] = t
        else:
            spans.append([t, t])
    text = ", ".join(f"{fmt_time(a)}–{fmt_time(b + timedelta(minutes=1))}" for a, b in spans)
    return f"No parking {text}"


def describe_rules(m: dict) -> str:
    """Short human summary of the posted rules, e.g. '$1.00/hr · 9 AM–6 PM Mon–Fri · 4 hr limit'."""
    if m.get("city") == "stpaul":
        from . import stpaul
        return stpaul.describe(m)
    parts = []
    rate = (m.get("rate") or "").strip()
    if rate and rate.lower() != "split":
        parts.append(f"{rate}/hr")
    elif rate:
        parts.append("Rate varies")
    hrs = parse_hours(m.get("hours") or "")
    if hrs:
        a = datetime(2000, 1, 1) + timedelta(minutes=hrs[0])
        b = datetime(2000, 1, 1) + timedelta(minutes=hrs[1])
        days = (m.get("days") or "").strip().replace("-", "–")
        parts.append(f"{fmt_time(a)}–{fmt_time(b)} {days}".strip())
    lim = parse_limit(m.get("hour_limit") or "")
    if lim:
        parts.append(fmt_limit(lim))
    return " · ".join(parts)


def fmt_limit(minutes: int) -> str:
    if minutes % H == 0:
        return f"{minutes // H} hr limit"
    return f"{minutes / H:g} hr limit" if minutes > H else f"{minutes} min limit"


_ORDINAL = re.compile(r"^(\d+)(ST|ND|RD|TH)$")


def pretty_address(s: str) -> str:
    """'401 2ND ST N' -> '401 2nd St N'."""
    out = []
    for w in (s or "").split():
        u = w.upper()
        if _ORDINAL.match(u):
            out.append(u.lower())
        elif u in ("N", "S", "E", "W", "NE", "NW", "SE", "SW"):
            out.append(u)
        else:
            out.append(w.capitalize())
    return " ".join(out)
