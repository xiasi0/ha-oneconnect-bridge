"""通用「常量上报」服务适配器。

═══════════════════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════════════════

华为 profile 里有相当一批特征是「设备自己上报、HA 侧没有对应实体」的，
典型就是 294P（空调伴侣）的面板配件服务：

    deviceList.list          遥控器索引（H5 要求**真 JS 数组**）
    switchPower.on           屏幕开关
    indicator1.ledOnoff      指示灯

而 mapping_config 的 `_validate_service_bindings` 强制要求：
  · 每个被选中的服务都必须有 binding
  · 每条 binding 都要过 adapter.validate_entities

这些服务在 HA 里根本没有实体可绑。本适配器允许绑定**任意一个已存在实体**
（实践上就绑同设备的 climate / light 本体当占位），read_state 直接返回
contract 里声明的常量值，apply_command 默认忽略下行。

═══════════════════════════════════════════════════════════════════════════
★ 字段名以 H5 为准，不要照抄 profile
═══════════════════════════════════════════════════════════════════════════

294P 的官方 294P.json 与实际 H5 包在字段名上有出入，实测（拿官方 H5 包
+ 伪造 window.hilink 桥跑出来的可用样本 case_F）：

    profile 声明                   H5 实际读取
    power.current            →    power.power
    electric.current         →    electric.totalConsum
    indicator1.on            →    indicator1.ledOnoff

**H5 才是真实契约**，profile 这几处是错的。本适配器不猜，特征名一律由
contract 显式给出。

═══════════════════════════════════════════════════════════════════════════
contract
═══════════════════════════════════════════════════════════════════════════

    characteristic: 特征名（必填，字符串）
    value:          上报值（必填，任意 JSON 可序列化值；数组/对象都行）

例：

    - st: deviceList
      sid: deviceList
      contract:
        characteristic: list
        value:
          - {zip: "1", index: 1, rid: "s_2016093013082803", tid: 7}
默认契约来源：
  prodId：**不绑定单一产品** —— 行为完全由 contract 的 characteristic/value
          驱动，因此不提供默认契约。
  常见性：通用型。适用于「设备需要上报、但 HA 侧没有对应实体」的只读配件服务。
  当前使用场景：294P 空调伴侣的 deviceList / switchPower / indicator1
  三个面板配件服务。

验证（2026-10-21 / HA 2026.10.0 / 华为 OneConnect 网关）：
  真实投影已验证：294P 上三个 sid 的 dataSync 全部 accepted（errcode=0），
  deviceList.list 以真数组形式提交，App 侧遥控器索引正常。
"""

from __future__ import annotations

from ..models import ServiceRef


class ConstantValueAdapter:
    service_id = "constantValue"
    # registry 会为下面每个 sid 登记同一个适配器实例
    service_ids = ("deviceList", "switchPower", "indicator1")
    _domains: set[str] | None = None  # 不限制实体域

    def validate_service(self, service: ServiceRef) -> None:
        unknown = set(dict(service.contract)) - {"characteristic", "value"}
        if unknown:
            raise ValueError(
                f"constantValue has unsupported contract keys: "
                f"{', '.join(sorted(unknown))}"
            )
        _characteristic(service)
        _value(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        # 只做占位绑定，不关心实体域，但必须是"至少一个、且格式合法"
        if len(entity_ids) != 1:
            raise ValueError("constantValue requires exactly one placeholder entity")
        domain, separator, object_id = entity_ids[0].partition(".")
        if not separator or not domain or not object_id:
            raise ValueError("constantValue placeholder entity must look like domain.object_id")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        return {_characteristic(service): _value(service)}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        # 常量服务不接受下行写入：收到命令静默忽略。
        # （294P 的开关类面板项即使被点，也不会改变任何真实状态）
        self.validate_service(service)
        self.validate_entities(entity_ids)


ADAPTER = ConstantValueAdapter()


def _characteristic(service: ServiceRef) -> str:
    name = service.contract_value("characteristic", None)
    if isinstance(name, bool):
        # YAML 1.1 会把裸 on/off/yes/no 解析成布尔：characteristic: on → True
        raise ValueError(
            "constantValue characteristic was parsed as a boolean "
            f"({name!r}); quote it in YAML, e.g. characteristic: \"on\""
        )
    if not isinstance(name, str) or not name:
        raise ValueError("constantValue characteristic must be a non-empty string")
    return name


def _value(service: ServiceRef) -> object:
    marker = object()
    raw = service.contract_value("value", marker)
    if raw is marker:
        raise ValueError("constantValue value is required")
    return raw
