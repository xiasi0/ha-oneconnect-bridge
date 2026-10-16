"""OneConnect Bridge 映射计划数据模型."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class ProductSpec:
    """Stable Huawei product identity from the bundled catalog."""

    prod_id: str
    sub_prod_id: str
    device_type: str
    manufacturer: str
    model: str
    device_name: str
    protocol_type: int
    setup_type: int

@dataclass(frozen=True)
class ServiceRef:
    """One explicitly selected Huawei service."""

    service_type: str
    service_id: str

@dataclass(frozen=True)
class BindingField:
    """One HA field to Huawei field conversion declaration."""

    service_id: str
    field_name: str
    entity_id: str
    source: str
    attribute: str | None = None
    ha_to_huawei: dict[str, Any] = field(default_factory=dict)
    huawei_to_ha: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ChildPlan:
    """Desired child-device projection under one parent gateway."""

    key: str
    product: ProductSpec
    friendly_name: str | None
    room: str | None
    services: tuple[ServiceRef, ...]
    bindings: tuple[BindingField, ...]
    on_product_change: str = "block"

@dataclass(frozen=True)
class GatewayPlan:
    """Compiled mapping plan for one gateway storage scope."""

    gateway_serial: str | None
    children: tuple[ChildPlan, ...]