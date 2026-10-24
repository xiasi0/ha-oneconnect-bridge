"""Generic read-only single-number Huawei service adapter.

Several Profiles expose a dedicated read-only numeric service per measurement
where the service id, the service type and the characteristic name are all the
same token. Default contract source: prodId `A0VG` (海尔冰箱), Profile
https://smarthome-drcn.dbankcdn.com/device/guide/A0VG/A0VG.json

| sid                       | st                       | characteristic           | type | range    | method |
| ------------------------- | ------------------------ | ------------------------ | ---- | -------- | ------ |
| refrigeratorTemperatureC  | refrigeratorTemperatureC | refrigeratorTemperatureC | int  | -38..50  | R      |
| freezerTemperatureC       | freezerTemperatureC      | freezerTemperatureC      | int  | -23..50  | R      |
| vtRoomTemperature         | vtRoomTemperature        | vtRoomTemperature        | int  | -38..50  | R      |

All three are whole degrees Celsius with no scaling.

Contract:
  `min` / `max` clamp the reported value (defaults come from the source
  Profiles above, so an unknown sid defaults to no clamping).
  `characteristic` overrides the uploaded field name.
  `factor` scales the reading (default 1) and must be non-zero.

STATUS: candidate. The service contract is implemented and unit-tested, but a
real projection has **not** been confirmed end to end: on this account the
Huawei cloud accepts the `dataSync` ACK (`errcode: 0`) yet never applies the
reported value, so the Smart Life app keeps showing the Profile minimum
(-38 / -23 / -38). See
docs/adapter-verification-real-devices.md for the full observation. Treat this
adapter as unverified until that behaviour is understood.
"""

from __future__ import annotations

from ..models import ServiceRef


_DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    "refrigeratorTemperatureC": (-38, 50),
    "freezerTemperatureC": (-23, 50),
    "vtRoomTemperature": (-38, 50),
}


class ReadonlyNumberServiceAdapter:
    """Adapter for a single read-only numeric characteristic."""

    def __init__(self, service_id: str) -> None:
        self.service_id = service_id

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_id != self.service_id:
            raise ValueError(f"{self.service_id} adapter cannot serve {service.service_id}")
        if not service.service_type:
            raise ValueError(f"{self.service_id} requires a service type")
        unknown = set(dict(service.contract)) - {"min", "max", "characteristic", "factor"}
        if unknown:
            raise ValueError(
                f"{self.service_id} has unsupported contract keys: "
                + ", ".join(sorted(unknown))
            )
        _factor(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError(f"{self.service_id} currently requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in {"sensor", "number", "input_number"}:
            raise ValueError(
                f"{self.service_id} requires a sensor, number or input_number entity"
            )

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        raw = state.attributes.get("value", state.state)
        parsed = _as_number(raw)
        if parsed is None:
            return {}
        minimum, maximum = _limits(service)
        value = parsed * _factor(service)
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return {_characteristic(service): int(round(value))}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        # Read-only on the source Profiles.
        self.validate_service(service)
        self.validate_entities(entity_ids)


ADAPTER_FACTORY = ReadonlyNumberServiceAdapter


def _characteristic(service: ServiceRef) -> str:
    override = service.contract_value("characteristic")
    if isinstance(override, str) and override:
        return override
    if isinstance(service.service_type, str) and service.service_type:
        return service.service_type
    return service.service_id


def _factor(service: ServiceRef) -> float:
    value = service.contract_value("factor", 1)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value == 0:
        raise ValueError(
            f"{service.service_id} contract factor must be a non-zero number"
        )
    return float(value)


def _limits(service: ServiceRef) -> tuple[int | None, int | None]:
    override_min = service.contract_value("min")
    override_max = service.contract_value("max")
    if isinstance(override_min, bool) or not isinstance(override_min, int):
        override_min = None
    if isinstance(override_max, bool) or not isinstance(override_max, int):
        override_max = None
    if override_min is not None or override_max is not None:
        return override_min, override_max
    return _DEFAULT_LIMITS.get(service.service_id, (None, None))


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def register_readonly_number_adapter(service_id: str) -> ReadonlyNumberServiceAdapter:
    """Build one adapter instance for a read-only numeric sid."""
    return ReadonlyNumberServiceAdapter(service_id)
