# Curb

A Home Assistant panel for finding free and cheap parking near where you're going, so you can navigate to the parking spot instead of circling the block.

Search a destination (or use your location or a map tap), set when you'll arrive and how long you'll stay, and Curb ranks the metered blocks and your saved spots by what they'll actually cost for that stay and how far the walk is. One tap opens Google Maps with the parking spot as the destination.

## What it knows

- **St. Paul parking meters** from the city's downtown parking map: day and night rates, time limits, rush-hour and overnight restrictions, Sundays and city holidays, and event rates for Grand Casino Arena and CHS Field, plus the city's list of ramps and lots with (2020) rates.
- **Minneapolis parking meters** from the city's public meter map: rate, enforcement hours and days, time limits, split rates, tow/no-stopping windows, and event rates for Target Field, Target Center, U.S. Bank Stadium, Huntington Bank Stadium and the Convention Center. Refreshed weekly.
- **Your saved spots**, shared by everyone in the house.
- **Lots and ramps** from OpenStreetMap: locations and names, but rarely prices.
- **Walk times** from Google Routes (falls back to estimates).

It doesn't know whether a space is open right now, unmetered street rules, or Minneapolis holidays. Posted signs win.

## Install

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/awendellpass/ha-curb` (Integration).
2. Install **Curb**, then add to `configuration.yaml`:

   ```yaml
   curb:
     google_api_key: !secret curb_google_api_key
   ```

   and to `secrets.yaml`:

   ```yaml
   curb_google_api_key: "your key"
   ```

   The key needs **Places API (New)** and **Routes API** enabled; restrict it to those two. A bare `curb:` line also works: no place search, estimated walk times.
3. Restart Home Assistant. **Curb** appears in the sidebar; the meter map downloads about 30 seconds after startup.
