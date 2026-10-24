"""Huawei `switch1.on` adapter for multi-key switch Profiles.

Some switch Profiles (for example Huawei OSLO wireless switches) expose one
service per physical key instead of the single-key `switch` service. The
`switch1` service carries the same `on` characteristic, so the contract is the
same as the built-in `switch` adapter with a different service id.

Default contract source: prodId `ZG2R` (智能开关 奥斯陆(无线版) 1键), Profile
https://smarthome-drcn.dbankcdn.com/device/guide/ZG2R/ZG2R.json
`switch1.on` is `bool`; `on_type: bool` uploads a JSON boolean while the
default `on_type: int` uploads 0/1.

Verified against a real projection (20 ZG2R switch children registered through
OneConnect Bridge 0.1.x, state and command round-trips confirmed).
"""

from __future__ import annotations

from ..models import ServiceRef


class Switch1ServiceAdapter:
    service_id = "switch1"
    _domains = {"light", "switch", "fan", "climate", "sensor", "binary_sensor", "humidifier", "input_boolean", "lock"}

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type not in {"switch", "binarySwitch"}:
            raise ValueError("switch1 requires st: switch or binarySwitch")
        unknown = set(dict(service.contract)) - {"on_type", "on_states", "off_states"}
        if unknown:
            raise ValueError(
                f"switch1 has unsupported contract keys: {', '.join(sorted(unknown))}"
            )
        if _on_type(service) not in {"int", "bool"}:
            raise ValueError("switch1 contract on_type must be int or bool")

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("switch1 currently requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in self._domains:
            raise ValueError("switch1 requires a light, switch, fan, climate, sensor, binary_sensor, humidifier, input_boolean or lock entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        domain = entity_ids[0].partition(".")[0]
        if domain == "climate":
            # A climate entity is on whenever it is not off.
            is_on = state.state != "off"
        elif state.state in {"on", "off"}:
            is_on = state.state == "on"
        elif state.state in {"locked", "unlocked", "locking", "unlocking"}:
            is_on = state.state in {"unlocked", "unlocking"}
        else:
            truth = _custom_truth(service, state.state)
            if truth is None:
                return {}
            is_on = truth
        return {"on": is_on if _on_type(service) == "bool" else int(is_on)}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        enabled = _parse_on_value(service, payload.get("on"))
        if enabled is None:
            return
        domain, _separator, _object_id = entity_ids[0].partition(".")
        # Read-only domains have no command path.
        if domain in {"sensor", "binary_sensor"}:
            return
        if domain == "lock":
            # HA locks expose lock/unlock, not turn_on/turn_off.
            await hass.services.async_call(
                "lock",
                "unlock" if enabled else "lock",
                {"entity_id": entity_ids[0]},
                blocking=True,
            )
            return
        await hass.services.async_call(
            domain,
            "turn_on" if enabled else "turn_off",
            {"entity_id": entity_ids[0]},
            blocking=True,
        )


ADAPTER = Switch1ServiceAdapter()


def _parse_on_value(service: ServiceRef, value: object) -> bool | None:
    if _on_type(service) == "int":
        if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1}:
            return None
        return bool(value)
    return value if isinstance(value, bool) else None


def _custom_truth(service: ServiceRef, state: str) -> bool | None:
    """Declared state sets for domains whose states are not on/off."""
    custom_on = service.contract_value("on_states")
    if isinstance(custom_on, (list, tuple)) and custom_on:
        text = str(state)
        if text in {str(v) for v in custom_on}:
            return True
        custom_off = service.contract_value("off_states")
        if isinstance(custom_off, (list, tuple)) and custom_off:
            return False if text in {str(v) for v in custom_off} else None
        return False
    return None


def _on_type(service: ServiceRef) -> str:
    return service.contract_value("on_type", "int")
