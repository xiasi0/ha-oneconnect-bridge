"""OneConnect Bridge 网关连接状态实体."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant

from . import HhbConfigEntry
from .const import GATEWAY_STATUS_ONLINE
from .entity import GatewayEntity
from .gateway import GatewayRuntime

GATEWAY_BINARY_SENSORS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: HhbConfigEntry,
    async_add_entities,
) -> None:
    runtime = entry.runtime_data.runtime
    async_add_entities(
        GatewayOnlineEntity(description, runtime) for description in GATEWAY_BINARY_SENSORS
    )


class GatewayOnlineEntity(GatewayEntity, BinarySensorEntity):
    """Expose the cloud online state of the parent gateway."""

    def __init__(self, description: BinarySensorEntityDescription, runtime: GatewayRuntime) -> None:
        super().__init__(runtime)
        self.entity_description = description
        self._attr_unique_id = f"{runtime.serial}_{description.key}"

    @property
    def is_on(self) -> bool:
        return self.runtime.status == GATEWAY_STATUS_ONLINE

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"phase": self.runtime.status}

    @property
    def available(self) -> bool:
        return self.runtime.status != "stopped"