"""OneConnect Bridge 子设备绑定核心."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..const import MAPPING_FILENAME
from ..storage import GatewayStore
from .child_adapters import SUPPORTED_PRODUCT_REFS, build_child_raw
from .catalog import load_catalog
from .mapping_config import (
    MappingConfigError,
    load_mapping_if_present,
    parse_child,
)
from .models import ChildPlan, GatewayPlan

_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

@dataclass(frozen=True)
class ChildView:
    """One bound child as presented by the management UI."""

    key: str
    product_ref: str
    entity_ids: tuple[str, ...]
    friendly_name: str | None
    room: str | None

    @property
    def primary_entity_id(self) -> str:
        return self.entity_ids[0]

    @property
    def label(self) -> str:
        """Human-readable selector label, e.g. ``客厅 — 智能开关 (switch.a)``."""

        name = self.friendly_name or self.entity_ids[0]
        room = f"{self.room} — " if self.room else ""
        return f"{room}{name} ({self.primary_entity_id})"

@dataclass(frozen=True)
class BindResult:
    """Outcome of one batch binding run."""

    added: tuple[str, ...]
    skipped: tuple[str, ...]

def _slug(entity_id: str) -> str:
    """Derive a storage-safe child key from an HA entity id."""

    key = entity_id.replace(".", "_")
    if not _KEY_PATTERN.match(key):
        raise HomeAssistantError(
            f"entity id cannot be used as a child key: {entity_id}"
        )
    return key

def _entity_ids(child: ChildPlan) -> tuple[str, ...]:
    """Every HA entity id one child occupies, in first-seen order."""

    seen: dict[str, None] = {}
    for binding in child.bindings:
        seen.setdefault(binding.entity_id, None)
    return tuple(seen)

def _state_friendly_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """Use the entity's friendly name as the display name when available."""

    state = hass.states.get(entity_id)
    if state is not None:
        name = state.attributes.get("friendly_name")
        if isinstance(name, str) and name:
            return name
    return None

def child_to_raw(child: ChildPlan) -> dict[str, Any]:
    """Serialize one ChildPlan back into the mapping YAML child shape."""

    grouped: dict[str, dict[str, Any]] = {}
    for binding in child.bindings:
        fields = grouped.setdefault(binding.service_id, {})
        field: dict[str, Any] = {
            "entity_id": binding.entity_id,
            "source": binding.source,
        }
        if binding.attribute is not None:
            field["attribute"] = binding.attribute
        if binding.ha_to_huawei:
            field["ha_to_huawei"] = dict(binding.ha_to_huawei)
        if binding.huawei_to_ha:
            field["huawei_to_ha"] = dict(binding.huawei_to_ha)
        fields[binding.field_name] = field
    raw: dict[str, Any] = {
        "product": {
            "ref": child.product.prod_id,
            "services": [
                {"st": service.service_type, "sid": service.service_id}
                for service in child.services
            ],
        },
        "bindings": [
            {"sid": service_id, "fields": fields}
            for service_id, fields in grouped.items()
        ],
    }
    display: dict[str, Any] = {}
    if child.friendly_name is not None:
        display["friendly_name"] = child.friendly_name
    if child.room is not None:
        display["room"] = child.room
    if display:
        raw["display"] = display
    if child.on_product_change != "block":
        raw["lifecycle"] = {"on_product_change": child.on_product_change}
    return raw

def _child_view(child: ChildPlan) -> ChildView:
    return ChildView(
        key=child.key,
        product_ref=child.product.prod_id,
        entity_ids=_entity_ids(child),
        friendly_name=child.friendly_name,
        room=child.room,
    )

def _load_plan(config_dir: str, serial: str) -> GatewayPlan | None:
    """Executor helper: read the mapping document if it exists."""

    store = GatewayStore(config_dir, serial)
    return load_mapping_if_present(store.path(MAPPING_FILENAME))

def _entry_serial(hass: HomeAssistant, entry_id: str) -> str:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise HomeAssistantError(f"unknown config entry: {entry_id}")
    serial = entry.data.get("serial")
    if not isinstance(serial, str) or not serial:
        raise HomeAssistantError(f"config entry {entry_id} has no gateway serial")
    return serial

async def children_view(hass: HomeAssistant, entry_id: str) -> list[ChildView]:
    """List the children currently bound for one gateway entry."""

    serial = _entry_serial(hass, entry_id)
    plan = await hass.async_add_executor_job(
        _load_plan, hass.config.path(), serial
    )
    if plan is None:
        return []
    return [_child_view(child) for child in plan.children]

async def bind_entities(
    hass: HomeAssistant,
    entry_id: str,
    product_ref: str,
    entity_ids: list[str],
    room: str | None = None,
) -> BindResult:
    """把 HA 实体绑定到映射文档中的产品."""

    if product_ref not in SUPPORTED_PRODUCT_REFS:
        raise ValueError(f"unsupported product reference: {product_ref}")

    serial = _entry_serial(hass, entry_id)
    plan = await hass.async_add_executor_job(
        _load_plan, hass.config.path(), serial
    )
    children = list(plan.children) if plan is not None else []
    occupied = {entity_id for child in children for entity_id in _entity_ids(child)}
    known_keys = {child.key for child in children}

    catalog = await hass.async_add_executor_job(load_catalog)
    added: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for entity_id in entity_ids:
        if entity_id in seen:
            skipped.append(entity_id)
            continue
        seen.add(entity_id)
        if entity_id in occupied or hass.states.get(entity_id) is None:
            skipped.append(entity_id)
            continue
        try:
            key = _slug(entity_id)
        except HomeAssistantError:
            skipped.append(entity_id)
            continue
        if key in known_keys:
            skipped.append(entity_id)
            continue
        services, bindings = build_child_raw(product_ref, entity_id)
        children.append(
            parse_child(
                key,
                {
                    "product": {"ref": product_ref, "services": services},
                    "bindings": bindings,
                    "display": {
                        "friendly_name": _state_friendly_name(hass, entity_id),
                        "room": room or None,
                    },
                },
                catalog,
            )
        )
        known_keys.add(key)
        added.append(entity_id)

    if added:
        await _persist_and_apply(hass, entry_id, serial, children)
    return BindResult(added=tuple(added), skipped=tuple(skipped))

async def remove_children(
    hass: HomeAssistant, entry_id: str, keys: list[str]
) -> tuple[str, ...]:
    """Remove children by key; orphan cloud cleanup runs on next session."""

    serial = _entry_serial(hass, entry_id)
    plan = await hass.async_add_executor_job(
        _load_plan, hass.config.path(), serial
    )
    if plan is None:
        return ()
    doomed = {key for key in keys if key in {child.key for child in plan.children}}
    if not doomed:
        return ()
    children = [child for child in plan.children if child.key not in doomed]
    await _persist_and_apply(hass, entry_id, serial, children)
    return tuple(
        child.key for child in plan.children if child.key in doomed
    )

async def update_child(
    hass: HomeAssistant,
    entry_id: str,
    key: str,
    product_ref: str,
    friendly_name: str | None,
    room: str | None,
) -> None:
    """以新显示数据重投影单个子设备."""

    if product_ref not in SUPPORTED_PRODUCT_REFS:
        raise ValueError(f"unsupported product reference: {product_ref}")

    serial = _entry_serial(hass, entry_id)
    plan = await hass.async_add_executor_job(
        _load_plan, hass.config.path(), serial
    )
    if plan is None:
        raise HomeAssistantError(f"gateway mapping has no child {key!r}")
    target = next((child for child in plan.children if child.key == key), None)
    if target is None:
        raise HomeAssistantError(f"gateway mapping has no child {key!r}")

    catalog = await hass.async_add_executor_job(load_catalog)
    primary_entity = _entity_ids(target)[0]
    services, bindings = build_child_raw(product_ref, primary_entity)
    replacement = parse_child(
        key,
        {
            "product": {"ref": product_ref, "services": services},
            "bindings": bindings,
            "display": {"friendly_name": friendly_name or None, "room": room or None},
            "lifecycle": {"on_product_change": target.on_product_change},
        },
        catalog,
    )
    children = [
        replacement if child.key == key else child for child in plan.children
    ]
    await _persist_and_apply(hass, entry_id, serial, children)

async def _persist_and_apply(
    hass: HomeAssistant,
    entry_id: str,
    serial: str,
    children: list[ChildPlan],
) -> None:
    """Write the mapping document, validate it, then apply the plan once."""

    document = {
        "schema": 1,
        "enabled": True,
        "gateway_serial": serial,
        "children": {child.key: child_to_raw(child) for child in children},
    }

    def _write_and_validate() -> GatewayPlan:
        store = GatewayStore(hass.config.path(), serial)
        store.write_mapping(document)
        plan = load_mapping_if_present(store.path(MAPPING_FILENAME))
        if plan is None:
            raise MappingConfigError("written mapping failed to reload")
        return plan

    try:
        await hass.async_add_executor_job(_write_and_validate)
    except (MappingConfigError, OSError) as error:
        raise HomeAssistantError(f"unable to save gateway mapping: {error}") from error

    entry = hass.config_entries.async_get_entry(entry_id)
    reload_children = getattr(getattr(entry, "runtime_data", None), "reload_children", None)
    if reload_children is not None:
        await reload_children()

__all__ = [
    "BindResult",
    "ChildView",
    "bind_entities",
    "children_view",
    "remove_children",
    "update_child",
]