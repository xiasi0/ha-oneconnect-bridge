"""Huawei `airConditioner.mode` service adapter.

默认契约来源：
  prodId = 100Z  (奥克斯挂机 / 智能空调 / model TYC2)
  Profile = https://smarthome-drcn.dbankcdn.com/device/guide/100Z/100Z.json
  常见性：常见服务（智能空调品类的 mode 特征）
  能力范围：已验证的枚举 1..5

华为 `airConditioner.mode` 枚举 → HA `climate.hvac_mode`：
  1 = 自动   → auto
  2 = 制冷   → cool
  3 = 制热   → heat
  4 = 通风   → fan_only
  5 = 除湿   → dry

关闭态（off）不在此服务内表达 —— 由 `switch` 服务负责电源通断。

验证（2026-10-21 / HA 2026.10.0 / 华为 OneConnect 网关）：
  真实投影已验证：100Z 空调实测，模式双向可通
  （自动 / 制冷 / 制热 / 送风 / 除湿 五档均验证）。
  注：294P 空调伴侣的模式在 `airKey` 服务内部，不走本适配器。
"""

from __future__ import annotations

from ..models import ServiceRef

# 华为枚举 → HA hvac_mode
_HW_TO_HA: dict[int, str] = {
    1: "auto",
    2: "cool",
    3: "heat",
    4: "fan_only",
    5: "dry",
}
_HA_TO_HW: dict[str, int] = {v: k for k, v in _HW_TO_HA.items()}


class AirConditionerServiceAdapter:
    service_id = "airConditioner"
    _domains = {"climate"}

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "airConditioner":
            raise ValueError("airConditioner requires st: airConditioner")
        if service.contract:
            raise ValueError("airConditioner does not support contract overrides")

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("airConditioner requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in self._domains:
            raise ValueError("airConditioner requires a climate entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable", "off"}:
            return {}
        mode = _HA_TO_HW.get(state.state)
        if mode is None:
            return {}
        return {"mode": mode}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        value = payload.get("mode")
        if isinstance(value, bool) or not isinstance(value, int):
            return
        hvac_mode = _HW_TO_HA.get(value)
        if hvac_mode is None:
            return
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_ids[0], "hvac_mode": hvac_mode},
            blocking=True,
        )


ADAPTER = AirConditionerServiceAdapter()
