from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductSpec:
    prod_id: str
    sub_prod_id: str
    device_type: str
    manufacturer: str
    model: str
    protocol_type: int
    setup_type: int
    friendly_name: str | None = None
    known_fields_only: bool = False


@dataclass(frozen=True)
class ServiceRef:
    service_type: str
    service_id: str
    contract: tuple[tuple[str, object], ...] = ()

    def contract_value(self, key: str, default: object = None) -> object:
        return dict(self.contract).get(key, default)


@dataclass(frozen=True)
class ServiceBinding:
    service_id: str
    entity_ids: tuple[str, ...]


@dataclass(frozen=True)
class ChildPlan:
    key: str
    product: ProductSpec
    room: str | None
    services: tuple[ServiceRef, ...]
    bindings: tuple[ServiceBinding, ...] = ()


@dataclass(frozen=True)
class ResyncPlan:
    """周期性全量状态重同步配置。

    为什么需要：桥接的状态上报是**事件驱动**的（HA 实体变化才上报）。
    这带来两个问题：

    1. 厂商 Profile 明确要求"定期上报"的特征拿不到时间序列。
       例：294P 的 `electric.current`，官方 descCh 原文是
       「实时电流 单位:mA 每10分钟上报」。
    2. 读数型特征（电池电量、温湿度）长期不变，云端影子会永远停在
       启动那一刻的快照上。

    默认**开启**：不开启时，读数型特征（电池、温湿度）的云端影子会永远
    停在启动那一刻，属于"接进去当天能看、第二天就不动"的静默故障。
    确属纯事件驱动产品（如只有开关/调光的灯）时可显式关闭以省掉这笔开销。

    `skip_services` 默认跳过 `power`：官方对 `power.current` 的定义是
    「实时功率 单位:瓦 **变化上报**」，对它做周期重报属于违背语义，
    而且会让云端把它当成周期性数据参与统计。
    """

    enabled: bool = True
    interval: int = 600
    first_delay: int = 90
    skip_services: tuple[str, ...] = ("power",)


@dataclass(frozen=True)
class GatewayPlan:
    gateway_serial: str | None
    children: tuple[ChildPlan, ...]
    resync: ResyncPlan = ResyncPlan()

