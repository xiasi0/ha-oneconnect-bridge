"""Explicit registry of trusted Huawei service adapters."""

from __future__ import annotations

from .action import ADAPTER as ACTION
from .api import ServiceAdapter
from .alarm import ADAPTER as ALARM
from .air_detector import ADAPTER as AIR_DETECTOR
from .brightness import ADAPTER as BRIGHTNESS
from .cct import ADAPTER as CCT
from .cleaner_robot import ADAPTER as CLEANER_ROBOT
from .colour import ADAPTER as COLOUR
from .electric import ADAPTER as ELECTRIC
from .filter_element import ADAPTER as FILTER_ELEMENT
from .gas_sensor import ADAPTER as GAS_SENSOR
from .fan import ADAPTER as FAN
from .hcho import ADAPTER as HCHO
from .humidity import ADAPTER as HUMIDITY
from .mode import ADAPTER as MODE
from .motor import ADAPTER as MOTOR
from .open_level import ADAPTER as OPEN_LEVEL
from .opener import ADAPTER as OPENER
from .status import ADAPTER as STATUS
from .switch import ADAPTER as SWITCH
from .temperature import ADAPTER as TEMPERATURE


_SERVICE_ADAPTERS: dict[str, ServiceAdapter] = {
    adapter.service_id: adapter
    for adapter in (
        SWITCH,
        BRIGHTNESS,
        CCT,
        COLOUR,
        ELECTRIC,
        TEMPERATURE,
        HUMIDITY,
        AIR_DETECTOR,
        MOTOR,
        OPEN_LEVEL,
        OPENER,
        HCHO,
        ALARM,
        GAS_SENSOR,
        MODE,
        FAN,
        CLEANER_ROBOT,
        STATUS,
        ACTION,
        FILTER_ELEMENT,
    )
}
for _service_id in ("switch1", "switch2", "switch3"):
    _SERVICE_ADAPTERS[_service_id] = SWITCH
_SERVICE_ADAPTERS["leftAction"] = ACTION
# A consumable filter is exposed under its own ``sid`` per filter slot, so a
# product with two filters publishes ``filterElement1`` and ``filterElement2``.
# The numbered variants behave identically, so they all reuse this adapter.
for _service_id in (
    "filterElement",
    "filterElement1",
    "filterElement2",
    "filterElement3",
    "filterElement4",
    "filterElement5",
    "filterElement6",
    "filterElement7",
    "filterElement8",
    "commonFilterElement1",
    "commonFilterElement2",
):
    _SERVICE_ADAPTERS[_service_id] = FILTER_ELEMENT


def get_service_adapter(service_id: str) -> ServiceAdapter:
    try:
        return _SERVICE_ADAPTERS[service_id]
    except KeyError as error:
        raise ValueError(f"unknown Huawei service: {service_id}") from error
