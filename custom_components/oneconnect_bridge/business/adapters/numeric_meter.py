"""通用「HA sensor → 华为数值特征」适配器。

═══════════════════════════════════════════════════════════════════════════
用途
═══════════════════════════════════════════════════════════════════════════

把 HA 侧的数值 sensor 按比例转成华为的整数特征。当前用于 294P 面板的两格：

    power.power            实时功率（瓦）
    electric.totalConsum   累计用电量

★ 字段名以 H5 为准：官方 294P.json 写的是 `power.current` / `electric.current`，
   但官方 H5 包实际读的是 `power.power` / `electric.totalConsum`。
   profile 这几处是错的，特征名一律由 contract 显式给出。

═══════════════════════════════════════════════════════════════════════════
与 temperature/humidity 的区别
═══════════════════════════════════════════════════════════════════════════

那两个是**固定语义**的服务（温度就是温度），只能用各自的 sid。
本适配器是**通用**的：特征名、缩放、范围全部来自 contract，
所以可以同时服务 `power`、`electric` 等不同 sid 的数值特征。

═══════════════════════════════════════════════════════════════════════════
contract
═══════════════════════════════════════════════════════════════════════════

    characteristic: 特征名（必填）
    scale:          华为值 = HA 值 × scale（默认 1）
    default:        实体不存在 / 不可用 / 非数值时的兜底值（默认 0）
    min / max:      可选，结果会被 clamp 进这个区间
    precision:      小数位数（默认 0，即取整）

例：

    - st: power
      sid: power
      contract:
        characteristic: power
        scale: 1
        default: 0
        min: 0
        max: 3600
默认契约来源：
  prodId：**不绑定单一产品** —— 行为由 contract 驱动，不提供默认契约。
  常见性：通用型。适用于任意「HA 数值实体 → 华为整数特征」的映射。
  当前使用场景：294P 空调伴侣的 power（实时功率，瓦）与 electric
  （云端电量累加原料）。

验证（2026-10-21 / HA 2026.10.0 / 华为 OneConnect 网关）：
  真实投影已验证：power 上报待机 2 W、面板显示一致；electric 用 delta 模式
  上报增量，面板数值与 HA 的 power_cost_today 对齐（差值在云端 0.01 量化精度内）。
  absolute / delta / constant / hold_when_unavailable 四种模式均有单元自检覆盖。
"""

from __future__ import annotations

import logging
import time

from ..models import ServiceRef

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE_STATES = {"unknown", "unavailable", "none", ""}


class NumericMeterAdapter:
    service_id = "numericMeter"
    # registry 会为下面每个 sid 登记同一个适配器实例
    service_ids = ("power", "electric")
    _domains = {"sensor", "input_number", "number"}

    def validate_service(self, service: ServiceRef) -> None:
        unknown = set(dict(service.contract)) - {
            "characteristic", "scale", "default", "min", "max", "precision",
            "constant", "mode", "hold_when_unavailable", "hold_timeout",
        }
        if unknown:
            raise ValueError(
                f"numericMeter has unsupported contract keys: "
                f"{', '.join(sorted(unknown))}"
            )
        _characteristic(service)
        _scale(service)
        _bounds(service)
        _precision(service)
        _mode(service)
        _hold(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("numericMeter requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in self._domains:
            raise ValueError(
                f"numericMeter requires a sensor-like entity "
                f"(got domain {domain!r})"
            )

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        if _mode(service) == "delta":
            await _ensure_delta_state(hass)
        name = _characteristic(service)
        scale = _scale(service)
        precision = _precision(service)
        mode = _mode(service)
        hold_enabled, hold_timeout = _hold(service)
        fallback = service.contract_value("default", 0)

        state = hass.states.get(entity_ids[0])
        raw = state.state if state is not None else None

        # constant 模式：无视 sensor，恒定上报给定值。
        # 用途是把某个特征当成可控变量做校准实验
        # （例：固定报 100，观察云端统计格是否跟着变，从而判断它是否依赖设备上报）。
        forced = service.contract_value("constant", None)
        if forced is not None and not isinstance(forced, bool):
            value: object = _clamp(int(forced), service)
            _LOGGER.debug(
                "numericMeter entity=%s constant=%s → %s=%s",
                entity_ids[0],
                forced,
                name,
                value,
            )
            return {name: value}

        if raw is None or raw in _UNAVAILABLE_STATES:
            # 瞬时不可用不等于"值是 0"。空调伴侣每 7 分钟左右会因本地
            # miio 超时短暂报 unavailable，若此时报 0，面板上的「当前功率」
            # 就会在 2 瓦和 0 瓦之间反复跳（实测一天 17 次）。
            # hold_when_unavailable 打开时沿用上一次的有效值，
            # 超过 hold_timeout 秒仍读不到才回落 default（防设备真断电）。
            value: object = (
                _held_value(entity_ids[0], service, scale, precision, hold_timeout)
                if hold_enabled
                else _clamp(fallback, service)
            )
        else:
            try:
                number = float(raw)
            except (TypeError, ValueError):
                value = _clamp(fallback, service)
            else:
                _remember_good(entity_ids[0], number)
                if mode == "delta":
                    # 增量模式：上报"自上次上报以来的新增量"
                    value = _clamp(
                        _delta_value(entity_ids[0], number, scale, precision),
                        service,
                    )
                    await _persist_delta_state()
                else:
                    value = _clamp(_scale_and_round(number, scale, precision), service)

        _LOGGER.debug(
            "numericMeter entity=%s ha_state=%r mode=%s scale=%s → %s=%s",
            entity_ids[0],
            raw,
            mode,
            scale,
            name,
            value,
        )
        return {name: value}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        # 纯只读计量服务，不下发任何命令
        self.validate_service(service)
        self.validate_entities(entity_ids)


ADAPTER = NumericMeterAdapter()


def _characteristic(service: ServiceRef) -> str:
    name = service.contract_value("characteristic", None)
    if isinstance(name, bool):
        # YAML 1.1 会把裸 on/off/yes/no 解析成布尔
        raise ValueError(
            "numericMeter characteristic was parsed as a boolean "
            f"({name!r}); quote it in YAML, e.g. characteristic: \"on\""
        )
    if not isinstance(name, str) or not name:
        raise ValueError("numericMeter characteristic must be a non-empty string")
    return name


def _scale(service: ServiceRef) -> float:
    value = service.contract_value("scale", 1)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value == 0:
        raise ValueError("numericMeter scale must be a non-zero number")
    return float(value)


def _precision(service: ServiceRef) -> int:
    value = service.contract_value("precision", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
        raise ValueError("numericMeter precision must be an integer within 0..6")
    return value


def _mode(service: ServiceRef) -> str:
    """上报模式。

    absolute（默认）：直接把 sensor 当前值换算后上报。
    delta          ：上报"自上次上报以来的新增量"。见 _delta_value。
    """
    value = service.contract_value("mode", "absolute")
    if value not in ("absolute", "delta"):
        raise ValueError("numericMeter mode must be 'absolute' or 'delta'")
    return value


# 最近一次有效读数：{entity_id: (原始值, monotonic 时间戳)}
_LAST_GOOD: dict[str, tuple[float, float]] = {}


def _hold(service: ServiceRef) -> tuple[bool, int]:
    """hold_when_unavailable / hold_timeout。

    hold_when_unavailable=True 时，读到 unavailable/unknown 不报 default，
    而是沿用上一次的有效值；超过 hold_timeout 秒仍读不到才回落 default。
    """
    enabled = service.contract_value("hold_when_unavailable", False)
    if not isinstance(enabled, bool):
        raise ValueError("numericMeter hold_when_unavailable must be a boolean")
    timeout = service.contract_value("hold_timeout", 1800)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("numericMeter hold_timeout must be a positive integer")
    return enabled, timeout


def _remember_good(entity_id: str, value: float) -> None:
    _LAST_GOOD[entity_id] = (value, time.monotonic())


def _held_value(
    entity_id: str,
    service: ServiceRef,
    scale: float,
    precision: int,
    timeout: int,
) -> object:
    record = _LAST_GOOD.get(entity_id)
    if record is not None:
        value, stamp = record
        age = time.monotonic() - stamp
        if age <= timeout:
            _LOGGER.debug(
                "numericMeter hold_last entity=%s value=%s age=%.0fs",
                entity_id,
                value,
                age,
            )
            return _clamp(_scale_and_round(value, scale, precision), service)
        _LOGGER.debug(
            "numericMeter hold expired entity=%s age=%.0fs > %ss",
            entity_id,
            age,
            timeout,
        )
    return _clamp(service.contract_value("default", 0), service)


def _scale_and_round(number: float, scale: float, precision: int) -> object:
    scaled = number * scale
    rounded = round(scaled, precision)
    if precision <= 0:
        rounded = int(rounded)
    return rounded


# 增量模式的状态：按实体分开保存"上次的原始读数"与"未上报的余数"。
#
# ★ 必须持久化：HA 重启会清空进程内存，若基准丢失，重启后会把"当前读数"
#   当成新基准，从 0 点到重启时刻的增量**永远补不回来**。
#   所以下面用 homeassistant.helpers.storage.Store 落到 .storage/，
#   重启后接着算。Store 不可用时（例如单元自检的假 hass）自动降级为内存模式。
_DELTA_STATE: dict[str, dict[str, float | None]] = {}
_DELTA_STORE_KEY = "oneconnect_bridge.numeric_meter_delta"
_DELTA_STORE_VERSION = 1
_DELTA_STORE: object = None
_DELTA_LOADED = False


async def _ensure_delta_state(hass) -> None:
    """首次调用时从 .storage 载入增量基准（失败则降级为内存模式）。"""
    global _DELTA_STORE, _DELTA_LOADED
    if _DELTA_LOADED:
        return
    _DELTA_LOADED = True  # 先置位，避免并发重入
    try:
        # 延迟导入：自检环境没有 homeassistant 包，不能放在模块顶层
        from homeassistant.helpers.storage import Store

        store = Store(hass, _DELTA_STORE_VERSION, _DELTA_STORE_KEY)
        data = await store.async_load()
        if isinstance(data, dict):
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                last = value.get("last")
                if last is not None and not isinstance(last, (int, float)):
                    continue
                _DELTA_STATE[key] = {
                    "last": last,
                    "rem": float(value.get("rem") or 0.0),
                }
        _DELTA_STORE = store
        _LOGGER.debug(
            "numericMeter delta state loaded: %s",
            {k: v.get("last") for k, v in _DELTA_STATE.items()},
        )
    except Exception as error:  # noqa: BLE001 - 持久化不可用不应影响上报
        _LOGGER.debug(
            "numericMeter delta state unavailable (%s); falling back to memory", error
        )


async def _persist_delta_state() -> None:
    store = _DELTA_STORE
    if store is None:
        return
    try:
        await store.async_save(dict(_DELTA_STATE))  # type: ignore[attr-defined]
    except Exception as error:  # noqa: BLE001
        _LOGGER.debug("numericMeter delta state save failed: %s", error)


def _delta_value(
    entity_id: str, current: float, scale: float, precision: int
) -> object:
    """把"累计读数"转成"本次新增量 × scale"。

    为什么需要这个：华为云的电量统计不是积分，而是**把每次上报值累加**
    （实测确认：连续 7 次报 100，面板涨 7.00 度，powerNum=100）。
    所以想让面板显示真实度数 D，应当报"增量的 100 倍"，让 Σ上报 = D×100。

    ★ 余数补偿：一次 10 分钟的增量可能只有 0.001 度，
      ×100 后 = 0.1，直接取整会变成 0 而永久丢失。
      因此把小数部分累计到下一次，长期精度守恒。

    ★ 归零检测：power_cost_today 每天 0 点归零，
      current < last 说明跨天了 —— 重置基准并返回 0，而不是当成负增量。
    """
    state = _DELTA_STATE.setdefault(entity_id, {"last": None, "rem": 0.0})
    last = state["last"]

    if last is None or current < last:
        state["last"] = current
        state["rem"] = 0.0
        _LOGGER.debug(
            "numericMeter delta baseline reset entity=%s current=%s (was %s)",
            entity_id,
            current,
            last,
        )
        return 0

    raw = (current - last) * scale + float(state["rem"] or 0.0)
    # ★ epsilon 不能省：二进制浮点下 0.110-0.100 = 0.009999999999999995，
    #   ×100 得 0.9999999999999996，直接 int() 会截成 0 而白丢一次增量。
    #   1e-9 足以吸收双精度误差（量级 1e-16），又小到不会改变真实小数。
    value = int(raw + 1e-9)
    remainder = raw - value
    if remainder < 0.0:
        remainder = 0.0
    state["rem"] = remainder
    state["last"] = current
    return value


def _bounds(service: ServiceRef) -> tuple[float | None, float | None]:
    minimum = service.contract_value("min", None)
    maximum = service.contract_value("max", None)
    for label, value in (("min", minimum), ("max", maximum)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise ValueError(f"numericMeter {label} must be a number when given")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("numericMeter min must not exceed max")
    return minimum, maximum


def _clamp(value: object, service: ServiceRef) -> object:
    minimum, maximum = _bounds(service)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value
