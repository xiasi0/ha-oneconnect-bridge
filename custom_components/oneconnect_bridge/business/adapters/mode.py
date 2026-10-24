"""Huawei `mode.mode` service adapter for enum-valued mode services.

Maps a Home Assistant `select`/`climate` hvac mode onto the Profile enum value.
The default mapping follows prodId `A0E0` (海尔智能空调), Profile
https://smarthome-drcn.dbankcdn.com/device/guide/A0E0/A0E0.json
whose `mode` characteristic is an `enum` with these values:

| enumVal | descEn       | descCh | HA hvac_mode  |
| ------- | ------------ | ------ | ------------- |
| 1       | auto         | 自动   | auto / heat_cool |
| 2       | refrigerate  | 制冷   | cool          |
| 3       | heat         | 制热   | heat          |
| 4       | ventilate    | 通风   | fan_only      |
| 5       | dehumidify   | 除湿   | dry           |

Profile enum sets differ per product, so the whole table is overridable through
the `mapping` contract key (`{"2": "cool"}`). Values outside the table are
dropped instead of guessed.

Verified against a real projection (8 A0E0 air-conditioner children registered
through OneConnect Bridge 0.1.x; HA→Huawei mode reporting and Huawei→HA
`set_hvac_mode` both confirmed).
"""

from __future__ import annotations

from ..models import ServiceRef


_DEFAULT_MAPPING = {
    "1": "auto",
    "2": "cool",
    "3": "heat",
    "4": "fan_only",
    "5": "dry",
}

_REVERSE_DEFAULT = {value: key for key, value in _DEFAULT_MAPPING.items()}


class ModeServiceAdapter:
    service_id = "mode"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "mode":
            raise ValueError("mode requires st: mode")
        unknown = set(dict(service.contract)) - {"mapping"}
        if unknown:
            raise ValueError(
                f"mode has unsupported contract keys: {', '.join(sorted(unknown))}"
            )
        _mapping(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("mode currently requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in {"climate", "select"}:
            raise ValueError("mode requires a climate or select entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        mapping = _mapping(service)
        domain = entity_ids[0].partition(".")[0]
        if domain == "climate":
            hvac = state.state
            if hvac == "heat_cool":
                hvac = "auto"
        else:
            hvac = state.state
        reverse = {value: key for key, value in mapping.items()}
        enum_value = reverse.get(hvac)
        if enum_value is None:
            return {}
        return {"mode": int(enum_value)}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, str] if False else tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        value = payload.get("mode")
        if isinstance(value, bool) or not isinstance(value, int):
            return
        mapping = _mapping(service)
        hvac = mapping.get(str(value))
        if hvac is None:
            return
        domain = entity_ids[0].partition(".")[0]
        if domain == "climate":
            await hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": entity_ids[0], "hvac_mode": hvac},
                blocking=True,
            )
        else:
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": entity_ids[0], "option": hvac},
                blocking=True,
            )


ADAPTER = ModeServiceAdapter()


def _mapping(service: ServiceRef) -> dict[str, str]:
    override = service.contract_value("mapping")
    if not isinstance(override, dict) or not override:
        return dict(_DEFAULT_MAPPING)
    cleaned = {}
    for key, value in override.items():
        if isinstance(key, str) and isinstance(value, str) and key and value:
            cleaned[key] = value
    return cleaned or dict(_DEFAULT_MAPPING)
