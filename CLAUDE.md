# Curb — Claude Project Conventions

Curb is a Home Assistant custom integration that finds free and cheap parking near a destination for a given arrival time and stay, then hands off to Google Maps with the *parking spot* as the destination. Andy uses it to plan before leaving, not to find an open space live. Same structure and patterns as Perk and Scout (`Documents\perk_ha`, `Documents\scout_ha`).

v1 scope (2026-10-27): Minneapolis meters, OpenStreetMap lots, saved spots (shared by the household), Google Places search and Routes walk times, manual event selection. Deferred: automatic event detection (MLB Stats API for the Twins, Ticketmaster Discovery for Target Center and U.S. Bank Stadium), snow emergency alerts, spot ratings, Minneapolis holidays.

v0.2 (2026-10-28): **St. Paul** added (see below). Expansion to other cities is on hold while Andy tries SpotAngels for travel.

## Stack

- **Backend:** Python, SQLite (`database.py`), HA's shared aiohttp session
- **Frontend:** Vanilla JS + Leaflet 1.9.4 from unpkg, Esri Dark Gray Canvas tiles (base + reference/labels layers, native zoom 16). Tile providers that failed (2026-10-27), don't switch to them: **CARTO** now requires an API key (watermarked tiles); **tile.openstreetmap.org** returns 403 "Access blocked" because HA serves panel files with `Referrer-Policy: no-referrer` and OSM requires a Referer. Two-file iframe auth pattern: `curb-panel.js` passes `hass.auth.data.access_token` to `curb-app.js` via `postMessage`; every fetch sends `Authorization: Bearer`. Panel host uses `height: 100vh`. The iframe has `allow="geolocation"` for "Near me".
- **Deploy:** GitHub `awendellpass/ha-curb` → HACS custom repository → restart HA. Ask before pushing.

## Configuration

```yaml
curb:
  google_api_key: !secret curb_google_api_key
```

The key lives in `/config/secrets.yaml`, never in this repo. Never read `secrets.yaml`. It's in Google Cloud project **HA APIs** (`second-elf-495919-b7`), restricted to Places API (New) + Routes API. A bare `curb:` works without search; walk times are estimated.

## File Structure

```
custom_components/curb/
  __init__.py   — config schema, DB init, static path, views, panel, meter refresh timer
  manifest.json
  const.py      — source URLs, ranking weights, limits
  meters.py     — Minneapolis meter rules + the shared cost walk (`assess`), venues (no HA imports)
  stpaul.py     — St. Paul spaces -> block faces, rules, holidays, listed ramps (no HA imports)
  sources.py    — city meters, Overpass lots, Google Places + Routes (fetch + parse)
  database.py   — SQLite: meters (both cities, `city` column), facilities, spots, lots_cache, meta; schema 2
  api.py        — meter refresh, search, plan ranking, lots, spot validation
  views.py      — REST endpoints
  panel.py      — sidebar panel
  frontend/
    curb-panel.js  — HA web component (iframe + token postMessage)
    curb-app.html  — markup + all CSS
    curb-app.js    — app logic (Find parking / Saved spots)
```

## Sources (checked 2026-10-27)

- **City meters:** ArcGIS FeatureServer `msvcPW_ParkingMeters2/FeatureServer/0` behind the city's meter map. 1,046 block-face polygons, about 1,043 kept (`Space_Stat` "Remove" skipped). maxRecordCount 1000, so it's paged. Last edited 2026-08-25. `Station_ID` isn't unique; key on `OBJECTID_12`. Refreshed weekly; a refresh returning under half the current count is rejected.
- **OpenStreetMap lots:** Overpass. The main server took **~26-32 s** for a lots query; the two mirrors timed out from Andy's PC (they may work from the Pi). Lots load separately from the plan (`GET /lots`) and are cached 7 days per rounded point + radius. Most downtown OSM lots are bare unnamed outlines; `parse_lots` keeps only lots with a name, operator, fee tag, garage type or public access (146 → 23 in the North Loop test).
- **Google:** Places Text Search (New) for destinations, biased to HA's home location. Routes `computeRouteMatrix` (WALK) for the top 25 candidates, cached 24 h per destination/candidate pair. **Proto3 JSON omits zero values: origin index 0 arrives with no `originIndex`.** Google calls were not tested before the first deploy (the key stays on the Pi).

## St. Paul (checked 2026-10-28)

- **Data:** city ArcGIS org `services1.arcgis.com/9meaaHE3uiba0zr8`, found via the "Parking in Downtown" web app (95361716a5434600b04e07e301c2373e).
  - `Metered_Street_Parking_Spaces_(View)/3`: 1,882 **points**, one per space, with StdDayRate/Limi, StdNightRa/Li, Restrict_R/S/M, Zone + Zone_Rate, Status, Notes.
  - `StreetCenterlines/{1,2,3,4,6}`: names (ST_CONCAT, F_XSTREET, T_XSTREET). Layer 4 needs paging. Interstates and ramps are skipped.
  - `DowntownParkingv5_view/0`: 53 ramps/lots with hourly/daily rates, but `LastUpdated` is 2020, so they're labeled "rate listed 2020".
  - Many more layers exist (snow emergency, residential permit, NoParking, street sweeping) for later.
- **Rules** (stpaul.gov/parking-saint-paul/events-and-hourly):
  - Downtown Mon–Sat 8–10: day rate until 6 PM, night rate 6–10.
  - Outside downtown Mon–Sat 8–6: these spaces have no night rate.
  - Sundays are free, plus 11 city holidays with observed days (`stpaul.holidays`).
  - Event rates (XCEL zone $3.75 → venue `grand_casino`, CHS FIELD $2.50 → `chs_field`) run 6–10 PM by default (`Venue.fixed`).
  - Night limit "NONE" = `NO_LIMIT` (no limit at night even when days are 2 hr).
- **Grouping:** each space goes to the nearest centerline within 30 m (all 1,877 active matched, median 5 m). Faces = segment + side + identical rules → 273 faces, ~0.1 s. Names look like "7th Pl E, Jackson St–Sibley St (north side)"; a street listed as its own cross street becomes "Kellogg Blvd near Saint Peter St". Faces are stored as `shape = line` and drawn as polylines.
- **The city's event schedule page is stale** (10 events, all September 2026, during Wild season), so it isn't read. Manual venue pick for now; auto-detect later from NHL/Saints schedules.
- Verified by hand:
  - Downtown Tue 5 PM 3 h = $4.75 with no limit flag; 1 PM 3 h flags the 2 hr limit.
  - Sunday and Thanksgiving are free.
  - Arena zone on a Wild Sat 6–10 = $15.00 (vs $5.00 with no event).
  - Rush 4–6 restriction is flagged.

## Meter rules (meters.py)

- Base rule: `Hourly_rat` / `Hours_of_E` / `Days_of_en` / `Hour_Limit`. All 9 hour formats and 3 day formats in the city data parse.
- **Split rates** (8 distinct strings, 191 blocks) and **no-parking windows** (`Restrictio`, ~10 strings) are spelled out in `SPLIT_RULES` / `NO_PARKING`, keyed by the normalized string. All split blocks sit on an 8am-10pm or 8am-midnight daily base, so split rules only change rate/limit inside base hours. **A new string the city adds isn't guessed at:** it becomes a "check the meter" flag. When the city data changes, add the string to the table.
- **Event rates** are parsed (rate + venue names). The event rate runs 3 hr before to 1 hr after the start (Convention Center: all day), only while the meter is enforced. "Event Days 9a-10p Daily" extends enforcement to 9-10 on event days.
- `assess()` walks the stay minute by minute: cost, a plain-English note ("$5.00/hr until 7:40 PM, then $3.00/hr"), over-limit (longest paid run vs strictest limit in it), and no-parking overlap.
- **Time limits count per rule:** the paid run resets when the limit changes, so 1 hr under a 2 hr daytime limit followed by an unlimited evening isn't flagged. A limit of `NO_LIMIT` (0) means none.
- Verified against hand checks: the Rabbit Hole (411 N Washington), Tue 7pm → 2nd St N free; Twins weeknight → Harmon Pl and 2nd St N free, 10th St S $15.33.

## Ranking

**Sides merge:** before walk times are fetched, block faces in the same hundred-block of the same street whose result is identical for this stay (cost, note, rules, warnings, over-limit) fold into one option at the closer face: "2nd St N (400 block, both sides)". Faces that differ in anything stay separate. The map keeps every polygon but gives merged faces the kept option's key, so selecting highlights both. Addresses under 100 read "1–99 block".

Score = cost + $0.20 per walking minute + $25 if the stay runs past the time limit. Blocks with no parking during the stay are left out of the list (grey on the map). Default radius 1,200 m (~15 min walk); the Twins/Harmon spot is ~850 m from Target Field. The panel re-sorts client-side (Best / Closest / Cheapest) with the same weights.

## API Endpoints

```
GET    /api/curb/status            — config flags, per-city meter counts/freshness/errors, saved spots, venues (fixed flag)
POST   /api/curb/meters/refresh    — re-download the city's meters now
GET    /api/curb/search?q=         — Google Places text search
POST   /api/curb/plan              — {lat, lon, name, arrive, hours, radius, event?{venue, start}}
GET    /api/curb/lots?lat=&lon=&radius=
POST   /api/curb/spots             — {name, notes, rate, lat, lon}
PUT    /api/curb/spots/{id}
DELETE /api/curb/spots/{id}
```

## Testing

There are no tests in the repo. `meters.py` and the parsers in `sources.py` have no HA imports, so they can be run against a saved copy of the city data with plain Python. The rest was run once on Andy's PC with stub `homeassistant` modules in a scratch virtualenv with aiohttp. Never test with Andy's real saved spots or home location in anything committed.
