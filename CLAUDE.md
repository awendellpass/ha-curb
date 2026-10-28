# Curb — Claude Project Conventions

Curb is a Home Assistant custom integration that finds free and cheap parking near a destination for a given arrival time and stay, then hands off to Google Maps with the *parking spot* as the destination. Andy uses it to plan before leaving, not to find an open space live. Same structure and patterns as Perk and Scout (`Documents\perk_ha`, `Documents\scout_ha`).

v1 scope (2026-10-27): Minneapolis meters, OpenStreetMap lots, saved spots (shared by the household), Google Places search and Routes walk times, manual event selection. Deferred: automatic event detection (MLB Stats API for the Twins, Ticketmaster Discovery for Target Center and U.S. Bank Stadium), snow emergency alerts, St. Paul data, spot ratings, holidays.

## Stack

- **Backend:** Python, SQLite (`database.py`), HA's shared aiohttp session
- **Frontend:** Vanilla JS + Leaflet 1.9.4 from unpkg, CARTO dark tiles. Two-file iframe auth pattern: `curb-panel.js` passes `hass.auth.data.access_token` to `curb-app.js` via `postMessage`; every fetch sends `Authorization: Bearer`. Panel host uses `height: 100vh`. The iframe has `allow="geolocation"` for "Near me".
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
  meters.py     — city meter rules -> cost for one stay (no HA imports)
  sources.py    — city meters, Overpass lots, Google Places + Routes (fetch + parse)
  database.py   — SQLite: meters, spots, lots_cache, meta
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

## Meter rules (meters.py)

- Base rule: `Hourly_rat` / `Hours_of_E` / `Days_of_en` / `Hour_Limit`. All 9 hour formats and 3 day formats in the city data parse.
- **Split rates** (8 distinct strings, 191 blocks) and **no-parking windows** (`Restrictio`, ~10 strings) are spelled out in `SPLIT_RULES` / `NO_PARKING`, keyed by the normalized string. All split blocks sit on an 8am-10pm or 8am-midnight daily base, so split rules only change rate/limit inside base hours. **A new string the city adds isn't guessed at:** it becomes a "check the meter" flag. When the city data changes, add the string to the table.
- **Event rates** are parsed (rate + venue names). The event rate runs 3 hr before to 1 hr after the start (Convention Center: all day), only while the meter is enforced. "Event Days 9a-10p Daily" extends enforcement to 9-10 on event days.
- `assess()` walks the stay minute by minute: cost, a plain-English note ("$5.00/hr until 7:40 PM, then $3.00/hr"), over-limit (longest paid run vs strictest limit in it), and no-parking overlap.
- Verified against hand checks: the Rabbit Hole (411 N Washington), Tue 7pm → 2nd St N free; Twins weeknight → Harmon Pl and 2nd St N free, 10th St S $15.33.

## Ranking

Score = cost + $0.20 per walking minute + $25 if the stay runs past the time limit. Blocks with no parking during the stay are left out of the list (grey on the map). Default radius 1,200 m (~15 min walk); the Twins/Harmon spot is ~850 m from Target Field. The panel re-sorts client-side (Best / Closest / Cheapest) with the same weights.

## API Endpoints

```
GET    /api/curb/status            — config flags, meter freshness, saved spots, venues
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
