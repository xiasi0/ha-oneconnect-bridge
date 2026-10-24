"""Huawei remaining-time service adapter (`leftTime`, `lefttime1`, ...).

Reports one integer time characteristic as the remaining minutes of a running
program. Default contract source: prodId `A2GQ` (西门子干衣机) `leftTime.time`
and prodId `A2GT` (西门子洗碗机) `lefttime1.lefttime`, both `int` 0..1440
minutes, Profile URLs
https://smarthome-drcn.dbankcdn.com/device/guide/A2GQ/A2GQ.json and
https://smarthome-drcn.dbankcdn.com/device/guide/A2GT/A2GT.json

Contract:
  `characteristic` defaults per sid from the source Profiles (`time` for A2GQ,
  `lefttime` for A2GT) and can always be overridden explicitly.
  `min` / `max` clamp the reported value (defaults 0..1440 from the Profiles).
  `source_attribute` selects the HA attribute to read; defaults to
  `remaining_time` (minutes) when the bound entity exposes it. When the
  attribute is absent the entity state itself is parsed, because HA states are
  always strings.

The characteristic is read-only on both source Profiles, so no
`apply_command` mapping is provided.
"""

from __future__ import annotations

from ..models import ServiceRef


# Profile-documented characteristic names. Both source Profiles reuse the sid as
# the service type but name the characteristic differently.
_DEFAULT_CHARACTERISTICS = {
    "leftTime": "time",       # A2GQ: sid=leftTime, st=time,   char=time
    "lefttime1": "lefttime",  # A2GT: sid=lefttime1, st=lefttime1, char=lefttime
}


class LeftTimeServiceAdapter:
    def __init__(self, service_id: str) -> None:
        self.service_id = service_id

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_id != self.service_id:
            raise ValueError(f"{self.service_id} adapter cannot serve {service.service_id}")
        unknown = set(dict(service.contract)) - {
            "characteristic",
            "min",
            "max",
            "source_attribute",
        }
        if unknown:
            raise ValueError(
                f"{self.service_id} has unsupported contract keys: "
                + ", ".join(sorted(unknown))
            )
        _limits(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError(f"{self.service_id} currently requires exactly one HA entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        attribute = service.contract_value("source_attribute", "remaining_time")
        raw = state.attributes.get(attribute)
        if raw is None:
            # HA entity states are always strings, so parse the state itself.
            raw = state.state
        parsed = _as_number(raw)
        if parsed is None:
            return {}
        minimum, maximum = _limits(service)
        value = round(parsed)
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return {_characteristic(service): int(value)}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        # Read-only on both source Profiles.
        self.validate_service(service)
        self.validate_entities(entity_ids)


def _as_number(value: object) -> float | None:
    """Return a float for int/float/numeric-string values, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _characteristic(service: ServiceRef) -> str:
    override = service.contract_value("characteristic")
    if isinstance(override, str) and override:
        return override
    default = _DEFAULT_CHARACTERISTICS.get(service.service_id)
    if default:
        return default
    if isinstance(service.service_type, str) and service.service_type:
        return service.service_type
    return service.service_id


def _limits(service: ServiceRef) -> tuple[int | None, int | None]:
    minimum = _int(service.contract_value("min"), 0)
    maximum = _int(service.contract_value("max"), 1440)
    if minimum is not None and maximum is not None and minimum > maximum:
        minimum, maximum = maximum, minimum
    return minimum, maximum


def _int(value: object, default: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def register_left_time_adapter(
    service_id: str, service_type: str | None = None
) -> LeftTimeServiceAdapter:
    """Build one adapter instance for a remaining-time sid."""
    del service_type
    return LeftTimeServiceAdapter(service_id)
