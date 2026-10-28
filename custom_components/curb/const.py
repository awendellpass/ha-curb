"""Constants for the Curb integration."""

DOMAIN = "curb"
DB_NAME = "curb.db"
FRONTEND_PATH = "/curb_frontend"
API_BASE = "/api/curb"

# configuration.yaml key read by CONFIG_SCHEMA in __init__.py.
CONF_GOOGLE_API_KEY = "google_api_key"

# ── Minneapolis meters (ArcGIS layer behind the city's meter map) ────────
# Found 2026-10-27 via the meter map web app (item f8005414ee1a48338aba6bce7fee4280).
# 1,046 block faces citywide; maxRecordCount 1000, so it's paged.
METERS_URL = (
    "https://services.arcgis.com/afSMGVsC7QlRK1kZ/arcgis/rest/services/"
    "msvcPW_ParkingMeters2/FeatureServer/0/query"
)
METERS_PAGE = 1000
METERS_REFRESH_DAYS = 7
# A refresh that returns far fewer meters than we hold is treated as a bad
# response, not a citywide meter removal.
METERS_MIN_KEEP_RATIO = 0.5
# Space_Stat values seen: Active, active, new, New, Activw, Remove.
METER_SKIP_STATUS = ("remove",)

# ── St. Paul (city ArcGIS org behind its "Parking in Downtown" map) ──────
# Found 2026-10-28 via web app 95361716a5434600b04e07e301c2373e. Spaces are
# points (1,882), named and grouped using the city's street centerlines.
STP_ARCGIS = "https://services1.arcgis.com/9meaaHE3uiba0zr8/arcgis/rest/services"
STP_SPACES_URL = f"{STP_ARCGIS}/Metered_Street_Parking_Spaces_(View)/FeatureServer/3/query"
STP_CENTERLINES_URL = f"{STP_ARCGIS}/StreetCenterlines/FeatureServer/{{layer}}/query"
# 53 ramps/lots with hourly/daily rates; the rates were last updated in 2020.
STP_FACILITIES_URL = f"{STP_ARCGIS}/DowntownParkingv5_view/FeatureServer/0/query"

CITIES = {"minneapolis": "Minneapolis", "stpaul": "St. Paul"}
FACILITY_DEDUPE_M = 60   # an OSM lot this close to a city-listed one is the same lot

# ── Unmetered curbs (curbs.py) ───────────────────────────────────────────
# Rough city extents (s, w, n, e): a plan only asks a city for streets when
# its search area overlaps the city.
CITY_BOUNDS = {
    "minneapolis": (44.889, -93.330, 45.052, -93.193),
    "stpaul": (44.886, -93.208, 44.992, -93.004),
}
# St. Paul sign inventory: 72k signs with their text, edited 2026-08.
STP_SIGNS_URL = f"{STP_ARCGIS}/Traffic_Signs/FeatureServer/4/query"
# St. Paul 2025 parking tickets (57k, with lat/lon and violation).
STP_TICKETS_URL = f"{STP_ARCGIS}/ParkingCitationsSPPD_2025_public_view/FeatureServer/0/query"
MPLS_ARCGIS = "https://services.arcgis.com/afSMGVsC7QlRK1kZ/arcgis/rest/services"
MPLS_CENTERLINE_URL = f"{MPLS_ARCGIS}/MPLS_Centerline/FeatureServer/0/query"
# Critical Parking Area (permit) block faces: 330, "2 HR Except By Permit" +
# "8am-6pm Mon-Fri", edited 2024-11. Minneapolis's sign inventory uses
# numeric codes with no published legend, so this is all it gets.
MPLS_PERMIT_URL = f"{MPLS_ARCGIS}/CPA_Update/FeatureServer/0/query"
# A1-A25 are freeways and highways; A30 = arterials, A40 = local streets.
MPLS_STREET_CLASSES = ("A30", "A40")
CURBS_CACHE_DAYS = 7
CURBS_PAD_M = 150            # fetched area beyond the radius, so nearby searches share a cache entry
TICKETS_WARN = 15            # no-parking tickets nearby in 2025 before a block gets a warning

# ── OpenStreetMap lots (Overpass) ────────────────────────────────────────
# Lots and ramps exist in OSM but almost never have prices (146 features near
# the North Loop, ~none with fee info), so they're listed, not priced.
# Public Overpass servers are often busy (timeouts/502s in testing), so each
# is tried in turn.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
# The main server took ~26 s for a lots query in testing (2026-10-27), so lots
# load separately from the plan (GET /lots) and never hold it up.
OVERPASS_TIMEOUT_SECONDS = 40
LOTS_CACHE_DAYS = 7
LOTS_SKIP_ACCESS = ("private", "customers", "permit", "no", "delivery", "residents", "employees")
MAX_LOTS = 12

# ── Google (key restricted to Places API (New) + Routes API) ────────────
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = "places.id,places.displayName,places.formattedAddress,places.location"
ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
ROUTES_FIELDS = "originIndex,destinationIndex,duration,distanceMeters,condition"
WALK_CACHE_HOURS = 24
FETCH_TIMEOUT_SECONDS = 30

# ── Ranking ──────────────────────────────────────────────────────────────
DEFAULT_RADIUS_M = 1200      # ~15 min walk; the Twins/Harmon spot is ~850 m out
MAX_RADIUS_M = 2000
WALK_M_PER_MIN = 80          # ~3 mph, for straight-line estimates
WALK_DETOUR = 1.3            # streets vs straight line, for estimates
WALK_VALUE_PER_MIN = 0.20    # $ a minute of walking is worth when ranking
OVER_LIMIT_PENALTY = 25.0    # $ added when the stay runs past the time limit
# Unmetered curbs are free but unverified, so they rank a little behind a
# meter that's free at that hour (less so where parking is posted), and
# further back with a zone on part of the side or a lot of tickets nearby.
UNMETERED_PENALTY = 1.0
POSTED_PARKING_PENALTY = 0.25
PARTIAL_SIGNS_PENALTY = 1.0
TICKETS_PENALTY = 2.0
MAJOR_STREET_PENALTY = 2.0
WALKS_PER_PLAN = 25          # candidates sent to Google for real walk times
MAX_OPTIONS = 15
MAX_STAY_MINUTES = 12 * 60

STARTUP_DELAY_SECONDS = 30
