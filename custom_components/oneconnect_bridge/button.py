"""OneConnect Bridge 网关按钮."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant

from . import HhbConfigEntry
from .entity import GatewayEntity
from .gateway import GatewayRuntime

GATEWAY_BUTTONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key="reload_children",
        translation_key="reload_children",
    ),
)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: HhbConfigEntry,
    async_add_entities,
) -> None:
    runtime = entry.runtime_data.runtime
    async_add_entities(
        GatewayReloadChildrenButton(description, entry, runtime)
        for description in GATEWAY_BUTTONS
    )

class GatewayReloadChildrenButton(GatewayEntity, ButtonEntity):
    """Reload only this gateway's mapping.yaml and reconcile its children."""

    def __init__(
        self,
        description: ButtonEntityDescription,
        entry: HhbConfigEntry,
        runtime: GatewayRuntime,
    ) -> None:
        super().__init__(runtime)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{runtime.serial}_{description.key}"

    async def async_press(self) -> None:
        callback = self._entry.runtime_data.reload_children
        if callback is not None:
            await callback()