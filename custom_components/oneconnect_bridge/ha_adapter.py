"""OneConnect Bridge HA 适配层（云线程与 HA 事件循环间的同步桥）."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .business.models import BindingField, ChildPlan

_LOGGER = logging.getLogger(__name__)

class HAEntityBridge:
    """Read/call HA entities from the gateway's non-event-loop worker."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def read_child_state(self, child: ChildPlan) -> dict[str, dict[str, object]]:
        result = self._run(self._async_read_child_state(child))
        _LOGGER.debug("HA state snapshot child=%s state=%s", child.key, result)
        return result

    def apply_child_command(
        self, child: ChildPlan, service_id: str, payload: dict[str, object]
    ) -> None:
        _LOGGER.debug(
            "HA command received child=%s sid=%s payload=%s",
            child.key,
            service_id,
            payload,
        )
        self._run(self._async_apply_child_command(child, service_id, payload))

    def setup_state_listener(self, plan, callback):
        """Forward bound HA state changes to the gateway worker."""

        entity_ids = {
            binding.entity_id
            for child in plan.children
            for binding in child.bindings
        }
        if not entity_ids:
            return lambda: None

        async def state_changed(event) -> None:
            entity_id = event.data.get("entity_id")
            for child in plan.children:
                if not any(binding.entity_id == entity_id for binding in child.bindings):
                    continue
                snapshot = await self._async_read_child_state(child)
                _LOGGER.debug(
                    "HA state event child=%s entity=%s snapshot=%s",
                    child.key,
                    entity_id,
                    snapshot,
                )
                callback(child.key, snapshot)

        return async_track_state_change_event(self._hass, entity_ids, state_changed)

    def schedule_startup_sync(self, plan, callback, *, attempts: int = 12):
        """Retry initial snapshots while HA entities/cloud session restore."""

        remaining = attempts
        cancel_handle = None
        cancelled = False

        async def retry(_now) -> None:
            nonlocal remaining, cancel_handle
            if cancelled:
                return
            pending = False
            for child in plan.children:
                snapshot = await self._async_read_child_state(child)
                if snapshot:
                    _LOGGER.debug(
                        "Startup state snapshot child=%s state=%s",
                        child.key,
                        snapshot,
                    )
                    if callback(child.key, snapshot) is False:
                        pending = True
                else:
                    pending = True
            remaining -= 1
            if pending and remaining > 0:
                cancel_handle = async_call_later(self._hass, 5, retry)

        cancel_handle = async_call_later(self._hass, 2, retry)

        def cancel() -> None:
            nonlocal cancelled
            # 防止进行中的 retry 在句柄取消后又排一次 async_call_later
            cancelled = True
            if cancel_handle is not None:
                cancel_handle()

        return cancel

    def _run(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._hass.loop)
        return future.result(timeout=15)

    async def _async_read_child_state(
        self, child: ChildPlan
    ) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for binding in child.bindings:
            state = self._hass.states.get(binding.entity_id)
            if state is None or state.state in {"unknown", "unavailable"}:
                continue
            value = _read_binding_value(state.state, state.attributes, binding)
            _LOGGER.debug(
                "HA binding read child=%s sid=%s field=%s entity=%s "
                "state=%s attribute=%s converted=%s",
                child.key,
                binding.service_id,
                binding.field_name,
                binding.entity_id,
                state.state,
                binding.attribute,
                value,
            )
            if value is None:
                continue
            result.setdefault(binding.service_id, {})[binding.field_name] = value
        return result

    async def _async_apply_child_command(
        self, child: ChildPlan, service_id: str, payload: dict[str, object]
    ) -> None:
        bindings = [binding for binding in child.bindings if binding.service_id == service_id]
        for binding in bindings:
            if binding.field_name not in payload:
                continue
            await _call_binding(self._hass, binding, payload[binding.field_name])

def _read_binding_value(
    state_value: str, attributes: dict[str, Any], binding: BindingField
) -> object | None:
    if binding.source == "state":
        value: object = state_value
    elif binding.source == "attribute" and binding.attribute:
        value = attributes.get(binding.attribute)
    else:
        return None
    mapping = binding.ha_to_huawei.get("map")
    if isinstance(mapping, dict):
        value = mapping.get(str(value), value)
    if binding.field_name == "brightness" and isinstance(value, (int, float)):
        return max(0, min(100, round(float(value) * 100 / 255)))
    return value

async def _call_binding(
    hass: HomeAssistant, binding: BindingField, value: object
) -> None:
    domain, _separator, _object_id = binding.entity_id.partition(".")
    if not domain or not _object_id:
        return
    if binding.field_name == "on":
        service = "turn_on" if bool(value) else "turn_off"
        _LOGGER.debug(
            "Calling HA service %s.%s entity=%s value=%s",
            domain,
            service,
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            domain, service, {"entity_id": binding.entity_id}, blocking=True
        )
        return
    if binding.field_name == "brightness":
        brightness = max(0, min(100, int(value)))
        _LOGGER.debug(
            "Calling HA service %s.turn_on entity=%s brightness_pct=%s",
            domain,
            binding.entity_id,
            brightness,
        )
        await hass.services.async_call(
            domain,
            "turn_on",
            {"entity_id": binding.entity_id, "brightness_pct": brightness},
            blocking=True,
        )
        return
    if binding.field_name == "colorTemperature":
        kelvin = int(value)
        # 华为侧色温范围由产品档案声明，可能与具体灯的能力不符；
        # 收敛到实体声明的支持范围，避免 HA 服务校验直接拒绝
        state = hass.states.get(binding.entity_id)
        attributes = state.attributes if state is not None else {}
        min_kelvin = attributes.get("min_color_temp_kelvin")
        max_kelvin = attributes.get("max_color_temp_kelvin")
        if isinstance(min_kelvin, (int, float)) and kelvin < min_kelvin:
            kelvin = int(min_kelvin)
        if isinstance(max_kelvin, (int, float)) and kelvin > max_kelvin:
            kelvin = int(max_kelvin)
        _LOGGER.debug(
            "Calling HA service %s.turn_on entity=%s color_temp_kelvin=%s",
            domain,
            binding.entity_id,
            kelvin,
        )
        await hass.services.async_call(
            domain,
            "turn_on",
            {"entity_id": binding.entity_id, "color_temp_kelvin": kelvin},
            blocking=True,
        )