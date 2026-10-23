"""Explicit registry of trusted Huawei service adapters."""

from __future__ import annotations

from .api import ServiceAdapter
from .air_conditioner import ADAPTER as AIR_CONDITIONER
from .air_detector import ADAPTER as AIR_DETECTOR
from .air_key import ADAPTER as AIR_KEY
from .alarm import ADAPTER as ALARM
from .battery import ADAPTER as BATTERY
from .brightness import ADAPTER as BRIGHTNESS
from .cct import ADAPTER as CCT
from .cleaner_robot import ADAPTER as CLEANER_ROBOT
from .colour import ADAPTER as COLOUR
from .constant_value import ADAPTER as CONSTANT_VALUE
from .fan import ADAPTER as FAN
from .gas_sensor import ADAPTER as GAS_SENSOR
from .hcho import ADAPTER as HCHO
from .humidity import ADAPTER as HUMIDITY
from .mode import ADAPTER as MODE
from .motor import ADAPTER as MOTOR
from .numeric_meter import ADAPTER as NUMERIC_METER
from .open_level import ADAPTER as OPEN_LEVEL
from .status import ADAPTER as STATUS
from .switch import ADAPTER as SWITCH
from .temperature import ADAPTER as TEMPERATURE
from .wind import ADAPTER as WIND


ADAPTERS: tuple[ServiceAdapter, ...] = (
    SWITCH,
    BRIGHTNESS,
    CCT,
    COLOUR,
    TEMPERATURE,
    HUMIDITY,
    AIR_DETECTOR,
    MOTOR,
    OPEN_LEVEL,
    HCHO,
    ALARM,
    GAS_SENSOR,
    MODE,
    FAN,
    CLEANER_ROBOT,
    STATUS,
    AIR_KEY,
    AIR_CONDITIONER,
    WIND,
    BATTERY,
    CONSTANT_VALUE,
    NUMERIC_METER,
)


def _declared_service_ids(adapter: ServiceAdapter) -> tuple[str, ...]:
    """An adapter may serve several Profile sids through ``service_ids``."""

    declared = getattr(adapter, "service_ids", None)
    if declared is None:
        return (adapter.service_id,)
    ids = tuple(declared)
    if not ids:
        raise ValueError(f"{type(adapter).__name__} declares an empty service_ids")
    return ids


def _build_index() -> dict[str, ServiceAdapter]:
    index: dict[str, ServiceAdapter] = {}
    for adapter in ADAPTERS:
        for service_id in _declared_service_ids(adapter):
            existing = index.get(service_id)
            if existing is not None and existing is not adapter:
                raise ValueError(
                    f"service {service_id} is claimed by both "
                    f"{type(existing).__name__} and {type(adapter).__name__}"
                )
            index[service_id] = adapter
    return index


_SERVICE_ADAPTERS: dict[str, ServiceAdapter] = _build_index()


def registered_services() -> tuple[str, ...]:
    """Every Profile sid this build can bind, sorted for stable output."""

    return tuple(sorted(_SERVICE_ADAPTERS))


def get_service_adapter(service_id: str) -> ServiceAdapter:
    try:
        return _SERVICE_ADAPTERS[service_id]
    except KeyError as error:
        raise ValueError(f"unknown Huawei service: {service_id}") from error
