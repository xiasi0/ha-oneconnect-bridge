"""Huawei `airKey` service adapter（空调伴侣的整套红外控制，RW）。

默认契约来源：
  prodId = 294P  (遥控大师空调伴侣 / YKK-KT16A/YKK-KT10A)
  Profile = https://smarthome-drcn.dbankcdn.com/device/guide/294P/294P.json
  品类：空调伴侣（categoryLv4 = 空调伴侣）
  能力范围：已验证的 mode/wind/airSwing/temp/power 五组特性

为什么不像 100Z 那样拆成 4 个服务：
  294P 的设计是「一个 airKey 服务装下所有空调控制特性」：
    mode / wind / airSwing / power / temp 全是 airKey 的特性
  而 100Z 是拆开的（switch / airConditioner / temperature / wind 四个服务）。
  这是华为两种不同的建模方式，必须按各自 profile 来。

特性映射（与 HA climate 实体）：

  airKey.mode      enum  → HA hvac_mode
      0 自动   → auto
      1 除湿   → dry
      2 送风   → fan_only
      3 制热   → heat
      4 制冷   → cool

  airKey.wind      enum  → HA fan_mode（文本）
      0 自动 → "自动"/"auto"
      1 低风 → "低"/"low"
      2 中风 → "中"/"medium"
      3 高风 → "高"/"high"

  airKey.airSwing  enum  → HA swing_mode
      0 全部关闭       → "off"
      1 上下+左右全开   → "on"
      2 上下开左右关    → "on"
      3 上下关左右开    → "on"

  airKey.power     bool  → HA state 是否为 off
  airKey.temp      int   → HA temperature
      范围 16..30，步进 1，**单位是整数 ℃**（不是 0.1℃）
      与 100Z 的 160..300 不同，注意区分

═══════════════════════════════════════════════════════════════════════════
★ 关键：为什么每个字段都必须上报（此处曾是本项目最大的坑）
═══════════════════════════════════════════════════════════════════════════

294P 的设备页不是 App 原生页，而是华为云 CDN 上厂商上传的 H5 包
（`uiType=H5`，`uriInfo.uri=294P/h5_001/index.html`，匿名可下）。
H5 的 Vue 模板用 `airKey` 各字段**直接索引列表**，Vuex 里又是整对象替换
（`SETAIRKEY(JSON.parse(...))`）。实测（同类项目 hwhomebridge 用真实 H5 包
+ 伪造 window.hilink 桥 + headless 逐字段 A/B）结论：

  case E  按"只报 power/matchKey*"上报 → DOM 11,154 → 「还没有空调」（故障复现）
  case F  case E + 只加 wind:0        → DOM 46,232 → ★ 面板渲染成功

⇒ **`wind` 为 undefined 会让模板索引抛异常，Vue 退回空态**。
   `mode` / `temp` / `airSwing` 同理，都是模板直接索引的字段。

因此本适配器的铁律：**无论 HA 实体处于什么状态（不存在 / unavailable /
关机），都要返回一份字段齐全的快照**，绝不允许因为读不到某个属性就少报字段。
宁可报兜底默认值，也不能让 H5 拿到 undefined。

第二个坑：`matchKeyStatus` 的取值。
  H5 匹配页的 watch 只处理三个值（源码 `6.23156828f378d42d4527.js`）：
      5 → 显示"匹配中"
      4 → 显示"完成" + **300ms 后 history.go(-t) 把页面顶回去**
      6 → 显示"超时"
  **7 没有任何分支** ⇒ 匹配页原地干等，直到云端自己判定超时并落 6
  ⇒ 用户看到「显示匹配超时」。
  **4 更不能用**：它的分支会 `history.go(-t)`，而设备页每次状态变化都会
  上报一份快照 —— 于是用户每按一个键，页面就被弹走一次，
  表现为「点不动 / 无法操作」。
  ⇒ 稳态必须报 **0（成功）**：0 没有副作用分支。
  旁证：同类项目存档的 9 个 H5 A/B 样本（case_A~I）里，
  `matchKeyStatus` 全部是 0，包括渲染成功的那几个。

不处理的特性（云→设备单向，设备禁止上报）：
  matchKey / matchKeyDownloadUrl1 / matchKeyDownloadUrl2
    —— 官方 294P.json 里是 method=W / permission=P / report=0，
       **整包上报会被云端丢弃**，必须剔除。
  controlSource —— 控制来源标记，不上报。

验证（2026-10-21 / HA 2026.10.0 / 华为 OneConnect 网关）：
  真实投影已验证：294P 空调伴侣实测，App 原生 H5 面板正常渲染，
  电源 / 模式 / 温度 / 风速 / 扫风全部可控。
  「八个字段无条件齐全」策略经四种输入验证（正常 / unavailable / 零属性 / 实体不存在）。
  `matchKeyStatus` 取 0 的结论来自官方 H5 的 watch 分支实测
  （7 无分支会等到超时，4 会触发 history.go 把页面顶回）。
"""

from __future__ import annotations

import logging

from ..models import ServiceRef

_LOGGER = logging.getLogger(__name__)

# HA 侧表示"空调关着"的状态
_CLIMATE_OFF_STATES = {"off", "unknown", "unavailable"}

# HA 侧表示"这个实体现在拿不到真实数据"
_UNAVAILABLE_STATES = {"unknown", "unavailable", "none", ""}

# 温度兜底默认值（属性缺失时用，会被 clamp 到 contract 范围内）
_DEFAULT_TEMP = 26

# ---- mode ----
_MODE_HW_TO_HA: dict[int, str] = {
    0: "auto",
    1: "dry",
    2: "fan_only",
    3: "heat",
    4: "cool",
}
_MODE_HA_TO_HW: dict[str, int] = {v: k for k, v in _MODE_HW_TO_HA.items()}

# ---- wind（华为枚举 → HA fan_mode 文本候选）----
_WIND_HW_TO_HA: dict[int, tuple[str, ...]] = {
    0: ("自动", "auto"),
    1: ("低", "low"),
    2: ("中", "medium", "mid"),
    3: ("高", "high"),
}

# ---- airSwing（HA swing_mode → 华为枚举）----
_SWING_HA_TO_HW: dict[str, int] = {"off": 0, "on": 1}
# 华为 2/3 是部分扫风，读回来时统一视为 "on"
_SWING_HW_TO_HA: dict[int, str] = {0: "off", 1: "on", 2: "on", 3: "on"}

# 兜底枚举值（H5 模板索引越界的起点）
_DEFAULT_MODE = 0      # 自动
_DEFAULT_WIND = 0      # 自动风
_DEFAULT_SWING = 0     # 扫风关


class AirKeyServiceAdapter:
    service_id = "airKey"
    _domains = {"climate"}

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "airKey":
            raise ValueError("airKey requires st: airKey")
        unknown = set(dict(service.contract)) - {
            "temp_min", "temp_max", "temp_step", "power_type",
            "match_key_status", "match_key_id", "match_key_encryption",
        }
        if unknown:
            raise ValueError(
                f"airKey has unsupported contract keys: {', '.join(sorted(unknown))}"
            )
        _temp_limits(service)
        _power_type(service)
        _match_key(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("airKey requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in self._domains:
            raise ValueError("airKey requires a climate entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        """返回字段齐全的 airKey 快照。

        ★ 不管 HA 实体是否存在、是否 unavailable、是否关机，
          都返回 power/mode/temp/wind/airSwing + matchKey* 的完整字段集。
          H5 模板对任何一个字段取 undefined 都会抛异常退回空态。
        """
        self.validate_service(service)
        self.validate_entities(entity_ids)
        entity_id = entity_ids[0]
        minimum, maximum, step = _temp_limits(service)
        power_type = _power_type(service)

        state = hass.states.get(entity_id)
        attributes = dict(state.attributes) if state is not None else {}
        raw_state = state.state if state is not None else "unavailable"
        available = raw_state not in _UNAVAILABLE_STATES
        is_on = available and raw_state not in _CLIMATE_OFF_STATES

        result: dict[str, object] = {}

        # ---- power：只认 off 为关；实体不可用一律按关机上报 ----
        on_flag = bool(is_on)
        result["power"] = on_flag if power_type == "bool" else int(on_flag)

        # ---- mode：始终上报（H5 用 mode 索引模式列表）----
        mode = _MODE_HA_TO_HW.get(raw_state) if is_on else None
        result["mode"] = mode if mode is not None else _DEFAULT_MODE

        # ---- temp：始终上报，缺失时用兜底值并 clamp 进 contract 范围 ----
        result["temp"] = _clamp_temp(
            attributes.get("temperature"), minimum, maximum, step
        )

        # ---- wind：始终上报（缺 wind 会直接让面板渲染失败）----
        result["wind"] = _resolve_wind(attributes)

        # ---- airSwing：始终上报 ----
        result["airSwing"] = _resolve_swing(attributes)

        # ---- 红外码库匹配状态（只读上行）----
        match = _match_key(service)
        if match is not None:
            status, key_id, encryption = match
            result["matchKeyStatus"] = status
            result["matchKeyId"] = key_id
            result["matchKeyEncryption"] = encryption

        _LOGGER.debug(
            "airKey snapshot entity=%s ha_state=%s available=%s payload=%s",
            entity_id,
            raw_state,
            available,
            result,
        )
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
        entity_id = entity_ids[0]
        state = hass.states.get(entity_id)
        if state is None:
            return

        # 1) power
        power = payload.get("power")
        power_on: bool | None = None
        if isinstance(power, bool):
            power_on = power
        elif isinstance(power, int) and power in (0, 1):
            power_on = power == 1
        if power_on is not None:
            await hass.services.async_call(
                "climate",
                "turn_on" if power_on else "turn_off",
                {"entity_id": entity_id},
                blocking=True,
            )

        # 2) mode
        mode = payload.get("mode")
        if isinstance(mode, int) and not isinstance(mode, bool):
            hvac_mode = _MODE_HW_TO_HA.get(mode)
            if hvac_mode is not None:
                await hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": entity_id, "hvac_mode": hvac_mode},
                    blocking=True,
                )

        # 3) temp
        temp = payload.get("temp")
        if isinstance(temp, int) and not isinstance(temp, bool):
            minimum, maximum, step = _temp_limits(service)
            if minimum <= temp <= maximum and (temp - minimum) % step == 0:
                await hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {"entity_id": entity_id, "temperature": float(temp)},
                    blocking=True,
                )

        # 4) wind
        wind = payload.get("wind")
        if isinstance(wind, int) and not isinstance(wind, bool):
            fan_modes = state.attributes.get("fan_modes") or []
            for candidate in _WIND_HW_TO_HA.get(wind, ()):
                if candidate in fan_modes:
                    await hass.services.async_call(
                        "climate",
                        "set_fan_mode",
                        {"entity_id": entity_id, "fan_mode": candidate},
                        blocking=True,
                    )
                    break

        # 5) airSwing
        swing = payload.get("airSwing")
        if isinstance(swing, int) and not isinstance(swing, bool):
            swing_mode = _SWING_HW_TO_HA.get(swing)
            if swing_mode is not None:
                swing_modes = state.attributes.get("swing_modes") or []
                if swing_mode in swing_modes:
                    await hass.services.async_call(
                        "climate",
                        "set_swing_mode",
                        {"entity_id": entity_id, "swing_mode": swing_mode},
                        blocking=True,
                    )


ADAPTER = AirKeyServiceAdapter()


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clamp_temp(
    value: object, minimum: int, maximum: int, step: int
) -> int:
    """把 HA 目标温度对齐并 clamp 进华为范围；非法/缺失时用兜底默认值。"""
    if _is_number(value):
        raw = int(round(float(value)))  # type: ignore[arg-type]
    else:
        raw = _DEFAULT_TEMP
    aligned = minimum + round((raw - minimum) / step) * step
    return max(minimum, min(maximum, aligned))


def _resolve_wind(attributes: dict[str, object]) -> int:
    """HA fan_mode → 华为 wind 枚举；匹配不到一律兜底 0（自动）。"""
    fan_modes = attributes.get("fan_modes") or []
    fan_mode = attributes.get("fan_mode")
    if fan_mode and isinstance(fan_modes, (list, tuple)):
        for hw_value, candidates in _WIND_HW_TO_HA.items():
            if fan_mode in candidates and fan_mode in fan_modes:
                return hw_value
    return _DEFAULT_WIND


def _resolve_swing(attributes: dict[str, object]) -> int:
    """HA swing_mode → 华为 airSwing 枚举；匹配不到一律兜底 0（关）。"""
    swing_modes = attributes.get("swing_modes") or []
    swing_mode = attributes.get("swing_mode")
    if swing_mode and isinstance(swing_modes, (list, tuple)):
        hw_swing = _SWING_HA_TO_HW.get(swing_mode)
        if hw_swing is not None and swing_mode in swing_modes:
            return hw_swing
    return _DEFAULT_SWING


def _temp_limits(service: ServiceRef) -> tuple[int, int, int]:
    minimum = service.contract_value("temp_min", 16)
    maximum = service.contract_value("temp_max", 30)
    step = service.contract_value("temp_step", 1)
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (minimum, maximum, step)
    ):
        raise ValueError("airKey temp_min/temp_max/temp_step must be integers")
    if not -50 <= minimum < maximum <= 100 or step <= 0:
        raise ValueError(
            "airKey temp contract must be within -50..100 with a positive step"
        )
    if (maximum - minimum) % step != 0:
        raise ValueError("airKey temp_step must divide its range")
    return minimum, maximum, step


def _power_type(service: ServiceRef) -> str:
    value = service.contract_value("power_type", "bool")
    if not isinstance(value, str) or value not in {"bool", "int"}:
        raise ValueError("airKey power_type must be bool or int")
    return value


def _match_key(service: ServiceRef) -> tuple[int, str, str] | None:
    """红外码库匹配状态。

    matchKeyStatus 枚举（来自 294P profile，官方原文）：
        0 成功            1 创建遥控器      2 删除遥控器
        3 码库下载失败     4 码库下载成功    5 码库下载中
        6 码库下载超时     7 红外码发送正常  8 红外码发送失败
        9 匹配失败

    默认 0（成功）。理由见模块文档：
      H5 匹配页 watch 只认 5/4/6，但 4 会 history.go(-t) 弹走页面，
      7 无人处理会干等超时；0 无副作用，且同类项目 A~I 九个可用样本
      的稳态都是 0。

    contract 里 `match_key_status: -1` 可关闭上报。
    """
    status = service.contract_value("match_key_status", 0)
    if isinstance(status, bool) or not isinstance(status, int):
        raise ValueError("airKey match_key_status must be an integer")
    if status == -1:
        return None
    if not 0 <= status <= 9:
        raise ValueError("airKey match_key_status must be -1 or within 0..9")

    # matchKeyId 是 H5 渲染总闸（`"" !== e.airKey.matchKeyId`），必须非空。
    key_id = service.contract_value("match_key_id", "virtual-0001")
    if not isinstance(key_id, str) or not key_id:
        raise ValueError("airKey match_key_id must be a non-empty string")

    encryption = service.contract_value("match_key_encryption", "01")
    if not isinstance(encryption, str) or not encryption:
        raise ValueError("airKey match_key_encryption must be a non-empty string")

    return status, key_id, encryption
