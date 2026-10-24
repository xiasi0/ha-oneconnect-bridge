"""Explicit registry of trusted Huawei service adapters.

Upstream adapter set (switch, brightness, cct, colour, mode, temperature,
motor, open_level, opener, status, action, ...) combined with the real-device
adapters added on the bench: motorization, clothesHanger, enum-backed
action/status, leftTime countdowns, boolean single-feature services and
read-only numeric measurements.
"""

from __future__ import annotations

from .action import ADAPTER as ACTION
from .alarm import ADAPTER as ALARM
from .air_detector import ADAPTER as AIR_DETECTOR
from .api import ServiceAdapter
from .boolean import register_boolean_adapter
from .brightness import ADAPTER as BRIGHTNESS
from .cct import ADAPTER as CCT
from .cleaner_robot import ADAPTER as CLEANER_ROBOT
from .clothes_hanger import ADAPTER as CLOTHES_HANGER
from .colour import ADAPTER as COLOUR
from .enum_service import register_enum_adapter
from .fan import ADAPTER as FAN
from .gas_sensor import ADAPTER as GAS_SENSOR
from .hcho import ADAPTER as HCHO
from .humidity import ADAPTER as HUMIDITY
from .left_time import register_left_time_adapter
from .mode import ADAPTER as MODE
from .motor import ADAPTER as MOTOR
from .motorization import ADAPTER as MOTORIZATION
from .open_level import ADAPTER as OPEN_LEVEL
from .opener import ADAPTER as OPENER
from .readonly_number import register_readonly_number_adapter
from .status import ADAPTER as STATUS
from .switch import ADAPTER as SWITCH
from .switch1 import ADAPTER as SWITCH1
from .temperature import ADAPTER as TEMPERATURE


# Boolean single-feature services. sid == service type == characteristic name.
# Default contract source: prodId A0VG (海尔冰箱), Profile
# https://smarthome-drcn.dbankcdn.com/device/guide/A0VG/A0VG.json
_BOOLEAN_SERVICE_IDS = (
    "intelligenceMode",
    "dailyStatus",
    "travelMode",
    "quickFreezingMode",
    "quickRefrigeratingMode",
)

# Enum services whose value set differs per product and therefore has no
# default table. Each projection must supply `contract.mapping`.
# A2GQ (西门子干衣机) https://smarthome-drcn.dbankcdn.com/device/guide/A2GQ/A2GQ.json
# A2GT (西门子洗碗机) https://smarthome-drcn.dbankcdn.com/device/guide/A2GT/A2GT.json
_ENUM_SERVICE_IDS = ("action", "status")

# Read-only numeric measurement services.
# A0VG (海尔冰箱) https://smarthome-drcn.dbankcdn.com/device/guide/A0VG/A0VG.json
# Candidate status: contract implemented and unit-tested, end-to-end projection
# not yet confirmed (cloud ACKs dataSync but does not apply the value).
_READONLY_NUMBER_SERVICE_IDS = (
    "refrigeratorTemperatureC",
    "freezerTemperatureC",
    "vtRoomTemperature",
)

# Remaining-time services.
# A2GQ leftTime.time and A2GT lefttime1.lefttime, both int 0..1440 minutes.
_LEFT_TIME_SERVICE_IDS = ("leftTime", "lefttime1")

def _build_adapters() -> dict[str, ServiceAdapter]:
    adapters: dict[str, ServiceAdapter] = {}
    for adapter in (
        SWITCH,
        BRIGHTNESS,
        CCT,
        COLOUR,
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
        MOTORIZATION,
        CLOTHES_HANGER,
    ):
        adapters[adapter.service_id] = adapter
    adapters["switch1"] = SWITCH1
    adapters["leftAction"] = ACTION
    # 9MY5 (HA curtain) Profile uses leftOpener/rightOpener for the
    # opener's position characteristic, mirroring leftAction/rightAction.
    adapters["leftOpener"] = OPENER
    adapters["rightOpener"] = OPENER
    for _service_id in _BOOLEAN_SERVICE_IDS:
        adapters[_service_id] = register_boolean_adapter(_service_id)
    # `status` is method R on the real Profiles; `action` is method RW.
    adapters["action"] = register_enum_adapter("action", writable=True)
    adapters["status"] = register_enum_adapter("status", writable=False)
    for _service_id in _LEFT_TIME_SERVICE_IDS:
        adapters[_service_id] = register_left_time_adapter(_service_id)
    # A0VG fridge set-point services reuse the temperature adapter.
    for _service_id in ("refrigerator", "freezer", "vtRoom", "flexzone"):
        adapters[_service_id] = TEMPERATURE
    # Haier A401 four-door fridges carry target+current in a `heat` service.
    for _service_id in ("refrigeratorFloat", "freezerFloat"):
        adapters[_service_id] = TEMPERATURE
    for _service_id in _READONLY_NUMBER_SERVICE_IDS:
        adapters[_service_id] = register_readonly_number_adapter(_service_id)
    return adapters


_SERVICE_ADAPTERS: dict[str, ServiceAdapter] = _build_adapters()


def get_service_adapter(service_id: str) -> ServiceAdapter:
    try:
        return _SERVICE_ADAPTERS[service_id]
    except KeyError as error:
        raise ValueError(f"unknown Huawei service: {service_id}") from error
