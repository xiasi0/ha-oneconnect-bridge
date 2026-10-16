"""OneConnect Bridge 子设备适配器."""
from __future__ import annotations

from typing import Any
# 单个华为服务与其绑定的 HA 字段；entity_id_suffix 用于同类型多服务产品（如三键开关）
_SERVICE_SPEC = dict[str, Any]

_ON_FIELD: dict[str, Any] = {
    "source": "state",
    "ha_to_huawei": {"type": "state_map", "map": {"on": 1, "off": 0}},
    "huawei_to_ha": {"type": "state_map", "map": {"0": "off", "1": "on"}},
}

def _switch_spec(sid: str, suffix: str = "") -> _SERVICE_SPEC:
    """One relay/switch service bound to the on/off state of an entity."""
    return {
        "st": "switch",
        "sid": sid,
        "entity_id_suffix": suffix,
        "fields": {"on": dict(_ON_FIELD)},
    }

def _brightness_spec() -> _SERVICE_SPEC:
    return {
        "st": "brightness",
        "sid": "brightness",
        "entity_id_suffix": "",
        "fields": {"brightness": {"source": "attribute", "attribute": "brightness"}},
    }

def _cct_spec() -> _SERVICE_SPEC:
    return {
        "st": "cct",
        "sid": "cct",
        "entity_id_suffix": "",
        "fields": {
            "colorTemperature": {
                "source": "attribute",
                "attribute": "color_temp_kelvin",
            }
        },
    }

def _dim_cct_specs() -> list[_SERVICE_SPEC]:
    """switch + brightness + cct — 调光调色灯/驱动形状（ZG13 实机 sid）."""
    return [_switch_spec("switch"), _brightness_spec(), _cct_spec()]

def _dim_specs() -> list[_SERVICE_SPEC]:
    """switch + brightness — 调光灯形状（上游 1013 样品 ZG14 sid）."""
    return [_switch_spec("switch"), _brightness_spec()]

def _multi_key_specs(count: int) -> list[_SERVICE_SPEC]:
    """N-relay switch: switch1..switchN bound to entity, entity_2, ... ."""
    return [_switch_spec(f"switch{i}", "" if i == 1 else f"_{i}") for i in range(1, count + 1)]

# product_ref -> 有序服务规格表（产品全集对齐上游 1013 catalog；旧 ZG0I/K/ZG0Z/ZG10/ZG11/ZG1V/ZG1I 已随目录替换移除）
PRODUCT_ADAPTERS: dict[str, list[_SERVICE_SPEC]] = {
    # ---- 奥斯陆无线开关（上游实机档案 PCP-OB*；sid 形状沿用多键实锤 ZG2R=switch1）----
    "ZG2R": _multi_key_specs(1),
    "ZG2S": _multi_key_specs(2),
    "ZG2T": _multi_key_specs(3),
    # ---- 调光调色灯/灯带/吊灯/射灯/筒灯/磁吸灯（ZG13 实机 sid；ZG2F-J/ZG17 同形状）----
    "ZG13": _dim_cct_specs(),
    "ZG17": _dim_cct_specs(),
    "ZG2F": _dim_cct_specs(),
    "ZG2G": _dim_cct_specs(),
    "ZG2H": _dim_cct_specs(),
    "ZG2I": _dim_cct_specs(),
    "ZG2J": _dim_cct_specs(),
    # ---- 调光灯（上游样品 ZG14：switch+brightness）----
    "ZG14": _dim_specs(),
    # ---- RGBCW 恒压驱动（上游实机 BER-LD01；colour/subProduct 服务待适配，先出 dim+cct）----
    "ZG0O": _dim_cct_specs(),
    # ---- on/off 单色灯（sid 实锤：ZG0X/Y/R/S/V 适配器；ZG0T/U/W 同族）----
    "ZG0X": [_switch_spec("switch")],
    "ZG0Y": [_switch_spec("switch")],
    "ZG0R": [_switch_spec("switch")],
    "ZG0S": [_switch_spec("switch")],
    "ZG0T": [_switch_spec("switch")],
    "ZG0U": [_switch_spec("switch")],
    "ZG0V": [_switch_spec("switch")],
    "ZG0W": [_switch_spec("switch")],
}

# UI 当前可绑定的产品；扩展时向 PRODUCT_ADAPTERS 追加
SUPPORTED_PRODUCT_REFS: tuple[str, ...] = tuple(PRODUCT_ADAPTERS)

def build_child_raw(
    product_ref: str, entity_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回与映射 YAML 同形的 (services, bindings) 原始字典，供 mapping_config 共享解析器消费."""

    specs = PRODUCT_ADAPTERS.get(product_ref)
    if specs is None:
        raise KeyError(product_ref)
    services: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for spec in specs:
        services.append({"st": spec["st"], "sid": spec["sid"]})
        fields: dict[str, Any] = {}
        for field_name, field in spec["fields"].items():
            bound: dict[str, Any] = {
                "entity_id": entity_id + spec.get("entity_id_suffix", ""),
                "source": field["source"],
            }
            attribute = field.get("attribute")
            if attribute is not None:
                bound["attribute"] = attribute
            ha_to = field.get("ha_to_huawei")
            if ha_to:
                bound["ha_to_huawei"] = ha_to
            huawei_to = field.get("huawei_to_ha")
            if huawei_to:
                bound["huawei_to_ha"] = huawei_to
            fields[field_name] = bound
        bindings.append({"sid": spec["sid"], "fields": fields})
    return services, bindings