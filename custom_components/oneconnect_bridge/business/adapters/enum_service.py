"""Generic enum action/status service adapter.

Several Profiles carry a single enum characteristic whose value set differs per
product, so no shared default table can be trusted. This adapter requires the
projection to supply the whole table through the `mapping` contract key and
refuses to guess.

Examples of the same sid with different value sets:

| prodId | sid      | characteristic | enum values                        |
| ------ | -------- | -------------- | ---------------------------------- |
| A2GQ   | action   | action         | 1 启用 / 2 暂停                     |
| A2GT   | action   | action         | 0 启用 / 1 停止                     |
| 2QXU   | action   | action         | 0 开 / 1 关 / 2 暂停                 |
| A2GQ   | status   | status         | 1 空闲 / 2 延迟 / 3 运行 / 4 暂停 / 5 完成 / 6 错误 / 7 关机 |
| A2GT   | status   | status         | 0 空闲 / 1 运行 / 2 完成 / 3 暂停     |

Contract:
  `mapping` (required): `{"<enumVal>": "<HA value>"}`.
  `domains` (optional): allowed HA domains, defaults to the widest set that can
  carry the value.

`read_state` reports the enum value for the current HA state; unknown HA states
are dropped. `apply_command` maps an incoming enum value onto either an HA
service call (`action`) or nothing at all when the service is read-only
(`status`); pass `writable: false` to mark a read-only characteristic.
"""

from __future__ import annotations

from ..models import ServiceRef


class EnumServiceAdapter:
    """Adapter for a single-characteristic enum service with no default table."""

    def __init__(self, service_id: str, *, writable: bool = True) -> None:
        self.service_id = service_id
        self._writable = writable

    # -- validation ------------------------------------------------------

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_id != self.service_id:
            raise ValueError(f"{self.service_id} adapter cannot serve {service.service_id}")
        if not service.service_type:
            raise ValueError(f"{self.service_id} requires a service type")
        allowed = {"mapping", "characteristic"}
        if self._writable:
            allowed.add("service")
        unknown = set(dict(service.contract)) - allowed
        if unknown:
            raise ValueError(
                f"{self.service_id} has unsupported contract keys: "
                + ", ".join(sorted(unknown))
            )
        mapping = _mapping(service)
        if not mapping:
            raise ValueError(f"{self.service_id} contract mapping must not be empty")
        if not self._writable and "service" in dict(service.contract):
            raise ValueError(f"{self.service_id} is read-only and takes no service key")

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError(f"{self.service_id} currently requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or not domain:
            raise ValueError(f"{self.service_id} requires one valid HA entity")

    # -- conversion ------------------------------------------------------

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        characteristic = _characteristic(service)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        reverse = {value: key for key, value in _mapping(service).items()}
        enum_value = reverse.get(state.state)
        if enum_value is None:
            return {}
        try:
            return {characteristic: int(enum_value)}
        except (TypeError, ValueError):
            return {characteristic: enum_value}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        if not self._writable:
            return
        characteristic = _characteristic(service)
        if characteristic not in payload:
            return
        value = payload[characteristic]
        if isinstance(value, bool):
            return
        key = str(value)
        mapping = _mapping(service)
        if key not in mapping:
            return
        target = mapping[key]
        domain, _separator, _object_id = entity_ids[0].partition(".")
        service_name = service.contract_value("service", "turn_on")
        if not isinstance(service_name, str) or not service_name:
            return
        if target in {"on", "off", "toggle"} and domain in {"switch", "input_boolean", "light", "fan"}:
            await hass.services.async_call(
                domain,
                "turn_on" if target == "on" else "turn_off",
                {"entity_id": entity_ids[0]},
                blocking=True,
            )
            return
        if domain == "vacuum":
            # vacuum.start / pause / return_to_base take no option argument.
            await hass.services.async_call(
                domain,
                target,
                {"entity_id": entity_ids[0]},
                blocking=True,
            )
            return
        await hass.services.async_call(
            domain,
            service_name,
            {"entity_id": entity_ids[0], "option": target},
            blocking=True,
        )


def _characteristic(service: ServiceRef) -> str:
    value = service.contract_value("characteristic", service.service_id)
    if isinstance(value, str) and value:
        return value
    return service.service_id


def _mapping(service: ServiceRef) -> dict[str, str]:
    override = service.contract_value("mapping")
    if not isinstance(override, dict) or not override:
        return {}
    cleaned = {}
    for key, value in override.items():
        if isinstance(key, str) and isinstance(value, str) and key and value:
            cleaned[key] = value
    return cleaned


def register_enum_adapter(
    service_id: str, service_type: str | None = None, *, writable: bool = True
) -> EnumServiceAdapter:
    """Build one adapter instance for an enum sid.

    `service_type` only documents the expected Profile `st`; validation accepts
    any non-empty type because Huawei reuses the sid as the service type.
    """
    del service_type
    return EnumServiceAdapter(service_id, writable=writable)
