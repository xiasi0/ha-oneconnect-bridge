"""OneConnect Bridge 集成入口."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir, translation

from .business import GatewayPlan, MappingConfigError, load_mapping_if_present
from .business.child_cleanup import ChildCleanupClient
from .const import (
    DOMAIN,
    GATEWAY_STATUS_ONLINE,
    GATEWAY_STATUS_PAIRING,
    MAX_GATEWAY_INSTANCES,
    MAPPING_FILENAME,
    PLATFORMS,
)
from .gateway import (
    GatewayConfig,
    GatewayLimitError,
    GatewayManager,
    GatewayRuntime,
)
from .ha_adapter import HAEntityBridge
from .services import async_setup_services
from .storage import GatewayStore

_LOGGER = logging.getLogger(__name__)

# 通知 category：persistent_notification 无 translation_key，经 helper 取词
_NOTIFICATION_CATEGORY = "notification"

def _pairing_notification_id(runtime: GatewayRuntime) -> str:
    return f"{DOMAIN}_{runtime.serial}_pairing"

def _mapping_issue_id(entry: HhbConfigEntry) -> str:
    return f"mapping_invalid_{entry.entry_id}"

@dataclass
class GatewayEntryData:
    """Per-entry runtime objects exposed as ``entry.runtime_data``."""

    runtime: GatewayRuntime
    mapping_path: str | None = None
    plan: GatewayPlan | None = None
    platforms_forwarded: bool = False
    state_remover: Callable[[], None] | None = None
    startup_sync_remover: Callable[[], None] | None = None
    notification_remover: Callable[[], None] | None = None
    reload_children: Callable[[], Awaitable[None]] | None = None

type HhbConfigEntry = ConfigEntry[GatewayEntryData]

def _teardown_entry_data(
    hass: HomeAssistant, entry: HhbConfigEntry
) -> GatewayRuntime | None:
    """解绑单条目的运行时对象（幂等）；返回 runtime 供调用方在 executor 中停线程."""

    entry_data: GatewayEntryData | None = getattr(entry, "runtime_data", None)
    if entry_data is None:
        return None
    entry.runtime_data = None
    if entry_data.state_remover is not None:
        entry_data.state_remover()
    if entry_data.startup_sync_remover is not None:
        entry_data.startup_sync_remover()
    if entry_data.notification_remover is not None:
        entry_data.notification_remover()
    runtime = entry_data.runtime
    persistent_notification.async_dismiss(
        hass, _pairing_notification_id(runtime)
    )
    get_gateway_manager(hass).remove(runtime)
    return runtime

def get_gateway_manager(hass: HomeAssistant) -> GatewayManager:
    """返回域级 manager（幂等）；配置流在建条目前也会调用本函数."""
    return hass.data.setdefault(DOMAIN, {"manager": GatewayManager()})["manager"]

def _read_gateway_removal_material(config_dir: str, serial: str):
    """Read removal-time files outside Home Assistant's event loop."""

    store = GatewayStore(config_dir, serial)
    material = store.read_json("cloud/material.json")
    ca_path = store.path("certificates/huawei_root_ca_trust.json")
    return (
        material,
        ca_path if ca_path.is_file() else None,
        store.read_child_materials(),
    )

def _remove_gateway_storage(config_dir: str, serial: str) -> None:
    """Remove one gateway's storage outside Home Assistant's event loop."""

    GatewayStore(config_dir, serial).remove_gateway()

def _load_gateway_mapping(config_dir: str, serial: str):
    """解析映射路径并按需加载（映射文件由配置界面按需创建，未绑定过则不存在)."""

    store = GatewayStore(config_dir, serial)
    path = store.path(MAPPING_FILENAME)
    return path, load_mapping_if_present(path)

def _report_mapping_issue(
    hass: HomeAssistant, entry: HhbConfigEntry, error: Exception
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        _mapping_issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="mapping_invalid",
        translation_placeholders={
            "serial": entry.data["serial"],
            "error": str(error),
        },
    )

async def async_update_pairing_notification(
    hass: HomeAssistant, runtime: GatewayRuntime, notification_id: str
) -> None:
    """按当前语言创建或撤销配对通知（自定义 category 经 en.json 兜底)."""

    if runtime.status != GATEWAY_STATUS_PAIRING or not runtime.pin:
        persistent_notification.async_dismiss(hass, notification_id)
        return

    strings = await translation.async_get_translations(
        hass, hass.config.language, _NOTIFICATION_CATEGORY, {DOMAIN}
    )
    prefix = f"component.{DOMAIN}.{_NOTIFICATION_CATEGORY}."
    message = (
        strings[f"{prefix}pairing_message"]
        .replace("{serial}", runtime.serial)
        .replace("{pin}", runtime.pin)
    )
    persistent_notification.async_create(
        hass,
        message,
        title=strings[f"{prefix}pairing_title"],
        notification_id=notification_id,
    )

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    get_gateway_manager(hass)
    await async_setup_services(hass)
    return True

async def async_setup_entry(hass: HomeAssistant, entry: HhbConfigEntry) -> bool:
    manager: GatewayManager = get_gateway_manager(hass)
    config = GatewayConfig(
        config_dir=hass.config.path(),
        interface_ip=entry.options.get("interface_ip", entry.data["interface_ip"]),
        mac=entry.data["mac"],
        serial=entry.data["serial"],
    )
    try:
        runtime = manager.create(config)
    except GatewayLimitError as error:
        # 上游实锤上限：单 HA 进程最多 3 个独立网关实例
        _LOGGER.error("Gateway limit reached: %s", error)
        strings = await translation.async_get_translations(
            hass, hass.config.language, _NOTIFICATION_CATEGORY, {DOMAIN}
        )
        prefix = f"component.{DOMAIN}.{_NOTIFICATION_CATEGORY}."
        persistent_notification.async_create(
            hass,
            strings[f"{prefix}gateway_limit_message"].format(
                limit=MAX_GATEWAY_INSTANCES, error=error
            ),
            title=strings[f"{prefix}gateway_limit_title"],
            notification_id=f"{DOMAIN}_gateway_limit",
        )
        raise HomeAssistantError(str(error)) from error
    persistent_notification.async_dismiss(hass, f"{DOMAIN}_gateway_limit")
    entry_data = entry.runtime_data = GatewayEntryData(runtime=runtime)

    try:
        mapping_path, gateway_plan = await hass.async_add_executor_job(
            _load_gateway_mapping,
            hass.config.path(),
            runtime.serial,
        )
    except MappingConfigError as error:
        _LOGGER.error("Invalid gateway mapping for %s: %s", runtime.serial, error)
        _report_mapping_issue(hass, entry, error)
        mapping_path, gateway_plan = None, None
    else:
        ir.async_delete_issue(hass, DOMAIN, _mapping_issue_id(entry))

    runtime.set_gateway_plan(gateway_plan)
    entity_bridge = HAEntityBridge(hass)
    runtime.set_entity_bridge(entity_bridge)

    def install_state_listeners(plan) -> None:
        if entry_data.state_remover is not None:
            entry_data.state_remover()
            entry_data.state_remover = None
        if entry_data.startup_sync_remover is not None:
            entry_data.startup_sync_remover()
            entry_data.startup_sync_remover = None
        if plan is not None:
            entry_data.state_remover = entity_bridge.setup_state_listener(
                plan,
                runtime.enqueue_child_state,
            )
            entry_data.startup_sync_remover = entity_bridge.schedule_startup_sync(
                plan,
                runtime.enqueue_child_state,
            )

    install_state_listeners(gateway_plan)
    entry_data.mapping_path = mapping_path
    entry_data.plan = gateway_plan
    if gateway_plan is None:
        _LOGGER.debug("Gateway %s child mapping is disabled", runtime.serial)
    else:
        _LOGGER.info(
            "Loaded %d child mappings for gateway %s",
            len(gateway_plan.children),
            runtime.serial,
        )

    async def reload_children() -> None:
        if entry_data.mapping_path is None:
            raise HomeAssistantError("gateway mapping file is not available")
        try:
            new_plan = await hass.async_add_executor_job(
                load_mapping_if_present,
                entry_data.mapping_path,
            )
            await hass.async_add_executor_job(runtime.reload_plan, new_plan)
        except Exception as error:  # noqa: BLE001 - button reports failure in logs
            _LOGGER.error(
                "Gateway %s child mapping reload failed: %s", runtime.serial, error
            )
            _report_mapping_issue(hass, entry, error)
            raise HomeAssistantError(str(error)) from error
        ir.async_delete_issue(hass, DOMAIN, _mapping_issue_id(entry))
        entry_data.plan = new_plan
        install_state_listeners(new_plan)
        _LOGGER.info("Gateway %s child mapping reload requested", runtime.serial)

    entry_data.reload_children = reload_children

    notification_id = _pairing_notification_id(runtime)
    has_bound_material = gateway_plan is not None and bool(gateway_plan.children)

    async def forward_platforms() -> None:
        try:
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        except Exception:
            entry_data.platforms_forwarded = False
            _LOGGER.exception(
                "Unable to create OneConnect Bridge entities for %s",
                runtime.serial,
            )

    def schedule_platforms_if_online() -> None:
        if (
            not has_bound_material
            and runtime.status != GATEWAY_STATUS_ONLINE
        ) or entry_data.platforms_forwarded:
            return
        entry_data.platforms_forwarded = True
        # 条目级任务：条目卸载时自动取消
        entry.async_create_task(hass, forward_platforms())

    def on_runtime_change() -> None:
        def update_on_loop() -> None:
            entry.async_create_task(
                hass, async_update_pairing_notification(hass, runtime, notification_id)
            )
            schedule_platforms_if_online()

        hass.loop.call_soon_threadsafe(update_on_loop)

    entry_data.notification_remover = runtime.add_listener(on_runtime_change)
    await hass.async_add_executor_job(runtime.start)
    await async_update_pairing_notification(hass, runtime, notification_id)
    schedule_platforms_if_online()
    return True

async def async_unload_entry(hass: HomeAssistant, entry: HhbConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False
    runtime = _teardown_entry_data(hass, entry)
    if runtime is not None:
        # join 云/配对线程；禁止阻塞事件循环
        await hass.async_add_executor_job(runtime.stop)
    return True

async def async_remove_entry(hass: HomeAssistant, entry: HhbConfigEntry) -> None:
    """Remove this gateway's persisted identity and runtime materials."""

    ir.async_delete_issue(hass, DOMAIN, _mapping_issue_id(entry))
    runtime = _teardown_entry_data(hass, entry)
    if runtime is not None:
        await hass.async_add_executor_job(runtime.stop)
    serial = entry.data.get("serial")
    if not isinstance(serial, str) or not serial:
        return

    material, ca_path, child_materials = await hass.async_add_executor_job(
        _read_gateway_removal_material,
        hass.config.path(),
        serial,
    )
    mac = entry.data.get("mac")
    if (
        material
        and isinstance(mac, str)
        and isinstance(material.get("devId"), str)
        and isinstance(material.get("secret"), str)
        and isinstance(material.get("cloudPrimaryUrl"), str)
    ):
        client = ChildCleanupClient(
            serial_number=serial,
            mac=mac,
            cloud_primary_url=material["cloudPrimaryUrl"],
            cloud_standby_url=(
                material.get("cloudStandbyUrl")
                if isinstance(material.get("cloudStandbyUrl"), str)
                else None
            ),
            ca_json=ca_path,
        )
        try:
            result = await hass.async_add_executor_job(
                client.remove_children_then_revoke,
                material,
                child_materials,
            )
            if result.failed:
                _LOGGER.warning(
                    "Gateway %s was revoked, but child cloud deletion failed for: %s",
                    serial,
                    ", ".join(result.failed),
                )
            else:
                _LOGGER.info(
                    "Gateway %s child devices deleted before parent revoke: %d",
                    serial,
                    len(result.deleted),
                )
        except Exception as error:  # noqa: BLE001 - local deletion must finish
            _LOGGER.warning(
                "Gateway %s cloud child cleanup/revoke failed during HA removal; local materials "
                "will still be deleted: %s",
                serial,
                error,
            )
    await hass.async_add_executor_job(
        _remove_gateway_storage,
        hass.config.path(),
        serial,
    )