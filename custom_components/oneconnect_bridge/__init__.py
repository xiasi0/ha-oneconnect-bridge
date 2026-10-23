from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.translation import async_get_translations
import voluptuous as vol

from .business import MappingConfigError, load_enabled_mapping
from .const import DOMAIN, MAX_GATEWAY_INSTANCES
from .business.child_cleanup import ChildCleanupClient
from .gateway import GatewayConfig, GatewayLimitError, GatewayManager, GatewayRuntime
from .ha_adapter import HAEntityBridge
from .storage import GatewayStore


PLATFORMS: list[str] = ["binary_sensor", "sensor", "button"]
_LOGGER = logging.getLogger(__name__)
DEFAULT_MAPPING_PATH = Path(__file__).with_name("default_mapping.yaml")


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


def _prepare_gateway_mapping(config_dir: str, serial: str):
    """Create the per-gateway sample and compile it only when enabled."""

    store = GatewayStore(config_dir, serial)
    path = store.ensure_mapping_file(DEFAULT_MAPPING_PATH)
    return path, load_enabled_mapping(path)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    await async_get_translations(
        hass,
        hass.config.language,
        "exceptions",
        integrations={DOMAIN},
    )
    data = hass.data.setdefault(
        DOMAIN,
        {
            "manager": GatewayManager(),
            "runtimes": {},
            "forwarded_entries": set(),
            "gateway_mappings": {},
            "reload_callbacks": {},
        },
    )
    if not hass.services.has_service(DOMAIN, "start_pairing"):
        async def start_pairing(call) -> None:
            serial = call.data["serial"]
            runtime = data["manager"].get(serial)
            if runtime is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_gateway",
                    translation_placeholders={"serial": serial},
                )
            await hass.async_add_executor_job(runtime.start_pairing)

        hass.services.async_register(
            DOMAIN,
            "start_pairing",
            start_pairing,
            schema=vol.Schema({vol.Required("serial"): vol.Coerce(str)}),
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(
        DOMAIN,
        {
            "manager": GatewayManager(),
            "runtimes": {},
            "forwarded_entries": set(),
            "gateway_mappings": {},
            "reload_callbacks": {},
        },
    )
    notification_translations = await async_get_translations(
        hass,
        hass.config.language,
        "common",
        integrations={DOMAIN},
    )

    def create_notification(
        key: str,
        notification_id: str,
        **placeholders: str,
    ) -> None:
        prefix = f"component.{DOMAIN}.common.{key}"
        persistent_notification.async_create(
            hass,
            notification_translations[f"{prefix}_message"].format(**placeholders),
            title=notification_translations[f"{prefix}_title"].format(**placeholders),
            notification_id=notification_id,
        )

    config = GatewayConfig(
        config_dir=hass.config.path(),
        interface_ip=entry.options.get("interface_ip", entry.data["interface_ip"]),
        mac=entry.options.get("mac", entry.data["mac"]),
        serial=entry.data["serial"],
    )
    try:
        runtime = data["manager"].create(config)
    except GatewayLimitError as error:
        create_notification(
            "gateway_limit",
            notification_id=f"{DOMAIN}_gateway_limit",
            limit=str(MAX_GATEWAY_INSTANCES),
            error=str(error),
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="gateway_limit",
            translation_placeholders={"limit": str(MAX_GATEWAY_INSTANCES)},
        ) from error
    persistent_notification.async_dismiss(hass, f"{DOMAIN}_gateway_limit")
    data["runtimes"][entry.entry_id] = runtime
    mapping_notification_id = f"{DOMAIN}_{runtime.serial}_mapping_error"
    try:
        mapping_path, gateway_plan = await hass.async_add_executor_job(
            _prepare_gateway_mapping,
            hass.config.path(),
            runtime.serial,
        )
    except MappingConfigError as error:
        _LOGGER.error("Invalid gateway mapping for %s: %s", runtime.serial, error)
        mapping_path = GatewayStore(hass.config.path(), runtime.serial).path(
            "mapping.yaml"
        )
        gateway_plan = None
        create_notification(
            "mapping_not_loaded",
            notification_id=mapping_notification_id,
            serial=runtime.serial,
            error=str(error),
        )
    else:
        persistent_notification.async_dismiss(hass, mapping_notification_id)
    runtime.set_gateway_plan(gateway_plan)
    entity_bridge = HAEntityBridge(hass)
    runtime.set_entity_bridge(entity_bridge)
    def install_state_listeners(plan) -> None:
        old_state_remover = data.setdefault("state_removers", {}).pop(entry.entry_id, None)
        if old_state_remover is not None:
            old_state_remover()
        old_startup_remover = data.setdefault("startup_sync_removers", {}).pop(
            entry.entry_id, None
        )
        if old_startup_remover is not None:
            old_startup_remover()
        old_resync_remover = data.setdefault("periodic_resync_removers", {}).pop(
            entry.entry_id, None
        )
        if old_resync_remover is not None:
            old_resync_remover()
        if plan is not None:
            data["state_removers"][entry.entry_id] = entity_bridge.setup_state_listener(
                plan,
                runtime.enqueue_child_state,
            )
            data["startup_sync_removers"][entry.entry_id] = entity_bridge.schedule_startup_sync(
                plan,
                runtime.enqueue_child_state,
            )
            # 周期性全量重报：Profile 要求"每 N 分钟上报"的特征（如 294P 的
            # electric.current）只靠事件驱动拿不到时间序列；电池、温湿度这类
            # 慢变读数也需要定期刷新云端影子。默认开启，见 ResyncPlan。
            data["periodic_resync_removers"][entry.entry_id] = (
                entity_bridge.schedule_periodic_resync(
                    plan,
                    runtime.enqueue_child_state,
                )
            )

    install_state_listeners(gateway_plan)
    data.setdefault("gateway_mappings", {})[entry.entry_id] = {
        "path": mapping_path,
        "plan": gateway_plan,
    }
    if gateway_plan is None:
        _LOGGER.debug("Gateway %s child mapping is disabled", runtime.serial)
    else:
        _LOGGER.info(
            "Loaded %d child mappings for gateway %s",
            len(gateway_plan.children),
            runtime.serial,
        )

    async def reload_children() -> None:
        current_path = data["gateway_mappings"][entry.entry_id]["path"]
        if current_path is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="mapping_file_unavailable",
            )
        try:
            new_plan = await hass.async_add_executor_job(
                load_enabled_mapping,
                current_path,
            )
            await hass.async_add_executor_job(runtime.reload_plan, new_plan)
        except MappingConfigError as error:
            _LOGGER.error("Gateway %s child mapping is invalid: %s", runtime.serial, error)
            create_notification(
                "mapping_not_reloaded",
                notification_id=mapping_notification_id,
                serial=runtime.serial,
                error=str(error),
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="mapping_reload_failed",
                translation_placeholders={"error": str(error)},
            ) from error
        except Exception as error:  # noqa: BLE001 - button reports failure in logs
            _LOGGER.error("Gateway %s child mapping reload failed: %s", runtime.serial, error)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="mapping_reload_failed",
                translation_placeholders={"error": str(error)},
            ) from error
        persistent_notification.async_dismiss(hass, mapping_notification_id)
        data["gateway_mappings"][entry.entry_id]["plan"] = new_plan
        install_state_listeners(new_plan)
        _LOGGER.info("Gateway %s child mapping reload requested", runtime.serial)

    data.setdefault("reload_callbacks", {})[entry.entry_id] = reload_children
    forwarded_entries: set[str] = data.setdefault("forwarded_entries", set())
    has_bound_material = await hass.async_add_executor_job(runtime.has_bound_material)

    notification_id = f"{DOMAIN}_{runtime.serial}_pairing"

    def sync_pairing_notification() -> None:
        if runtime.status == "pairing" and runtime.pin:
            create_notification(
                "pairing",
                notification_id=notification_id,
                serial=runtime.serial,
                pin=runtime.pin,
            )
        else:
            persistent_notification.async_dismiss(hass, notification_id)

    async def forward_platforms() -> None:
        try:
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        except Exception:
            forwarded_entries.discard(entry.entry_id)
            _LOGGER.exception(
                "Unable to create OneConnect Bridge entities for %s",
                runtime.serial,
            )

    def schedule_platforms_if_online() -> None:
        if (
            not has_bound_material
            and runtime.status != "online"
        ) or entry.entry_id in forwarded_entries:
            return
        forwarded_entries.add(entry.entry_id)
        hass.async_create_task(forward_platforms())

    def on_runtime_change() -> None:
        def update_on_loop() -> None:
            sync_pairing_notification()
            schedule_platforms_if_online()

        hass.loop.call_soon_threadsafe(update_on_loop)

    data.setdefault("notification_removers", {})[entry.entry_id] = runtime.add_listener(
        on_runtime_change
    )
    await hass.async_add_executor_job(runtime.start)
    sync_pairing_notification()
    schedule_platforms_if_online()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False
    data = hass.data[DOMAIN]
    data.get("forwarded_entries", set()).discard(entry.entry_id)
    data.get("gateway_mappings", {}).pop(entry.entry_id, None)
    data.get("reload_callbacks", {}).pop(entry.entry_id, None)
    state_remover = data.get("state_removers", {}).pop(entry.entry_id, None)
    if state_remover is not None:
        state_remover()
    startup_sync_remover = data.get("startup_sync_removers", {}).pop(entry.entry_id, None)
    if startup_sync_remover is not None:
        startup_sync_remover()
    resync_remover = data.get("periodic_resync_removers", {}).pop(entry.entry_id, None)
    if resync_remover is not None:
        resync_remover()
    runtime: GatewayRuntime | None = data["runtimes"].pop(entry.entry_id, None)
    if runtime is not None:
        remover = data.get("notification_removers", {}).pop(entry.entry_id, None)
        if remover is not None:
            remover()
        await hass.async_add_executor_job(runtime.stop)
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_{runtime.serial}_pairing"
        )
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_{runtime.serial}_mapping_error"
        )
        data["manager"].remove(runtime)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove this gateway's persisted identity and runtime materials."""

    serial = entry.data.get("serial")
    if not isinstance(serial, str) or not serial:
        return
    data = hass.data.get(DOMAIN)
    if data is not None:
        data.get("forwarded_entries", set()).discard(entry.entry_id)
        data.get("gateway_mappings", {}).pop(entry.entry_id, None)
        data.get("reload_callbacks", {}).pop(entry.entry_id, None)
        state_remover = data.get("state_removers", {}).pop(entry.entry_id, None)
        if state_remover is not None:
            state_remover()
        startup_sync_remover = data.get("startup_sync_removers", {}).pop(entry.entry_id, None)
        if startup_sync_remover is not None:
            startup_sync_remover()
        resync_remover = data.get("periodic_resync_removers", {}).pop(entry.entry_id, None)
        if resync_remover is not None:
            resync_remover()
        runtime: GatewayRuntime | None = data.get("runtimes", {}).pop(entry.entry_id, None)
        if runtime is not None:
            await hass.async_add_executor_job(runtime.stop)
            data["manager"].remove(runtime)
        remover = data.get("notification_removers", {}).pop(entry.entry_id, None)
        if remover is not None:
            remover()
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_{serial}_pairing"
        )
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_{serial}_mapping_error"
        )

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
