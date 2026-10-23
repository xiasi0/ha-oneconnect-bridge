"""Huawei ``temperature`` measurement service adapter.

默认契约来源：
  prodId = 294P / 100Z (空调伴侣 / 空调)
  Profile = https://smarthome-drcn.dbankcdn.com/device/guide/294P/294P.json
  常见性：常见服务（climate 品类的 temperature）
  能力范围：current 为 int（0.1℃ 由 scale 决定），targetTemperature 为 int，
            常见 min=160 / max=320 / step=10（即 16.0℃ .. 32.0℃，步进 1℃）

换算：
  scale 是「HA 值 → 华为 wire 值」的乘数，两侧同用。
  climate 常用 scale: 10（华为按 0.1℃ 传整数，HA 是摄氏度）；
  sensor 常用 scale: 1（A2LW 的 current 按整数℃ 传，profile 里的
  min=-100 max=400 是 profile 笔误，不是 0.1 刻度）。

contract：
  field         当前值字段名（默认 current，置 "" 可只上报目标）
  target_field  目标值字段名（默认 target，置 "" 可只上报当前）
  scale         换算乘数（默认 1）
  min / max     wire 值域（可省略；省略即不裁剪，保持上游行为）
  step          wire 步进格点（可省略；省略即不对齐）

验证（2026-10-21 / HA 2026.10.0 / 华为 OneConnect 网关）：
  climate 分支：100Z 空调实测（targetTemperature，0.1℃ 精度，格点 10）
  sensor  分支：21DJ / 113D 温湿度传感器实测（current 按 scale 换算）
"""

from __future__ import annotations

import math

from ..models import ServiceRef


class TemperatureServiceAdapter:
    service_id = "temperature"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "temperature":
            raise ValueError("temperature requires st: temperature")
        unknown = set(dict(service.contract)) - {
            "field",
            "target_field",
            "scale",
            "min",
            "max",
            "step",
        }
        if unknown:
            raise ValueError(
                "temperature has unsupported contract keys: "
                f"{', '.join(sorted(unknown))}"
            )
        _measurement_contract(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("temperature requires exactly one sensor or climate entity")
        domain, separator, object_id = entity_ids[0].partition(".")
        if not separator or not object_id or domain not in {"sensor", "climate"}:
            raise ValueError("temperature requires exactly one sensor or climate entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        limits = _measurement_contract(service)
        field, target_field, scale = limits.field, limits.target_field, limits.scale
        domain = entity_ids[0].partition(".")[0]
        if domain == "sensor":
            if field is None:
                return {}
            value = _number(state.state)
            if value is None:
                return {}
            return _wire_field(field, value * scale, limits, clamp=False)

        attributes = state.attributes
        result: dict[str, object] = {}
        current = _number(attributes.get("current_temperature"))
        if field is not None and current is not None:
            result.update(_wire_field(field, current * scale, limits, clamp=True))
        # 关机状态下室温仍然有效，但目标温度没有意义，只上报当前值。
        if state.state == "off":
            return result
        target = _number(attributes.get("temperature"))
        if target is None:
            target = _number(attributes.get("target_temperature"))
        if target_field is not None and target is not None:
            result.update(_wire_field(target_field, target * scale, limits, clamp=True))
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
            # 传感器是只读服务，华为侧不会下发温度命令。
            return
        limits = _measurement_contract(service)
        target_field = limits.target_field
        if target_field is None:
            return
        wire_target = _wire_number_or_float(payload.get(target_field))
        if wire_target is None:
            return
        # 越界或不在格点上的目标温度一律丢弃：宁可不上报，也不猜用户意图。
        if not _accepts(wire_target, limits):
            return
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": entity_ids[0], "temperature": wire_target / limits.scale},
            blocking=True,
        )


class _Limits:
    __slots__ = ("field", "target_field", "scale", "minimum", "maximum", "step")

    def __init__(
        self,
        field: str | None,
        target_field: str | None,
        scale: float,
        minimum: float | None,
        maximum: float | None,
        step: float | None,
    ) -> None:
        self.field = field
        self.target_field = target_field
        self.scale = scale
        self.minimum = minimum
        self.maximum = maximum
        self.step = step


def _measurement_contract(service: ServiceRef) -> _Limits:
    """Map HA Celsius to the Huawei field using ``scale`` in both directions."""

    field = service.contract_value("field", "current")
    target_field = service.contract_value("target_field", "target")
    scale = service.contract_value("scale", 1)
    minimum = service.contract_value("min", None)
    maximum = service.contract_value("max", None)
    step = service.contract_value("step", None)
    if field == "":
        field = None
    elif not isinstance(field, str):
        raise ValueError("temperature contract field must be text")
    if target_field == "":
        target_field = None
    elif not isinstance(target_field, str):
        raise ValueError("temperature contract target_field must be text")
    if field is None and target_field is None:
        raise ValueError("temperature contract must enable a current or target field")
    scale = _positive_number(scale, "scale")
    if minimum is not None:
        minimum = _finite_number(minimum, "min")
    if maximum is not None:
        maximum = _finite_number(maximum, "max")
    if step is not None:
        step = _positive_number(step, "step")
    if minimum is not None and maximum is not None and minimum >= maximum:
        raise ValueError("temperature contract min must be less than max")
    return _Limits(field, target_field, scale, minimum, maximum, step)


def _positive_number(value: object, key: str) -> float:
    number = _finite_number(value, key)
    if number <= 0:
        raise ValueError(f"temperature contract {key} must be positive and finite")
    return number


def _finite_number(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"temperature contract {key} must be numeric")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"temperature contract {key} must be finite") from error
    if not math.isfinite(number):
        raise ValueError(f"temperature contract {key} must be finite")
    return number


def _quantize(wire: float, limits: _Limits) -> float:
    """Snap onto the Profile's step grid, anchored at ``min`` when known."""

    if limits.step is None:
        return wire
    anchor = limits.minimum if limits.minimum is not None else 0.0
    return anchor + round((wire - anchor) / limits.step) * limits.step


def _accepts(wire: float, limits: _Limits) -> bool:
    if limits.minimum is not None and wire < limits.minimum:
        return False
    if limits.maximum is not None and wire > limits.maximum:
        return False
    if limits.step is not None:
        anchor = limits.minimum if limits.minimum is not None else 0.0
        remainder = abs((wire - anchor) % limits.step)
        if remainder > 1e-9 and abs(remainder - limits.step) > 1e-9:
            return False
    return True


def _wire_field(
    field: str, wire: float, limits: _Limits, *, clamp: bool
) -> dict[str, object]:
    """Quantize a reading; drop it unless ``clamp`` is allowed to pull it in range."""

    value = _quantize(wire, limits)
    if limits.minimum is not None:
        if value < limits.minimum:
            if not clamp:
                return {}
            value = limits.minimum
    if limits.maximum is not None:
        if value > limits.maximum:
            if not clamp:
                return {}
            value = limits.maximum
    return {field: _wire_number(value)}


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


def _wire_number_or_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _wire_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


ADAPTER = TemperatureServiceAdapter()
