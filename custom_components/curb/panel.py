from homeassistant.components.frontend import async_register_built_in_panel
from homeassistant.core import HomeAssistant

from .const import DOMAIN, FRONTEND_PATH


async def async_register_panel(hass: HomeAssistant) -> None:
    async_register_built_in_panel(
        hass,
        "custom",
        sidebar_title="Curb",
        sidebar_icon="mdi:parking",
        frontend_url_path=DOMAIN,
        config={"_panel_custom": {
            "name": "curb-panel",
            "module_url": f"{FRONTEND_PATH}/curb-panel.js",
        }},
        require_admin=False,
    )
