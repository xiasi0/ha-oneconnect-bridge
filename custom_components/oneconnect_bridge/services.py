"""OneConnect Bridge 服务."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

async def async_setup_services(hass: HomeAssistant) -> None:
    """Register OneConnect Bridge services."""
    if hass.services.has_service(DOMAIN, "start_pairing"):
        return

    data = hass.data[DOMAIN]

    async def start_pairing(call: ServiceCall) -> None:
        serial = call.data["serial"]
        runtime = data["manager"].get(serial)
        if runtime is None:
            raise HomeAssistantError(f"unknown OneConnect Bridge gateway: {serial}")
        await hass.async_add_executor_job(runtime.start_pairing)

    hass.services.async_register(
        DOMAIN,
        "start_pairing",
        start_pairing,
        schema=vol.Schema({vol.Required("serial"): vol.Coerce(str)}),
    )