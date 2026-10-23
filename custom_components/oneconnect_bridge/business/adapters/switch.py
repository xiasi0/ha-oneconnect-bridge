"""Huawei `switch.on` adapter with configurable bool/int wire encoding.

默认契约来源：
  prodId = 100Z (奥克斯挂机 / 智能空调 / model TYC2)
    Profile = https://smarthome-drcn.dbankcdn.com/device/guide/100Z/100Z.json
  常见性：**复用最多的服务** —— 灯、开关、风扇、空调的电源都用 `switch` 表达
  能力范围：
    on: bool | int（由 contract 的 power_type 决定线上编码）
    HA 域：light / switch / fan / climate
      light / switch / fan → 对应域的 turn_on / turn_off
      climate              → climate.turn_on / turn_off
                             只有 `off` 算关，auto/cool/heat 等其余状态都算开
                             这是空调电源的语义，不是开关的二进制语义

验证（2026-10-21 / HA 2026.10.0 / 华为 OneConnect 网关）：
  真实投影已验证：A2VL 灯泡、100E/A2VL 开关、100Z 与 294P 空调均实测通过。
  bool 与 int 两种线上编码均已覆盖（部分 profile 用 true/false，部分用 1/0）。
"""

from __future__ import annotations

from ..models import ServiceRef

# climate 实体的“关机”状态集合；其余状态（cool/heat/auto/dry/fan_only）都算开机
_CLIMATE_OFF_STATES = {"off", "unknown", "unavailable"}


class SwitchServiceAdapter:
    service_id = "switch"
    # Profile 里 switch 的编号变体走同一个适配器，不必为每个编号再写一份
    service_ids = ("switch", "switch1", "switch2", "switch3")
    _domains = {"light", "switch", "fan", "climate"}

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type not in {"switch", "binarySwitch"}:
            raise ValueError("switch requires st: switch or binarySwitch")
        unknown = set(dict(service.contract)) - {"on_type"}
        if unknown:
            raise ValueError(
                f"switch has unsupported contract keys: {', '.join(sorted(unknown))}"
            )
        if _on_type(service) not in {"int", "bool"}:
            raise ValueError("switch contract on_type must be int or bool")

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("switch currently requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in self._domains:
            raise ValueError("switch requires a light, switch, fan, or climate entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None:
            return {}
        domain = entity_ids[0].partition(".")[0]
        if domain == "climate":
            # climate 的 state 不是 on/off，而是 off/auto/cool/heat/dry/fan_only
            is_on = state.state not in _CLIMATE_OFF_STATES
        else:
            if state.state not in {"on", "off"}:
                return {}
            is_on = state.state == "on"
        return {"on": is_on if _on_type(service) == "bool" else int(is_on)}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        enabled = _parse_on_value(service, payload.get("on"))
        if enabled is None:
            return
        domain = entity_ids[0].partition(".")[0]
        # climate 用 climate.turn_on / turn_off；light/switch/fan 用各自的
        await hass.services.async_call(
            domain,
            "turn_on" if enabled else "turn_off",
            {"entity_id": entity_ids[0]},
            blocking=True,
        )


ADAPTER = SwitchServiceAdapter()


def _parse_on_value(service: ServiceRef, value: object) -> bool | None:
    if _on_type(service) == "int":
        if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1}:
            return None
        return bool(value)
    return value if isinstance(value, bool) else None


def _on_type(service: ServiceRef) -> str:
    return service.contract_value("on_type", "int")
