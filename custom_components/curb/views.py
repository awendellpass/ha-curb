"""HTTP API views for Curb."""
from __future__ import annotations

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from . import api, database
from .const import API_BASE
from .sources import SourceError


def async_setup_views(hass: HomeAssistant) -> None:
    # Specific routes before parameterized ones.
    hass.http.register_view(CurbStatusView)
    hass.http.register_view(CurbMetersRefreshView)
    hass.http.register_view(CurbSearchView)
    hass.http.register_view(CurbPlanView)
    hass.http.register_view(CurbLotsView)
    hass.http.register_view(CurbSpotsView)
    hass.http.register_view(CurbSpotView)


def _hass(request):
    return request.app["hass"]


async def _body(request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class _View(HomeAssistantView):
    requires_auth = True

    def error(self, msg: str, status: int = 400):
        return self.json({"error": msg}, status_code=status)


# ---------------------------------------------------------------------------
# GET  /api/curb/status          — config flags, meter freshness, saved spots
# POST /api/curb/meters/refresh  — re-download the city's meters now
# ---------------------------------------------------------------------------

class CurbStatusView(_View):
    url = f"{API_BASE}/status"
    name = "api:curb:status"

    async def get(self, request):
        return self.json(await api.async_status(_hass(request)))


class CurbMetersRefreshView(_View):
    url = f"{API_BASE}/meters/refresh"
    name = "api:curb:meters_refresh"

    async def post(self, request):
        return self.json(await api.async_refresh_meters(_hass(request), force=True))


# ---------------------------------------------------------------------------
# GET  /api/curb/search?q=              — Google Places text search near home
# POST /api/curb/plan                   — ranked parking for a destination + stay
# GET  /api/curb/lots?lat=&lon=&radius= — OpenStreetMap lots (slow when uncached)
# ---------------------------------------------------------------------------

class CurbSearchView(_View):
    url = f"{API_BASE}/search"
    name = "api:curb:search"

    async def get(self, request):
        try:
            results = await api.async_search(_hass(request), request.query.get("q", ""))
        except SourceError as err:
            return self.error(str(err), 502)
        return self.json({"results": results})


class CurbPlanView(_View):
    url = f"{API_BASE}/plan"
    name = "api:curb:plan"

    async def post(self, request):
        try:
            return self.json(await api.async_plan(_hass(request), await _body(request)))
        except ValueError as err:
            return self.error(str(err))


class CurbLotsView(_View):
    url = f"{API_BASE}/lots"
    name = "api:curb:lots"

    async def get(self, request):
        try:
            return self.json(await api.async_lots(_hass(request), dict(request.query)))
        except ValueError as err:
            return self.error(str(err))


# ---------------------------------------------------------------------------
# POST   /api/curb/spots       — save a spot
# PUT    /api/curb/spots/{id}  — edit (partial)
# DELETE /api/curb/spots/{id}
# ---------------------------------------------------------------------------

class CurbSpotsView(_View):
    url = f"{API_BASE}/spots"
    name = "api:curb:spots"

    async def post(self, request):
        hass = _hass(request)
        try:
            data = api.clean_spot(await _body(request))
        except ValueError as err:
            return self.error(str(err))
        spot_id = await hass.async_add_executor_job(database.add_spot, hass, data)
        return self.json(await hass.async_add_executor_job(database.get_spot, hass, spot_id))


class CurbSpotView(_View):
    url = f"{API_BASE}/spots/{{spot_id}}"
    name = "api:curb:spot"

    async def put(self, request, spot_id):
        hass = _hass(request)
        try:
            sid = int(spot_id)
            data = api.clean_spot(await _body(request), partial=True)
        except ValueError as err:
            return self.error(str(err))
        if not await hass.async_add_executor_job(database.update_spot, hass, sid, data):
            return self.error("Spot not found", 404)
        return self.json(await hass.async_add_executor_job(database.get_spot, hass, sid))

    async def delete(self, request, spot_id):
        hass = _hass(request)
        try:
            sid = int(spot_id)
        except ValueError:
            return self.error("Bad spot id")
        if not await hass.async_add_executor_job(database.delete_spot, hass, sid):
            return self.error("Spot not found", 404)
        return self.json({"deleted": sid})
