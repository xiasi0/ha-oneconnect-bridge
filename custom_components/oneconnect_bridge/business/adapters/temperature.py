"""Huawei ``temperature`` measurement service adapter."""

from __future__ import annotations

import math

from ..models import ServiceRef


_DOMAINS = {"climate", "sensor", "number", "input_number", "select"}


class TemperatureServiceAdapter:
    service_id = "temperature"

    def validate_service(self, service: ServiceRef) -> None:
        # Haier four-door fridges carry target+current in a `heat` service.
        if service.service_type not in {"temperature", "heat"}:
            raise ValueError("temperature requires st: temperature or heat")
        unknown = set(dict(service.contract)) - {"factor", "min", "max", "step", "characteristic", "float"}
        if unknown:
            raise ValueError(
                "temperature has unsupported contract keys: "
                f"{', '.join(sorted(unknown))}"
            )
        _measurement_contract(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("temperature currently requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in _DOMAINS:
            raise ValueError(
                "temperature requires a climate, sensor, number, input_number or select entity"
            )

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        domain = entity_ids[0].partition(".")[0]
        factor = _factor(service)
        if domain != "climate":
            # A plain sensor/number carries the set-point in its own state.
            raw = _as_number(state.attributes.get("value", state.state))
            if raw is None:
                return {}
            minimum, maximum, step = _limits(service)
            degrees = raw
            if minimum is not None:
                degrees = max(minimum, degrees)
            if maximum is not None:
                degrees = min(maximum, degrees)
            if step:
                degrees = round(degrees / step) * step
            value = _quantize(
                degrees * factor, None, None, None,
                preserve_float=bool(service.contract_value("float", False)),
            )
            return {_characteristic(service): value} if value is not None else {}
        minimum, maximum, step = _limits(service)
        result: dict[str, object] = {}
        char = _characteristic(service)
        pairs = (("target", "temperature"), ("current", "current_temperature"))
        for field, attribute in pairs:
            raw = state.attributes.get(attribute)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            # min/max/step are Profile ranges in whole degrees: quantize in the
            # degree domain first, then scale onto the wire.
            degrees = raw
            if minimum is not None:
                degrees = max(minimum, degrees)
            if maximum is not None:
                degrees = min(maximum, degrees)
            if step:
                degrees = round(degrees / step) * step
            value = _quantize(
                degrees * factor, None, None, None,
                preserve_float=bool(service.contract_value("float", False)),
            )
            if value is not None:
                # A Profile may name the writable characteristic differently
                # (for example the A0VG fridge exposes `refrigerator.target`).
                result[char if field == "target" else field] = value
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
        factor = _factor(service)
        minimum, maximum, step = _limits(service)
        char = _characteristic(service)
        target = payload.get(char, payload.get("target"))
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            return
        degrees = target / factor
        if minimum is not None:
            degrees = max(minimum, degrees)
        if maximum is not None:
            degrees = min(maximum, degrees)
        if step:
            degrees = round(degrees / step) * step
        value = _quantize(
            degrees, None, None, None,
            preserve_float=bool(service.contract_value("float", False)),
        )
        if value is None:
            return
        domain = entity_ids[0].partition(".")[0]
        if domain == "climate":
            await hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_ids[0], "temperature": value},
                blocking=True,
            )
            return
        if domain in {"number", "input_number"}:
            await hass.services.async_call(
                domain,
                "set_value",
                {"entity_id": entity_ids[0], "value": value},
                blocking=True,
            )
            return
        if domain == "select":
            # Huawei render enum options like '-5℃（软冷冻）或42℉'; match the
            # option whose parsed number equals the requested degrees.
            state = hass.states.get(entity_ids[0])
            options = (state.attributes.get("options") if state else None) or []
            for option in options:
                if _as_number(option) == float(value):
                    await hass.services.async_call(
                        "select",
                        "select_option",
                        {"entity_id": entity_ids[0], "option": option},
                        blocking=True,
                    )
                    return


def _measurement_contract(
    service: ServiceRef,
) -> tuple[str | None, str | None, float]:
    """Map HA Celsius to the Huawei field using ``scale`` in both directions."""


def _characteristic(service: ServiceRef) -> str:
    """Profile characteristic name carrying the set-point (default `target`)."""
    value = service.contract_value("characteristic", "target")
    if isinstance(value, str) and value:
        return value
    return "target"


def _as_number(value: object) -> float | None:
    """Parse a numeric value out of an int, float or Huawei display string.

    Huawei cloud entities are not always bare numbers: the fridge set-point
    entities read like `+3℃或34℉` / `-5℃（软冷冻）或42℉`, so the first signed
    integer/decimal in the string is used.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    import re
    match = re.search(r"[+-]?\d+(?:\.\d+)?", text)
    if match is None:
        return None
    try:
        return float(match.group(0))
    except (TypeError, ValueError):
        return None


def _factor(service: ServiceRef) -> float:
    value = service.contract_value("factor", 10)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value == 0:
        return 10.0
    return float(value)


def _limits(service: ServiceRef) -> tuple[float | None, float | None, float]:
    minimum = _number(service.contract_value("min"))
    maximum = _number(service.contract_value("max"))
    step = _number(service.contract_value("step")) or 1.0
    return minimum, maximum, float(step)


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


def _command_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _quantize(
    value: float,
    minimum: float | None,
    maximum: float | None,
    step: float | None,
    *,
    preserve_float: bool = False,
) -> int | float | None:
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    if step:
        value = round(value / step) * step
    rounded = round(value)
    if abs(value - rounded) <= 1e-9:
        return int(rounded)
    # Profiles that declare `float` characteristics (for example the MC3 AC
    # protocol product with `targetFloat`) keep the fraction instead of
    # rejecting the reading.
    if preserve_float:
        return round(value, 2)
    return None

ADAPTER = TemperatureServiceAdapter()
