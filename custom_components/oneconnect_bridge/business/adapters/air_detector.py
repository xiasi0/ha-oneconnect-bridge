"""Huawei ``airDetector`` read-only measurement service adapter.

默认契约来源：
  prodId = 113D (温湿度传感器 / TH Sensor)
  Profile = https://smarthome-drcn.dbankcdn.com/device/guide/113D/113D.json
  常见性：常见服务
  能力范围：temperature / humidity / luminance 三个只读特性；
            Profile 按 0.01 单位传整数（temperature -2000..10000、
            humidity 0..10000、luminance 0..200000）

为什么需要这个适配器：
  113D 把温度和湿度放在**同一个服务**里，特性名就叫 temperature / humidity
  （而不是 current）。用通用的 temperature / humidity 适配器会绑不上。

contract：
  temperature_scale / humidity_scale / luminance_scale  换算乘数（默认 100/100/1）
  <feature>_min / <feature>_max                         wire 值域（可省略）
  scale                                                 指标未单列时的兜底乘数

实体绑定：1 到 3 个 sensor，按 device_class（缺失时按 unit_of_measurement）配对，
同一个 indicator 只取一次。

验证（2026-10-21 / HA 2026.10.0 / 华为 OneConnect 网关）：
  真实投影已验证：113D 实测（temperature 2665 → 26.65℃，humidity 5190 → 51.9%）。
  luminance 分支未在实机验证，按 Profile 声明实现，标为候选。
"""

from __future__ import annotations

import math

from ..models import ServiceRef

# feature -> (scale 的 contract 键, 默认 scale, 默认 wire 下限, 默认 wire 上限)
_FEATURES: dict[str, tuple[str, float, float, float]] = {
    "temperature": ("temperature_scale", 100.0, -2000.0, 10000.0),
    "humidity": ("humidity_scale", 100.0, 0.0, 10000.0),
    "luminance": ("luminance_scale", 1.0, 0.0, 200000.0),
}


class AirDetectorServiceAdapter:
    service_id = "airDetector"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "airDetector":
            raise ValueError("airDetector requires st: airDetector")
        allowed = {"scale"}
        for key in _FEATURES:
            allowed.add(f"{key}_scale")
            allowed.add(f"{key}_min")
            allowed.add(f"{key}_max")
        unknown = set(dict(service.contract)) - allowed
        if unknown:
            raise ValueError(
                "airDetector has unsupported contract keys: "
                f"{', '.join(sorted(unknown))}"
            )
        _limits(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if not 1 <= len(entity_ids) <= len(_FEATURES):
            raise ValueError(
                "airDetector requires one to three distinct sensor entities"
            )
        if len(set(entity_ids)) != len(entity_ids):
            raise ValueError("airDetector requires distinct sensor entities")
        if any(not entity_id.startswith("sensor.") for entity_id in entity_ids):
            raise ValueError("airDetector requires sensor entities")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        limits = _limits(service)
        result: dict[str, object] = {}
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            if state is None or state.state in {"unknown", "unavailable"}:
                continue
            attributes = state.attributes
            feature = _measurement_kind(
                attributes.get("device_class"), attributes.get("unit_of_measurement")
            )
            if feature is None:
                continue
            value = _number(state.state)
            if value is None:
                continue
            if feature == "temperature":
                parent = _celsius(value, attributes.get("unit_of_measurement"))
                if parent is None:
                    continue
                value = parent
            scale, minimum, maximum = limits[feature]
            wire = round(value * scale)
            if not minimum <= wire <= maximum:
                continue
            result[feature] = wire
        return result

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        """The 113D Profile declares every airDetector characteristic read-only."""

        self.validate_service(service)
        self.validate_entities(entity_ids)


def _measurement_kind(device_class: object, unit: object) -> str | None:
    """``device_class`` wins; fall back to the unit when the entity omits it."""

    if device_class == "temperature":
        return "temperature"
    if device_class == "humidity":
        return "humidity"
    if device_class == "illuminance":
        return "luminance"
    normalized = str(unit).strip().casefold()
    if normalized in {"°c", "c", "℃", "°f", "f", "℉", "fahrenheit", "k", "kelvin"}:
        return "temperature"
    if normalized in {"%", "%rh"}:
        return "humidity"
    if normalized in {"lx", "lux"}:
        return "luminance"
    return None


def _celsius(value: float, unit: object) -> float | None:
    normalized = str(unit).strip().casefold()
    if normalized in {"°f", "f", "℉", "fahrenheit"}:
        value = (value - 32) * 5 / 9
    elif normalized in {"k", "kelvin"}:
        value -= 273.15
    return value if math.isfinite(value) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _limits(
    service: ServiceRef,
) -> dict[str, tuple[float, float, float]]:
    """Resolve per-feature ``(scale, min, max)``, honouring the global fallback."""

    fallback = service.contract_value("scale", None)
    if fallback is not None:
        fallback = _positive_number(fallback, "scale")
    resolved: dict[str, tuple[float, float, float]] = {}
    for feature, (scale_key, scale_default, min_default, max_default) in _FEATURES.items():
        raw_scale = service.contract_value(
            scale_key, fallback if fallback is not None else scale_default
        )
        scale = _positive_number(raw_scale, scale_key)
        minimum = _number(service.contract_value(f"{feature}_min", min_default))
        maximum = _number(service.contract_value(f"{feature}_max", max_default))
        if minimum is None or maximum is None:
            raise ValueError(f"airDetector contract {feature}_min/max must be numeric")
        if minimum >= maximum:
            raise ValueError(
                f"airDetector contract {feature}_min must be less than {feature}_max"
            )
        resolved[feature] = (scale, minimum, maximum)
    return resolved


def _positive_number(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"airDetector contract {key} must be numeric")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"airDetector contract {key} must be finite") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"airDetector contract {key} must be positive and finite")
    return number


ADAPTER = AirDetectorServiceAdapter()
