"""OneConnect Bridge 网关诊断传感器."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from . import HhbConfigEntry
from .entity import GatewayEntity
from .gateway import GatewayRuntime

GATEWAY_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="serial",
        translation_key="serial",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:numeric",
    ),
    SensorEntityDescription(
        key="child_count",
        translation_key="child_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:counter",
    ),
)


def _build_sensor(
    description: SensorEntityDescription, runtime: GatewayRuntime
) -> SensorEntity:
    """每个诊断描述对应一个传感器类（按 key 分派)."""

    if description.key == "child_count":
        return GatewayChildCountSensor(description, runtime)
    return GatewaySerialSensor(description, runtime)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HhbConfigEntry,
    async_add_entities,
) -> None:
    runtime = entry.runtime_data.runtime
    async_add_entities(
        _build_sensor(description, runtime) for description in GATEWAY_SENSORS
    )


class GatewaySerialSensor(GatewayEntity, SensorEntity):
    def __init__(self, description: SensorEntityDescription, runtime: GatewayRuntime) -> None:
        super().__init__(runtime)
        self.entity_description = description
        self._attr_unique_id = f"{runtime.serial}_{description.key}"

    @property
    def native_value(self) -> str:
        return self.runtime.serial


class GatewayChildCountSensor(GatewayEntity, SensorEntity):
    """Expose the number of child-device entries configured for this gateway."""

    def __init__(self, description: SensorEntityDescription, runtime: GatewayRuntime) -> None:
        super().__init__(runtime)
        self.entity_description = description
        self._attr_unique_id = f"{runtime.serial}_{description.key}"

    @property
    def native_value(self) -> int:
        return self.runtime.child_count
