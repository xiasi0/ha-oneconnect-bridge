"""Huawei `wind.windSpeed` / `windDirection` service adapter.

默认契约来源：
  prodId = 100Z  (奥克斯挂机 / 智能空调 / model TYC2)
  Profile = https://smarthome-drcn.dbankcdn.com/device/guide/100Z/100Z.json
  常见性：常见服务；枚举集合因厂商而异
  能力范围：windSpeed 1..5，windDirection 1/3

windSpeed（风速）：
  1 = 低   → HA "低"
  2 = 中   → HA "中"
  3 = 高   → HA "高"
  4 = 自动 → HA "自动"
  5 = 静音 → HA "静音"（若 HA 侧没有这个选项则忽略）

windDirection（风向）：
  1 = 固定     → HA swing_mode "off"
  3 = 上下扫风 → HA swing_mode "on"

HA 的 fan_modes 是**文本**（您这台是「自动/低/中/高」），
所以这里做的是「华为枚举 ↔ HA 文本」的双向表，
而不是数值直接搬运。表可以由 contract 覆盖。

验证（2026-10-21 / HA 2026.10.0 / 华为 OneConnect 网关）：
  真实投影已验证：100Z 空调实测，风速与上下扫风双向可通。
  注：294P 空调伴侣的 wind 在 `airKey` 服务内部，不走本适配器。
"""

from __future__ import annotations

from ..models import ServiceRef

# 华为 windSpeed 枚举 → HA fan_mode 候选文本（按顺序取第一个在 fan_modes 里存在的）
_SPEED_HW_TO_HA: dict[int, tuple[str, ...]] = {
    1: ("低", "low"),
    2: ("中", "medium", "mid"),
    3: ("高", "high"),
    4: ("自动", "auto"),
    5: ("静音", "quiet", "silent"),
}

# 华为 windDirection 枚举 → HA swing_mode
_DIRECTION_HW_TO_HA: dict[int, str] = {
    1: "off",   # 固定
    3: "on",    # 上下扫风
}
_DIRECTION_HA_TO_HW: dict[str, int] = {v: k for k, v in _DIRECTION_HW_TO_HA.items()}


class WindServiceAdapter:
    service_id = "wind"
    _domains = {"climate"}

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "wind":
            raise ValueError("wind requires st: wind")
        if service.contract:
            raise ValueError("wind does not support contract overrides")

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("wind requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in self._domains:
            raise ValueError("wind requires a climate entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable", "off"}:
            return {}

        result: dict[str, object] = {}
        fan_modes = state.attributes.get("fan_modes") or []
        fan_mode = state.attributes.get("fan_mode")
        if fan_mode and isinstance(fan_modes, (list, tuple)):
            for hw_value, candidates in _SPEED_HW_TO_HA.items():
                if fan_mode in candidates and fan_mode in fan_modes:
                    result["windSpeed"] = hw_value
                    break

        swing_modes = state.attributes.get("swing_modes") or []
        swing_mode = state.attributes.get("swing_mode")
        if swing_mode and isinstance(swing_modes, (list, tuple)):
            hw_direction = _DIRECTION_HA_TO_HW.get(swing_mode)
            if hw_direction is not None and swing_mode in swing_modes:
                result["windDirection"] = hw_direction

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
        state = hass.states.get(entity_ids[0])
        if state is None:
            return
        fan_modes = state.attributes.get("fan_modes") or []
        swing_modes = state.attributes.get("swing_modes") or []

        speed = payload.get("windSpeed")
        if isinstance(speed, int) and not isinstance(speed, bool):
            for candidate in _SPEED_HW_TO_HA.get(speed, ()):
                if candidate in fan_modes:
                    await hass.services.async_call(
                        "climate",
                        "set_fan_mode",
                        {"entity_id": entity_ids[0], "fan_mode": candidate},
                        blocking=True,
                    )
                    break

        direction = payload.get("windDirection")
        if isinstance(direction, int) and not isinstance(direction, bool):
            swing_mode = _DIRECTION_HW_TO_HA.get(direction)
            if swing_mode is not None and swing_mode in swing_modes:
                await hass.services.async_call(
                    "climate",
                    "set_swing_mode",
                    {"entity_id": entity_ids[0], "swing_mode": swing_mode},
                    blocking=True,
                )


ADAPTER = WindServiceAdapter()
