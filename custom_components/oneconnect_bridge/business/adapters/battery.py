"""Huawei `battery` service adapter（只读电量 / 低电告警）。

默认契约来源：
  prodId = 21DJ  (温湿度传感器 / LS063WH)  → characteristic: level (int, 0..100)
  prodId = 113D  (豪恩温湿度传感器 / H-32ZB) → characteristic: lowBattery (bool)

为什么这个适配器要写得比其他几个更宽：
  `battery` 是华为 profile 里特征组合**最多**的 sid，实测 7 种：
    level:int                                          ← 21DJ / 283O
    lowBattery:bool                                     ← 113D
    alarm:bool, level:int                               ← 20YD / 2BE8 / 2GB1
    alarm:bool                                          ← 201E
    lowBattery:int | lowBattery:bool                    ← 若干门磁/人体传感器
    lowBattery:int, batteryThreshold:int, capacity:int, charging:int
    alarm:bool, threshold:int, level:int, capacity:int  ← 2OPO
  registry 只按 sid 索引，一个 sid 只能有一个适配器，所以统一在这里处理。

输出形态由 contract 的 `characteristic` 决定：
  characteristic: level       → {"level": 0..100}        （21DJ 这类）
  characteristic: lowBattery  → {"lowBattery": true/false}（113D 这类）
  characteristic: alarm       → {"alarm": true/false}     （201E/20YD 这类）
  include_alarm: true         → 在 level 基础上额外输出 alarm（兼容旧写法）

验证（2026-10-21 / HA 2026.10.0 / 华为 OneConnect 网关）：
  真实投影已验证：子设备注册、状态上报、命令链路全部通过。
  `level` 形态（21DJ）与 `lowBattery` 形态（113D）均已实机验证。
  Profile: https://smarthome-drcn.dbankcdn.com/device/guide/21DJ/21DJ.json
           https://smarthome-drcn.dbankcdn.com/device/guide/113D/113D.json
"""

from __future__ import annotations

from ..models import ServiceRef

_VALID_CHARACTERISTICS = {"level", "lowBattery", "alarm"}


class BatteryServiceAdapter:
    service_id = "battery"
    _domains = {"sensor"}

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "battery":
            raise ValueError("battery requires st: battery")
        unknown = set(dict(service.contract)) - {
            "min", "max", "low_threshold", "include_alarm", "characteristic",
        }
        if unknown:
            raise ValueError(
                f"battery has unsupported contract keys: {', '.join(sorted(unknown))}"
            )
        _limits(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("battery requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in self._domains:
            raise ValueError("battery requires a sensor entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None:
            return {}

        # 注意：不能在这里因 state 是 unknown/unavailable 就提前返回，
        # 部分集成把电量放在 battery_level 属性里，state 反而是 unknown。
        level = _read_level(state)
        if level is None:
            return {}

        minimum, maximum, low_threshold, include_alarm, characteristic = _limits(service)
        if not minimum <= level <= maximum:
            return {}

        low = level <= low_threshold
        result: dict[str, object] = {}

        if characteristic == "level":
            result["level"] = level
            if include_alarm:
                # 华为的 alarm 语义是"低电量告警"
                result["alarm"] = 1 if low else 0
        else:
            # lowBattery / alarm：只输出布尔告警
            result[characteristic] = low

        return result

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        # 只读服务：华为侧不会下发电池命令
        self.validate_service(service)
        self.validate_entities(entity_ids)
        return None


ADAPTER = BatteryServiceAdapter()


def _read_level(state) -> int | None:
    """从 HA 实体取出 0..100 的整数电量。"""
    # 1) 优先 battery_level 属性（部分集成把电量放属性里，state 可能是 unknown）
    attr = state.attributes.get("battery_level")
    if _is_number(attr):
        value = int(round(float(attr)))
        if 0 <= value <= 100:
            return value

    # 2) 退回实体 state
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not _is_number(value):
        return None
    value = int(round(value))
    if not 0 <= value <= 100:
        return None
    return value


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _limits(service: ServiceRef) -> tuple[int, int, int, bool, str]:
    minimum = service.contract_value("min", 0)
    maximum = service.contract_value("max", 100)
    low_threshold = service.contract_value("low_threshold", 20)
    include_alarm = service.contract_value("include_alarm", False)
    characteristic = service.contract_value("characteristic", "level")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (minimum, maximum, low_threshold)
    ):
        raise ValueError("battery contract min, max, low_threshold must be integers")
    if not isinstance(include_alarm, bool):
        raise ValueError("battery contract include_alarm must be boolean")
    if not isinstance(characteristic, str) or characteristic not in _VALID_CHARACTERISTICS:
        raise ValueError(
            f"battery contract characteristic must be one of "
            f"{', '.join(sorted(_VALID_CHARACTERISTICS))}"
        )
    if not 0 <= minimum < maximum <= 100:
        raise ValueError("battery contract must be within 0..100")
    if not minimum <= low_threshold <= maximum:
        raise ValueError("battery contract low_threshold must be within min..max")
    return minimum, maximum, low_threshold, include_alarm, characteristic
