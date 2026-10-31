"""Huawei ``filterElement`` service: remaining life of one consumable filter.

Contract source
    prodId   : ``A2D8``
    Profile  : https://smarthome-drcn.dbankcdn.com/device/guide/A2D8/A2D8.json
    Service  : ``filterElement1`` / ``filterElement2``, ``st: filterElement``,
               characteristics ``leftPer`` (R, int, 0..100) and, on most
               products, ``leftTime`` (R, int, 0..1000)

Commonality
    Across the water purifier, air purifier, humidifier, dehumidifier and
    fresh-air product families, the numbered form dominates:
    ``filterElement1`` 103 products, ``filterElement2`` 67,
    ``filterElement3`` 18, ``filterElement4`` 5, ``filterElement5/6/7/8``
    a few each, and the unnumbered ``filterElement`` on 12. A purifier with
    two filters therefore exposes two services, and each one must be
    registered under its own ``sid`` -- one Huawei ``sid`` maps to one
    adapter, and the ``sid`` keeps the Profile spelling. The numbered
    variants behave identically, so ``registry.py`` points every
    ``filterElementN`` at this same adapter instance.

    ``serviceType`` is not uniform either: some Products write the family
    name (``filterElement``), others repeat the ``sid``
    (``filterElement1``, ``filterElement2``, ``commonFilterElement1``).
    ``validate_service`` accepts every spelling that occurs in the
    catalogue instead of forcing a rewrite of the Profile.

Home Assistant mapping
    sensor : the entity state becomes ``leftPer`` by default, or
             ``leftTime`` when ``contract.field`` selects it. A
             ``sensor`` entity is required because the value is a
             measurement, not a state.

    Read-only. ``leftPer`` / ``leftTime`` are ``R`` in every Product that
    declares them; the writable neighbours ``reset`` / ``name`` / ``id``
    are not exposed here. ``apply_command`` stays empty on purpose: a
    Home Assistant sensor has no "reset the filter" action, and silently
    firing a reset from a failed state write would be destructive.

contract
    field : ``leftPer`` (default) or ``leftTime``. Use ``leftTime`` when
            the Product publishes days/hours and the dashboard should show
            that instead of a percentage.
    scale : numeric multiplier applied on read, default ``1``.

Limitations
    One entity per service: a Product with two filters needs two
    ``filterElementN`` services in the mapping, each bound to its own
    sensor. Multi-filter Products cannot be collapsed into one entity,
    because the ``sid`` is unique per child.
"""

from __future__ import annotations

import math

from ..models import ServiceRef

# Every serviceType spelling that the catalogue uses for this family.
_ACCEPTED_TYPES = frozenset(
    {
        "filterElement",
        "filterElement1",
        "filterElement2",
        "filterElement3",
        "filterElement4",
        "filterElement5",
        "filterElement6",
        "filterElement7",
        "filterElement8",
        "commonFilterElement1",
        "commonFilterElement2",
    }
)

_FIELDS = frozenset({"leftPer", "leftTime"})


class FilterElementServiceAdapter:
    service_id = "filterElement"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type not in _ACCEPTED_TYPES:
            raise ValueError(
                "filterElement requires st: filterElement or a numbered "
                "filterElementN / commonFilterElementN variant"
            )
        unknown = set(dict(service.contract)) - {"field", "scale"}
        if unknown:
            raise ValueError(
                "filterElement has unsupported contract keys: "
                f"{', '.join(sorted(unknown))}"
            )
        _contract(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1 or not entity_ids[0].startswith("sensor."):
            raise ValueError("filterElement requires exactly one sensor entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        field, scale = _contract(service)
        value = _number(state.state)
        if value is None:
            return {}
        return {field: _wire_number(value * scale)}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        # Report-only service: no Home Assistant action maps to a filter
        # reset, so commands are intentionally ignored.
        return


ADAPTER = FilterElementServiceAdapter()


def _contract(service: ServiceRef) -> tuple[str, float]:
    field = service.contract_value("field", "leftPer")
    if field not in _FIELDS:
        raise ValueError(
            "filterElement contract field must be one of: "
            + ", ".join(sorted(_FIELDS))
        )
    scale = service.contract_value("scale", 1)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        raise ValueError("filterElement contract scale must be numeric")
    try:
        scale = float(scale)
    except (OverflowError, ValueError) as error:
        raise ValueError("filterElement contract scale must be finite") from error
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("filterElement contract scale must be positive and finite")
    return field, scale


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _wire_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value
