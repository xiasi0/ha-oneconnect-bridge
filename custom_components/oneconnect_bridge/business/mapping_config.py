from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..const import MAX_CHILD_DEVICES_PER_GATEWAY
from .adapters import get_service_adapter
from .models import (
    ChildPlan,
    GatewayPlan,
    ProductSpec,
    ResyncPlan,
    ServiceBinding,
    ServiceRef,
)


class MappingConfigError(ValueError):
    pass


def load_enabled_mapping(path: Path) -> GatewayPlan | None:
    """Load service-selected child projections from a gateway mapping."""

    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise MappingConfigError(f"unable to read gateway mapping {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise MappingConfigError("gateway mapping must have schema: 1")
    if value.get("enabled") is not True:
        return None

    gateway_serial = value.get("gateway_serial")
    if gateway_serial is not None and not isinstance(gateway_serial, str):
        raise MappingConfigError("gateway_serial must be text when present")
    raw_children = value.get("children")
    if not isinstance(raw_children, dict):
        raise MappingConfigError("enabled gateway mapping must contain children")
    if len(raw_children) > MAX_CHILD_DEVICES_PER_GATEWAY:
        raise MappingConfigError(
            f"gateway mapping contains {len(raw_children)} child devices; "
            f"the maximum is {MAX_CHILD_DEVICES_PER_GATEWAY}"
        )

    children: list[ChildPlan] = []
    for key, raw_child in raw_children.items():
        if not isinstance(key, str) or not key:
            raise MappingConfigError("child keys must be non-empty text")
        if not isinstance(raw_child, dict):
            raise MappingConfigError(f"child {key} must be a mapping")
        if "adapter" in raw_child or "entities" in raw_child:
            raise MappingConfigError(
                f"child {key} uses removed adapter configuration; "
                "select services and bind HA entities instead"
            )
        if "display" in raw_child:
            raise MappingConfigError(
                f"child {key} uses removed display configuration; "
                "put friendly_name and room under product"
            )

        raw_product = raw_child.get("product")
        if not isinstance(raw_product, dict):
            raise MappingConfigError(f"child {key} has no product mapping")
        product = _parse_product(raw_product, key)
        room = _optional_text(raw_product, "room")
        services = _parse_services(raw_product.get("services"), key)
        bindings = _parse_bindings(raw_child.get("bindings"), key)
        _validate_service_bindings(key, services, bindings)

        children.append(
            ChildPlan(
                key=key,
                product=product,
                room=room,
                services=services,
                bindings=bindings,
            )
        )
    return GatewayPlan(
        gateway_serial=gateway_serial,
        children=tuple(children),
        resync=_parse_resync(value.get("resync")),
    )


def _parse_product(value: dict[str, Any], child_key: str) -> ProductSpec:
    if "device_name" in value:
        raise MappingConfigError(
            f"child {child_key} product uses removed device_name; use friendly_name"
        )
    required_fields = (
        "prod_id",
        "sub_prod_id",
        "device_type",
        "manufacturer",
        "model",
        "protocol_type",
        "setup_type",
    )
    missing = [field for field in required_fields if field not in value]
    if missing:
        raise MappingConfigError(
            f"child {child_key} product is missing fields: {', '.join(missing)}"
        )
    text_fields = ("prod_id", "device_type", "manufacturer", "model")
    if not all(isinstance(value[field], str) and value[field] for field in text_fields):
        raise MappingConfigError(f"child {child_key} product has an invalid text field")
    sub_prod_id = value["sub_prod_id"]
    if not isinstance(sub_prod_id, str):
        raise MappingConfigError(f"child {child_key} product has an invalid sub_prod_id")
    known_fields_only = value.get("known_fields_only", False)
    if not isinstance(known_fields_only, bool):
        raise MappingConfigError(
            f"child {child_key} product known_fields_only must be boolean"
        )
    if not sub_prod_id and not known_fields_only:
        raise MappingConfigError(
            f"child {child_key} empty sub_prod_id requires known_fields_only"
        )
    if any(
        isinstance(value[field], bool) or not isinstance(value[field], int)
        for field in ("protocol_type", "setup_type")
    ):
        raise MappingConfigError(
            f"child {child_key} product has an invalid numeric field"
        )
    return ProductSpec(
        prod_id=value["prod_id"],
        sub_prod_id=sub_prod_id,
        device_type=value["device_type"],
        manufacturer=value["manufacturer"],
        model=value["model"],
        protocol_type=value["protocol_type"],
        setup_type=value["setup_type"],
        friendly_name=_optional_text(value, "friendly_name"),
        known_fields_only=known_fields_only,
    )


def _parse_services(value: Any, child_key: str) -> tuple[ServiceRef, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MappingConfigError(f"child {child_key} product services must be a list")
    result: list[ServiceRef] = []
    service_ids: set[str] = set()
    for raw_service in value:
        if not isinstance(raw_service, dict):
            raise MappingConfigError(f"child {child_key} contains an invalid service")
        unknown = set(raw_service) - {"st", "sid", "contract"}
        if unknown:
            raise MappingConfigError(
                f"child {child_key} service has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        service_type = raw_service.get("st")
        service_id = raw_service.get("sid")
        if not all(
            isinstance(item, str) and item for item in (service_type, service_id)
        ):
            raise MappingConfigError(f"child {child_key} contains an incomplete service")
        if service_id in service_ids:
            raise MappingConfigError(f"child {child_key} selects service {service_id} twice")
        try:
            adapter = get_service_adapter(service_id)
        except ValueError as error:
            raise MappingConfigError(f"child {child_key} service {service_id} is not supported") from error
        contract = _parse_contract(raw_service.get("contract"), child_key, service_id)
        service = ServiceRef(service_type, service_id, contract)
        try:
            adapter.validate_service(service)
        except ValueError as error:
            raise MappingConfigError(
                f"child {child_key} service {service_id} is invalid: {error}"
            ) from error
        service_ids.add(service_id)
        result.append(service)
    return tuple(result)


def _parse_contract(
    value: Any, child_key: str, service_id: str
) -> tuple[tuple[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not key for key in value
    ):
        raise MappingConfigError(
            f"child {child_key} service {service_id} contract must be a mapping"
        )
    return tuple(value.items())


def _parse_bindings(value: Any, child_key: str) -> tuple[ServiceBinding, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MappingConfigError(f"child {child_key} bindings must be a list")
    result: list[ServiceBinding] = []
    binding_ids: set[str] = set()
    for raw_binding in value:
        if not isinstance(raw_binding, dict):
            raise MappingConfigError(f"child {child_key} contains an invalid binding")
        unknown = set(raw_binding) - {"sid", "entities"}
        if unknown:
            raise MappingConfigError(
                f"child {child_key} binding has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        service_id = raw_binding.get("sid")
        entity_ids = raw_binding.get("entities")
        if not isinstance(service_id, str) or not service_id:
            raise MappingConfigError(f"child {child_key} binding has no sid")
        if service_id in binding_ids:
            raise MappingConfigError(f"child {child_key} binds service {service_id} twice")
        if (
            not isinstance(entity_ids, list)
            or not entity_ids
            or any(not isinstance(entity_id, str) or not entity_id for entity_id in entity_ids)
        ):
            raise MappingConfigError(
                f"child {child_key} binding {service_id} must contain non-empty entities"
            )
        binding_ids.add(service_id)
        result.append(ServiceBinding(service_id, tuple(entity_ids)))
    return tuple(result)


def _validate_service_bindings(
    child_key: str,
    services: tuple[ServiceRef, ...],
    bindings: tuple[ServiceBinding, ...],
) -> None:
    selected = {service.service_id for service in services}
    bound = {binding.service_id for binding in bindings}
    if selected != bound:
        missing = sorted(selected - bound)
        unknown = sorted(bound - selected)
        details = []
        if missing:
            details.append(f"unbound services: {', '.join(missing)}")
        if unknown:
            details.append(f"bindings for unselected services: {', '.join(unknown)}")
        raise MappingConfigError(f"child {child_key} has " + "; ".join(details))
    for binding in bindings:
        try:
            get_service_adapter(binding.service_id).validate_entities(binding.entity_ids)
        except ValueError as error:
            raise MappingConfigError(
                f"child {child_key} service {binding.service_id} is invalid: {error}"
            ) from error


def _optional_text(value: Any, key: str) -> str | None:
    if value is None or key not in value:
        return None
    result = value[key]
    if result is not None and not isinstance(result, str):
        raise MappingConfigError(f"product {key} must be text")
    return result


def _parse_resync(value: Any) -> ResyncPlan:
    """解析顶层的 resync 段（可选，缺省即默认开启）。"""

    if value is None:
        return ResyncPlan()
    if not isinstance(value, dict):
        raise MappingConfigError("resync must be a mapping")

    known = {"enabled", "interval", "first_delay", "skip_services"}
    unknown = set(value) - known
    if unknown:
        raise MappingConfigError(
            f"resync has unsupported keys: {', '.join(sorted(unknown))}"
        )

    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MappingConfigError("resync.enabled must be a boolean")

    interval = value.get("interval", 600)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 60 <= interval <= 86400
    ):
        raise MappingConfigError(
            "resync.interval must be an integer within 60..86400 seconds"
        )

    first_delay = value.get("first_delay", 90)
    if (
        isinstance(first_delay, bool)
        or not isinstance(first_delay, int)
        or not 0 <= first_delay <= 3600
    ):
        raise MappingConfigError(
            "resync.first_delay must be an integer within 0..3600 seconds"
        )

    raw_skip = value.get("skip_services")
    if raw_skip is None:
        skip_services = ResyncPlan.skip_services
    else:
        if not isinstance(raw_skip, list) or any(
            not isinstance(item, str) or not item for item in raw_skip
        ):
            raise MappingConfigError("resync.skip_services must be a list of service ids")
        skip_services = tuple(raw_skip)

    return ResyncPlan(
        enabled=enabled,
        interval=interval,
        first_delay=first_delay,
        skip_services=skip_services,
    )
