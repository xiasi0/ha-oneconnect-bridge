"""OneConnect Bridge 映射文档编译."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import yaml

from .catalog import CATALOG_PATH, CatalogError, load_catalog
from .models import BindingField, ChildPlan, GatewayPlan, ProductSpec, ServiceRef

class MappingConfigError(ValueError):
    """Raised when an enabled gateway mapping is invalid."""

def load_mapping_if_present(
    path: Path,
    *,
    catalog_path: Path = CATALOG_PATH,
) -> GatewayPlan | None:
    """文件存在才加载映射；文件缺失只表示网关尚未绑定子设备，属正常状态."""

    if not path.is_file():
        return None
    return load_enabled_mapping(path, catalog_path=catalog_path)

def load_enabled_mapping(
    path: Path,
    *,
    catalog_path: Path = CATALOG_PATH,
) -> GatewayPlan | None:
    """映射显式启用才加载；标志缺失或为假返回 None，与无文件可区分."""

    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise MappingConfigError(f"unable to read gateway mapping {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise MappingConfigError("gateway mapping must have schema: 1")
    if value.get("enabled") is not True:
        return None

    try:
        catalog = load_catalog(catalog_path)
    except CatalogError as error:
        raise MappingConfigError(str(error)) from error

    gateway_serial = value.get("gateway_serial")
    if gateway_serial is not None and not isinstance(gateway_serial, str):
        raise MappingConfigError("gateway_serial must be text when present")
    raw_children = value.get("children")
    if not isinstance(raw_children, dict):
        raise MappingConfigError("enabled gateway mapping must contain children")

    children: list[ChildPlan] = []
    for key, raw_child in raw_children.items():
        if not isinstance(key, str) or not key:
            raise MappingConfigError("child keys must be non-empty text")
        children.append(parse_child(key, raw_child, catalog))
    return GatewayPlan(gateway_serial=gateway_serial, children=tuple(children))

def parse_child(key: str, raw_child: Any, catalog: dict[str, ProductSpec]) -> ChildPlan:
    """Parse and validate one ``children.<key>`` mapping into a ChildPlan."""

    if not isinstance(raw_child, dict):
        raise MappingConfigError(f"child {key} must be a mapping")
    raw_product = raw_child.get("product")
    if not isinstance(raw_product, dict):
        raise MappingConfigError(f"child {key} has no product mapping")
    product_ref = raw_product.get("ref")
    if not isinstance(product_ref, str) or product_ref not in catalog:
        raise MappingConfigError(
            f"child {key} references unknown product {product_ref!r}"
        )

    raw_services = raw_product.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise MappingConfigError(f"child {key} must select at least one service")
    services = tuple(_parse_service(item, key) for item in raw_services)
    bindings = tuple(_parse_bindings(raw_child.get("bindings"), key))
    service_ids = {service.service_id for service in services}
    if any(binding.service_id not in service_ids for binding in bindings):
        raise MappingConfigError(f"child {key} binds an unselected service")
    display = raw_child.get("display")
    if display is not None and not isinstance(display, dict):
        raise MappingConfigError(f"child {key} display must be a mapping")
    lifecycle = raw_child.get("lifecycle")
    if lifecycle is None:
        lifecycle = {}
    if not isinstance(lifecycle, dict):
        raise MappingConfigError(f"child {key} lifecycle must be a mapping")
    on_product_change = lifecycle.get("on_product_change", "block")
    if on_product_change not in {"block", "replace"}:
        raise MappingConfigError(
            f"child {key} on_product_change must be block or replace"
        )
    return ChildPlan(
        key=key,
        product=catalog[product_ref],
        friendly_name=_optional_text(display, "friendly_name"),
        room=_optional_text(display, "room"),
        services=services,
        bindings=bindings,
        on_product_change=on_product_change,
    )

def _parse_service(value: Any, child_key: str) -> ServiceRef:
    if not isinstance(value, dict):
        raise MappingConfigError(f"child {child_key} contains an invalid service")
    service_type = value.get("st")
    service_id = value.get("sid")
    if not all(isinstance(item, str) and item for item in (service_type, service_id)):
        raise MappingConfigError(f"child {child_key} contains an incomplete service")
    return ServiceRef(service_type=service_type, service_id=service_id)

def _parse_bindings(value: Any, child_key: str) -> list[BindingField]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MappingConfigError(f"child {child_key} bindings must be a list")
    result: list[BindingField] = []
    for raw_binding in value:
        if not isinstance(raw_binding, dict):
            raise MappingConfigError(f"child {child_key} contains an invalid binding")
        service_id = raw_binding.get("sid")
        fields = raw_binding.get("fields")
        if not isinstance(service_id, str) or not service_id:
            raise MappingConfigError(f"child {child_key} binding has no sid")
        if not isinstance(fields, dict):
            raise MappingConfigError(f"child {child_key} binding fields must be a mapping")
        for field_name, raw_field in fields.items():
            if not isinstance(field_name, str) or not isinstance(raw_field, dict):
                raise MappingConfigError(f"child {child_key} contains an invalid field binding")
            entity_id = raw_field.get("entity_id")
            source = raw_field.get("source")
            if not isinstance(entity_id, str) or not entity_id:
                raise MappingConfigError(f"child {child_key} field has no entity_id")
            if not isinstance(source, str) or not source:
                raise MappingConfigError(f"child {child_key} field has no source")
            attribute = raw_field.get("attribute")
            if attribute is not None and not isinstance(attribute, str):
                raise MappingConfigError(f"child {child_key} field attribute must be text")
            result.append(
                BindingField(
                    service_id=service_id,
                    field_name=field_name,
                    entity_id=entity_id,
                    source=source,
                    attribute=attribute,
                    ha_to_huawei=_mapping(raw_field.get("ha_to_huawei")),
                    huawei_to_ha=_mapping(raw_field.get("huawei_to_ha")),
                )
            )
    return result

def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MappingConfigError("value conversion must be a mapping")
    return dict(value)

def _optional_text(value: Any, key: str) -> str | None:
    if value is None or key not in value:
        return None
    result = value[key]
    if result is not None and not isinstance(result, str):
        raise MappingConfigError(f"display {key} must be text")
    return result