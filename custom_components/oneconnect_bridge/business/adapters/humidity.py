"""Huawei ``humidity`` measurement service adapter.

默认契约来源：
  prodId = A2LW  (温湿度传感器 / TH Sensor)
  Profile = https://smarthome-drcn.dbankcdn.com/device/guide/A2LW/A2LW.json
  常见性：常见服务（温湿度传感器品类的 humidity）
  能力范围：current 为 int，取值范围 0..1000，刻度 0.1
            → 即 0.0% .. 100.0%

换算：
  scale 默认 1（整数 %），min/max 用华为 wire 上的数值（默认 0/100）。
  若接 21DJ 这类 unit 明确写 `0.1%` 的产品，在 mapping 里写 scale: 10, max: 1000。
  sensor 域只读；climate 域按 field / target_field 读写目标湿度。

验证（2026-10-21 / HA 2026.10.0 / 华为 OneConnect 网关）：
  真实投影已验证：21DJ 实测（620 → 62.0%）。
  注：当前部署改用 113D（温湿度同属 `airDetector` 一个服务），本适配器保留
      以支持 21DJ 这类「湿度是独立服务」的产品。
"""

from __future__ import annotations

import math

from ..models import ServiceRef


class HumidityServiceAdapter:
    service_id = "humidity"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "humidity":
            raise ValueError("humidity requires st: humidity")
        unknown = set(dict(service.contract)) - {
            "field",
            "target_field",
            "scale",
            "min",
            "max",
        }
        if unknown:
            raise ValueError(
                "humidity has unsupported contract keys: "
                f"{', '.join(sorted(unknown))}"
            )
        _measurement_contract(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("humidity requires exactly one sensor or climate entity")
        domain, separator, object_id = entity_ids[0].partition(".")
        if not separator or not object_id or domain not in {"sensor", "climate"}:
            raise ValueError("humidity requires exactly one sensor or climate entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        field, target_field, scale, minimum, maximum = _measurement_contract(service)
        domain = entity_ids[0].partition(".")[0]
        if domain == "sensor":
            if field is None:
                return {}
            value = _humidity_value(state.state)
            if value is None:
                return {}
            return _wire_field(field, value * scale, minimum, maximum)

        attributes = state.attributes
        result: dict[str, object] = {}
        current = _humidity_value(attributes.get("current_humidity"))
        if field is not None and current is not None:
            result.update(_wire_field(field, current * scale, minimum, maximum))
        target = _humidity_value(attributes.get("humidity"))
        if target is None:
            target = _humidity_value(attributes.get("target_humidity"))
        if target_field is not None and target is not None:
            result.update(_wire_field(target_field, target * scale, minimum, maximum))
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
        if entity_ids[0].partition(".")[0] != "climate":
            return
        _field, target_field, scale, minimum, maximum = _measurement_contract(service)
        if target_field is None:
            return
        wire_target = _command_number(payload.get(target_field))
        if wire_target is None or not minimum <= wire_target <= maximum:
            return
        target = wire_target / scale
        if not 0 <= target <= 100:
            return
        await hass.services.async_call(
            "climate",
            "set_humidity",
            {"entity_id": entity_ids[0], "humidity": target},
            blocking=True,
        )


def _measurement_contract(
    service: ServiceRef,
) -> tuple[str | None, str | None, float, float, float]:
    """Map HA percent to the Huawei field using ``scale`` in both directions."""

    field = service.contract_value("field", "current")
    target_field = service.contract_value("target_field", "target")
    scale = service.contract_value("scale", 1)
    minimum = service.contract_value("min", 0)
    maximum = service.contract_value("max", 100)
    if field == "":
        field = None
    elif not isinstance(field, str):
        raise ValueError("humidity contract field must be text")
    if target_field == "":
        target_field = None
    elif not isinstance(target_field, str):
        raise ValueError("humidity contract target_field must be text")
    if field is None and target_field is None:
        raise ValueError("humidity contract must enable a current or target field")
    scale = _positive_number(scale, "scale")
    minimum = _finite_number(minimum, "min")
    maximum = _finite_number(maximum, "max")
    if minimum >= maximum:
        raise ValueError("humidity contract min must be less than max")
    return field, target_field, scale, minimum, maximum


def _positive_number(value: object, key: str) -> float:
    number = _finite_number(value, key)
    if number <= 0:
        raise ValueError(f"humidity contract {key} must be positive and finite")
    return number


def _finite_number(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"humidity contract {key} must be numeric")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"humidity contract {key} must be finite") from error
    if not math.isfinite(number):
        raise ValueError(f"humidity contract {key} must be finite")
    return number


def _wire_field(
    field: str, wire: float, minimum: float, maximum: float
) -> dict[str, object]:
    """Drop readings the Profile cannot carry instead of sending garbage."""

    if not minimum <= wire <= maximum:
        return {}
    return {field: _wire_number(wire)}


def _humidity_value(value: object) -> float | None:
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
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def _command_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _wire_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


ADAPTER = HumidityServiceAdapter()
