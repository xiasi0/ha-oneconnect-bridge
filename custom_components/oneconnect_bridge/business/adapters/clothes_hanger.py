"""Huawei `clothesHanger` service adapter for the Hotata airer.

The `clothesHanger` service bundles several characteristics. Default contract
source: prodId `174P` (好太太定制晾衣机), Profile
https://smarthome-drcn.dbankcdn.com/device/guide/174P/174P.json

| characteristic   | type | method | meaning                        |
| ---------------- | ---- | ------ | ------------------------------ |
| action           | int  | RW     | 0 raise / 1 lower / 2 pause     |
| lighting         | bool | RW     | 1 lighting on / 0 off          |
| disinfect        | bool | RW     | 1 disinfect on / 0 off         |
| status           | int  | RW     | 0 other / 1 highest / 2 lowest |
| lightingTime     | int  | RW     | 30..180 minutes                |
| disinfectTime    | int  | R      | 0..30 minutes                  |

The two `*Left` counters are read-only Profile internals and are not mapped.

A single binding may carry several entities: the adapter dispatches each one to
the characteristic implied by its Home Assistant domain, so one child can carry
the airer's lift, light and disinfect switch together.

| HA domain                  | characteristic |
| -------------------------- | -------------- |
| `cover`                    | action         |
| `light`                    | lighting       |
| `switch` / `input_boolean` | disinfect      |
| `sensor` / `number`        | status         |

Contract:
  `fields` optionally narrows which characteristics this child reports.
  `mapping` overrides the `action` table (`{"3": "open"}` style).

Verified against a real projection (1 Hotata airer registered through
OneConnect Bridge 0.1.x; lighting and disinfect on/off plus raise/lower/pause
all confirmed in both directions).
"""

from __future__ import annotations

from ..models import ServiceRef


# Profile 174P: 0 = 升 (raise), 1 = 降 (lower), 2 = 暂停 (pause).
# `open_cover` raises the airer, so 0 must map to open, not close.
_ACTION_SERVICES = {0: "open_cover", 1: "close_cover", 2: "stop_cover"}
_ACTION_DEFAULT = {"open": 0, "opening": 0, "closed": 1, "closing": 1}
_FIELDS = {"action", "lighting", "disinfect", "status"}

# HA domain -> characteristic this entity drives.
_DOMAIN_FIELD = {
    "cover": "action",
    "light": "lighting",
    "switch": "disinfect",
    "input_boolean": "disinfect",
    "sensor": "status",
    "number": "status",
    "input_number": "status",
}


class ClothesHangerServiceAdapter:
    service_id = "clothesHanger"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "clothesHanger":
            raise ValueError("clothesHanger requires st: clothesHanger")
        unknown = set(dict(service.contract)) - {"fields", "mapping"}
        if unknown:
            raise ValueError(
                "clothesHanger has unsupported contract keys: "
                + ", ".join(sorted(unknown))
            )
        if not _fields(service):
            raise ValueError("clothesHanger contract fields must not be empty")

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if not entity_ids:
            raise ValueError("clothesHanger requires at least one HA entity")
        for entity_id in entity_ids:
            domain = entity_id.partition(".")[0]
            if domain not in _DOMAIN_FIELD:
                raise ValueError(
                    f"clothesHanger cannot map domain {domain or '<empty>'}; use "
                    "cover, light, switch, input_boolean, sensor, number or "
                    "input_number"
                )

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        allowed = _fields(service)
        result: dict[str, object] = {}
        for entity_id in entity_ids:
            domain = entity_id.partition(".")[0]
            field = _DOMAIN_FIELD[domain]
            if field not in allowed:
                continue
            state = hass.states.get(entity_id)
            if state is None or state.state in {"unknown", "unavailable"}:
                continue
            value = _read_field(service, field, state)
            if value is not None:
                result[field] = value
            # A cover also carries the airer's position: `status` is the
            # Profile's raise/lower state (1 highest, 2 lowest, 0 in between),
            # while `action` only describes the last movement.
            if domain == "cover" and "status" in allowed:
                position = _cover_status(service, state)
                if position is not None:
                    result["status"] = position
        return result

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        allowed = _fields(service)
        for entity_id in entity_ids:
            field = _DOMAIN_FIELD[entity_id.partition(".")[0]]
            if field not in allowed or field not in payload:
                continue
            value = payload[field]
            if field == "action":
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
                name = _ACTION_SERVICES.get(value)
                if name is None:
                    continue
                await hass.services.async_call(
                    "cover", name, {"entity_id": entity_id}, blocking=True
                )
                continue
            if not isinstance(value, bool):
                continue
            domain = entity_id.partition(".")[0]
            if domain not in {"light", "switch", "input_boolean"}:
                continue
            await hass.services.async_call(
                domain,
                "turn_on" if value else "turn_off",
                {"entity_id": entity_id},
                blocking=True,
            )


ADAPTER = ClothesHangerServiceAdapter()


def _fields(service: ServiceRef) -> set[str]:
    value = service.contract_value("fields")
    if value is None:
        return set(_FIELDS)
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("clothesHanger contract fields must be a non-empty list")
    cleaned = {str(item) for item in value}
    unknown = cleaned - _FIELDS
    if unknown:
        raise ValueError(
            "clothesHanger contract fields has unknown entries: "
            + ", ".join(sorted(unknown))
        )
    return cleaned


def _cover_status(service: ServiceRef, state) -> int | None:
    """Map a cover entity's position onto Profile `status`.

    Profile 174P declares `status` as 0 other / 1 highest / 2 lowest, so the
    cover's `current_position` decides it: 100 means fully raised, 0 means fully
    lowered. Contracts may override the two endpoints through `mapping`.
    """
    if str(state.state) in {"unknown", "unavailable"}:
        return None
    position = (state.attributes or {}).get("current_position")
    if isinstance(position, bool) or not isinstance(position, (int, float)):
        return None
    high, low = 1, 2
    table = service.contract_value("mapping")
    if isinstance(table, dict):
        if isinstance(table.get("highest"), int):
            high = table["highest"]
        if isinstance(table.get("lowest"), int):
            low = table["lowest"]
    if position >= 100:
        return high
    if position <= 0:
        return low
    return 0


def _read_field(service: ServiceRef, field: str, state) -> object | None:
    text = str(state.state)
    if field == "action":
        mapping = dict(_ACTION_DEFAULT)
        table = service.contract_value("mapping")
        if isinstance(table, dict):
            for key, value in table.items():
                if isinstance(key, str) and isinstance(value, str):
                    mapping[value] = key
        raw = mapping.get(text)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if field in {"lighting", "disinfect"}:
        if text == "on":
            return True
        if text == "off":
            return False
        return None
    # status: numeric states only.
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None
