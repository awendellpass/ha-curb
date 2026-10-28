"""Curb — free and cheap parking planner for Home Assistant."""
from __future__ import annotations

import logging
import os
from datetime import timedelta

import voluptuous as vol
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from . import api, database
from .const import CONF_GOOGLE_API_KEY, DOMAIN, FRONTEND_PATH, STARTUP_DELAY_SECONDS
from .panel import async_register_panel
from .views import async_setup_views

_LOGGER = logging.getLogger(__name__)

# A bare `curb:` line runs on city meters + saved spots with estimated walk
# times; google_api_key adds destination search and real walk times. The key
# lives in secrets.yaml (curb_google_api_key), never in this folder.
CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Any(None, vol.Schema({
        vol.Optional(CONF_GOOGLE_API_KEY): cv.string,
    }))},
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    conf = config.get(DOMAIN) or {}
    hass.data[DOMAIN] = {"google_api_key": conf.get(CONF_GOOGLE_API_KEY, "")}
    await hass.async_add_executor_job(database.init_db, hass)

    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_PATH, frontend_dir, cache_headers=False)]
    )
    async_setup_views(hass)
    await async_register_panel(hass)

    # Meters are re-downloaded weekly; the daily check is a no-op until stale.
    async def _refresh(_now=None):
        await api.async_refresh_meters(hass)

    async_call_later(hass, STARTUP_DELAY_SECONDS, _refresh)
    async_track_time_interval(hass, _refresh, timedelta(days=1))

    _LOGGER.info("Curb loaded")
    return True
