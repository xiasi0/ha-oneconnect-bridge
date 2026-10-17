"""OneConnect Bridge 配置流."""
from __future__ import annotations

import logging
import secrets
import socket
from typing import Any

import psutil
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .business import (
    MappingConfigError,
    SUPPORTED_PRODUCT_REFS,
    bind_entities,
    children_view,
    load_catalog,
    remove_children,
    update_child,
)
from .const import (
    CONF_INTERFACE_IP,
    CONF_MAC,
    CONF_SERIAL,
    DOMAIN,
    GATEWAY_STATUS_PAIRING,
)

_LOGGER = logging.getLogger(__name__)

# 可绑定实体域：绑定契约仅要求 on/off 状态 + turn_on/turn_off，
# 只读或多态域（cover、climate 等）绑上去只会得到死开关
CHILD_ENTITY_DOMAINS = [
    "switch",
    "light",
    "input_boolean",
    "fan",
    "siren",
    "humidifier",
    "automation",
]

def _interface_options() -> dict[str, str]:
    options: dict[str, str] = {}
    for name, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if address.family != socket.AF_INET:
                continue
            if address.address.startswith("127."):
                continue
            options[address.address] = f"{name} — {address.address}"
    return options

def _virtual_mac() -> str:
    """随机本地管理位单播 MAC；网关是虚拟设备，MAC 生成一次即云端固定身份."""

    value = bytearray(secrets.token_bytes(6))
    value[0] = (value[0] & 0xFE) | 0x02
    return value.hex().upper()

def _product_options() -> list[dict[str, str]]:
    """Product dropdown entries in ``SelectSelectorConfig`` list form."""

    try:
        catalog = load_catalog()
    except Exception:  # noqa: BLE001 - catalog is bundled; fall back to refs
        catalog = {}
    return [
        {"value": ref, "label": catalog[ref].device_name if ref in catalog else ref}
        for ref in SUPPORTED_PRODUCT_REFS
    ]

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """创建 HA 条目，网关随后异步完成配对（PIN 在通知中展示）."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        options = await self.hass.async_add_executor_job(_interface_options)
        if not options:
            return self.async_abort(reason="no_ipv4_interface")

        if user_input is not None:
            serial = secrets.token_hex(8).upper()
            await self.async_set_unique_id(serial)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"智控网关 {serial}",
                data={
                    CONF_INTERFACE_IP: user_input[CONF_INTERFACE_IP],
                    CONF_MAC: _virtual_mac(),
                    CONF_SERIAL: serial,
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_INTERFACE_IP): vol.In(options)}
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return OptionsFlow()

class OptionsFlow(config_entries.OptionsFlowWithReload):
    """管理网关接口与暴露到智慧生活的子设备."""

    _edit_key: str | None = None

    async def async_step_init(self, user_input=None):
        try:
            children = await children_view(self.hass, self.config_entry.entry_id)
        except (MappingConfigError, OSError):
            return self.async_abort(reason="mapping_invalid")
        menu = ["interface", "add_children"]
        if self.config_entry.runtime_data.runtime.status == GATEWAY_STATUS_PAIRING:
            menu.append("pairing_status")
        if children:
            menu += ["manage_children", "remove_children"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_pairing_status(self, user_input=None):
        """显示当前 PIN 并提交检查：绑定材料落地（beta 门控语义）即收尾."""
        runtime = self.config_entry.runtime_data.runtime
        if await self.hass.async_add_executor_job(runtime.has_bound_material):
            # 收尾会触发条目重载，云端会话自 material.json 恢复；实体转发由门控完成
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )
        pin = runtime.pin
        if pin is None:
            # PIN 过期（TTL 5 分钟）：重开一轮配对生成新 PIN
            await self.hass.async_add_executor_job(runtime.stop)
            await self.hass.async_add_executor_job(runtime.start)
            pin = runtime.pin
        return self.async_show_form(
            step_id="pairing_status",
            data_schema=vol.Schema({}),
            description_placeholders={"pin": pin or "-", "serial": runtime.serial},
        )

    async def async_step_interface(self, user_input=None):
        options = await self.hass.async_add_executor_job(_interface_options)
        current_ip = self.config_entry.options.get(
            CONF_INTERFACE_IP, self.config_entry.data.get(CONF_INTERFACE_IP)
        )
        if user_input is not None:
            interface_ip = user_input[CONF_INTERFACE_IP]
            new_options = {
                **self.config_entry.options,
                CONF_INTERFACE_IP: interface_ip,
            }
            # OptionsFlowWithReload 仅在 options 实际变化时触发重载；
            # CONF_MAC 是云端身份（建条目时生成），此处刻意不碰
            return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(
            step_id="interface",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INTERFACE_IP,
                        default=current_ip,
                    ): vol.In(options)
                }
            ),
        )

    async def async_step_add_children(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            product_ref = user_input["product_ref"]
            if product_ref not in SUPPORTED_PRODUCT_REFS:
                errors["product_ref"] = "product_not_supported"
            else:
                try:
                    result = await bind_entities(
                        self.hass,
                        self.config_entry.entry_id,
                        product_ref,
                        list(user_input["entity_id"]),
                        (user_input.get("room") or "").strip() or None,
                    )
                except HomeAssistantError as error:
                    # 云端应用失败（token/超时/协议拒绝）在表单内呈现，不外溢成 500
                    _LOGGER.debug("add_children failed: %s", error)
                    errors["base"] = "apply_failed"
                else:
                    if result.added:
                        return self.async_abort(
                            reason="bulk_done",
                            description_placeholders={
                                "added": str(len(result.added)),
                                "skipped": str(len(result.skipped)),
                            },
                        )
                    return self.async_abort(
                        reason="nothing_added",
                        description_placeholders={"skipped": str(len(result.skipped))},
                    )

        try:
            children = await children_view(self.hass, self.config_entry.entry_id)
        except (MappingConfigError, OSError):
            return self.async_abort(reason="mapping_invalid")
        bound = sorted({entity for child in children for entity in child.entity_ids})
        # 目录读取走 executor，避免事件循环内阻塞 IO（HA 开发文档硬性要求）
        product_options = await self.hass.async_add_executor_job(_product_options)

        data: dict[Any, Any] = {
            vol.Required("entity_id"): EntitySelector(
                EntitySelectorConfig(
                    domain=CHILD_ENTITY_DOMAINS,
                    multiple=True,
                    exclude_entities=bound,
                )
            ),
            vol.Required("product_ref"): SelectSelector(
                SelectSelectorConfig(
                    options=product_options, mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Optional("room"): TextSelector(TextSelectorConfig()),
        }
        return self.async_show_form(
            step_id="add_children",
            data_schema=vol.Schema(data),
            errors=errors,
        )

    async def async_step_manage_children(self, user_input=None):
        """单选跳转编辑表单；删除在独立步骤."""
        try:
            children = await children_view(self.hass, self.config_entry.entry_id)
        except (MappingConfigError, OSError):
            return self.async_abort(reason="mapping_invalid")
        if not children:
            return self.async_abort(reason="no_children")

        if user_input is not None:
            self._edit_key = user_input.get("edit") or None
            if self._edit_key is None:
                return self.async_abort(reason="nothing_selected")
            return await self.async_step_edit_child()

        options = [{"value": child.key, "label": child.label} for child in children]
        return self.async_show_form(
            step_id="manage_children",
            data_schema=vol.Schema(
                {
                    vol.Required("edit"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            description_placeholders={"count": str(len(children))},
        )

    async def async_step_remove_children(self, user_input=None):
        """批量多选删除；云端删除随计划重载执行."""
        errors: dict[str, str] = {}
        try:
            children = await children_view(self.hass, self.config_entry.entry_id)
        except (MappingConfigError, OSError):
            return self.async_abort(reason="mapping_invalid")
        if not children:
            return self.async_abort(reason="no_children")

        options = [{"value": child.key, "label": child.label} for child in children]
        if user_input is not None:
            keys = [key for key in (user_input.get("remove") or []) if key]
            if not keys:
                return self.async_abort(reason="nothing_selected")
            try:
                removed = await remove_children(self.hass, self.config_entry.entry_id, keys)
            except HomeAssistantError as error:
                _LOGGER.debug("remove_children failed: %s", error)
                errors["base"] = "apply_failed"
            else:
                return self.async_abort(
                    reason="bulk_removed",
                    description_placeholders={"removed": str(len(removed))},
                )

        return self.async_show_form(
            step_id="remove_children",
            data_schema=vol.Schema(
                {
                    vol.Optional("remove", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={"count": str(len(children))},
        )

    async def async_step_edit_child(self, user_input=None):
        key = self._edit_key
        if key is None:
            return self.async_abort(reason="nothing_selected")
        try:
            children = await children_view(self.hass, self.config_entry.entry_id)
        except (MappingConfigError, OSError):
            return self.async_abort(reason="mapping_invalid")
        target = next((child for child in children if child.key == key), None)
        if target is None:
            return self.async_abort(reason="child_missing")

        errors: dict[str, str] = {}
        if user_input is not None:
            product_ref = user_input["product_ref"]
            if product_ref not in SUPPORTED_PRODUCT_REFS:
                errors["product_ref"] = "product_not_supported"
            else:
                try:
                    await update_child(
                        self.hass,
                        self.config_entry.entry_id,
                        key,
                        product_ref,
                        (user_input.get("friendly_name") or "").strip() or None,
                        (user_input.get("room") or "").strip() or None,
                    )
                except (HomeAssistantError, ValueError) as error:
                    _LOGGER.debug("edit_child failed: %s", error)
                    errors["base"] = "apply_failed"
                else:
                    return self.async_abort(
                        reason="child_updated",
                        description_placeholders={"name": target.label},
                    )

        product_options = await self.hass.async_add_executor_job(_product_options)
        data: dict[Any, Any] = {
            vol.Required("product_ref", default=target.product_ref): SelectSelector(
                SelectSelectorConfig(
                    options=product_options, mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Optional(
                "friendly_name", default=target.friendly_name
            ): TextSelector(TextSelectorConfig()),
            vol.Optional("room", default=target.room): TextSelector(
                TextSelectorConfig()
            ),
        }
        return self.async_show_form(
            step_id="edit_child",
            data_schema=vol.Schema(data),
            errors=errors,
            description_placeholders={
                "name": target.label,
                "entity": target.primary_entity_id,
            },
        )