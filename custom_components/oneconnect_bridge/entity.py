"""OneConnect Bridge 实体基类."""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN
from .gateway import GatewayRuntime

class GatewayEntity(Entity):
    """Base entity bound to one Python gateway instance."""

    _attr_has_entity_name = True

    def __init__(self, runtime: GatewayRuntime) -> None:
        self.runtime = runtime
        self._remove_listener = None

    @property
    def device_info(self) -> DeviceInfo:
        info = self.runtime.device_info
        return DeviceInfo(
            identifiers={(DOMAIN, self.runtime.serial)},
            name=info["name"],
            manufacturer=info["manufacturer"],
            model=info["model"],
            serial_number=self.runtime.serial,
        )

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.runtime.add_listener(self._runtime_changed)
        self.async_on_remove(self._remove_runtime_listener)

    def _remove_runtime_listener(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    def _runtime_changed(self) -> None:
        self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)