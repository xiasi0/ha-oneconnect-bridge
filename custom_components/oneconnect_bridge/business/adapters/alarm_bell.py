"""Huawei ``alarmBell`` read-only service adapter: pressure presence.

Default contract source
    prodId   : ``9MTM`` -- smart control pressure sensor
    Profile  : https://smarthome-drcn.dbankcdn.com/device/guide/9MTM/9MTM.json
    common   : the only entry in the public catalog whose ``deviceTypeName`` is
               ``压力传感器``; other products containing 压力 are electric
               pressure cookers
    verified : implementation follows the Profile's read-only ``mode`` enum
               (1 none / 2 present); a real projection is still required

Naming
    The service is called ``alarmBell`` for historical reasons and its Profile
    section is titled 报警铃声, but the single characteristic's ``attrName`` is
    ``有无压力``. Detect the meaning from ``attrName``, not from ``serviceId``.
"""

from __future__ import annotations

from ..models import ServiceRef


class AlarmBellServiceAdapter:
    service_id = "alarmBell"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "mode":
            raise ValueError("alarmBell requires st: mode")
        if service.contract:
            raise ValueError("alarmBell does not accept contract overrides")

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1 or not entity_ids[0].startswith("binary_sensor."):
            raise ValueError("alarmBell requires exactly one binary_sensor entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None:
            return {}
        if state.state == "on":
            return {"mode": 2}
        if state.state == "off":
            return {"mode": 1}
        return {}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)


ADAPTER = AlarmBellServiceAdapter()
