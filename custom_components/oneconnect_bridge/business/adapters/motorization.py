"""Huawei `motorization` service adapter for motorised covers.

Covers the Dooya `motorization` service, which carries three independent
characteristics:

| characteristic  | type | range | method | meaning                    |
| --------------- | ---- | ----- | ------ | -------------------------- |
| operationMode   | int  | 0..5  | W      | 1 open / 2 close / 3 stop  |
| targetPercent   | int  | 1..100| RW     | requested position, %      |
| currentPercent  | int  | 1..100| R      | reported position, %       |

The Profile documents 1..100 for both percent characteristics while Home
Assistant covers report 0..100 (0 fully closed). The adapter defaults to the
HA range; pass `min: 1` for a Profile that truly rejects 0.

Note that `motorization.action` is an OTA trigger on this Profile
(0 check / 1 start upgrade) and is deliberately **not** mapped here.

Default contract source: prodId `1004` (杜亚电动窗帘 DT360E), Profile
https://smarthome-drcn.dbankcdn.com/device/guide/1004/1004.json

Verified against a real projection (4 Dooya curtain children plus one
projection-screen child registered through OneConnect Bridge 0.1.x;
HA→Huawei `currentPercent` reporting and Huawei→HA open/close/stop plus
`set_cover_position` all confirmed).
"""

from __future__ import annotations

from ..models import ServiceRef


class MotorizationServiceAdapter:
    service_id = "motorization"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "motorization":
            raise ValueError("motorization requires st: motorization")
        unknown = set(dict(service.contract)) - {"min", "max"}
        if unknown:
            raise ValueError(
                "motorization has unsupported contract keys: "
                + ", ".join(sorted(unknown))
            )
        _limits(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("motorization currently requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain != "cover":
            raise ValueError("motorization requires a cover entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        minimum, maximum = _limits(service)
        result: dict[str, object] = {}
        position = state.attributes.get("current_position")
        if isinstance(position, bool) or not isinstance(position, (int, float)):
            return {}
        result["currentPercent"] = _clamp(round(position), minimum, maximum)
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
        minimum, maximum = _limits(service)
        operation = payload.get("operationMode")
        if not isinstance(operation, bool) and isinstance(operation, int):
            if operation == 1:
                await hass.services.async_call(
                    "cover", "open_cover", {"entity_id": entity_ids[0]}, blocking=True
                )
                return
            if operation == 2:
                await hass.services.async_call(
                    "cover", "close_cover", {"entity_id": entity_ids[0]}, blocking=True
                )
                return
            if operation == 3:
                await hass.services.async_call(
                    "cover", "stop_cover", {"entity_id": entity_ids[0]}, blocking=True
                )
                return
        target = payload.get("targetPercent")
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            return
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": entity_ids[0], "position": _clamp(round(target), minimum, maximum)},
            blocking=True,
        )


ADAPTER = MotorizationServiceAdapter()


def _limits(service: ServiceRef) -> tuple[int, int]:
    # HA covers report 0..100 (0 fully closed), so 0 is the default lower bound
    # even though the Dooya Profile documents 1..100.
    minimum = service.contract_value("min", 0)
    maximum = service.contract_value("max", 100)
    if isinstance(minimum, bool) or not isinstance(minimum, int):
        minimum = 1
    if isinstance(maximum, bool) or not isinstance(maximum, int):
        maximum = 100
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    return minimum, maximum


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))
