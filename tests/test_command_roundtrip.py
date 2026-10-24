"""End-to-end command round-trip through the encoder, decoder and adapters.

This exercises the path a cloud command takes: compact bridge JSON from the
gateway, decoded into a child command, then dispatched to the bound HA entity
through the service adapter for that sid.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


_BUSINESS_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "oneconnect_bridge"
    / "business"
)
_INTEGRATION_PATH = _BUSINESS_PATH.parent
_integration_package = ModuleType("custom_components.oneconnect_bridge")
_integration_package.__path__ = [str(_INTEGRATION_PATH)]
sys.modules.setdefault("custom_components.oneconnect_bridge", _integration_package)
_business_package = ModuleType("custom_components.oneconnect_bridge.business")
_business_package.__path__ = [str(_BUSINESS_PATH)]
sys.modules.setdefault("custom_components.oneconnect_bridge.business", _business_package)

from custom_components.oneconnect_bridge.business.adapters import get_service_adapter
from custom_components.oneconnect_bridge.business.models import ServiceRef


class _FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object], bool]] = []

    async def async_call(self, domain, service, data, *, blocking) -> None:
        self.calls.append((domain, service, data, blocking))


class _FakeHass:
    def __init__(self, states=None) -> None:
        self.services = _FakeServices()
        state_map = states or {}
        self.states = SimpleNamespace(get=state_map.get)


def _state(value: str, **attributes: object) -> SimpleNamespace:
    return SimpleNamespace(state=value, attributes=attributes)


def _run(coro):
    return asyncio.run(coro)


# Each case mirrors a payload a gateway sends for one child service and asserts
# the HA call the bound adapter must produce.

_COMMAND_CASES = [
    # (sid, st, contract, entity_id, entity_state, payload, expected (domain, service))
    ("switch", "switch", (), "light.bulb", "off", {"on": 1}, ("light", "turn_on")),
    ("switch", "switch", (), "light.bulb", "on", {"on": 0}, ("light", "turn_off")),
    ("switch1", "switch", (), "switch.s1", "off", {"on": 1}, ("switch", "turn_on")),
    ("switch", "switch", (), "climate.ac", "off", {"on": 1}, ("climate", "turn_on")),
    ("switch", "switch", (), "climate.ac", "cool", {"on": 0}, ("climate", "turn_off")),
    ("switch1", "switch", (), "lock.door", "locked", {"on": 1}, ("lock", "unlock")),
    ("switch1", "switch", (), "lock.door", "unlocked", {"on": 0}, ("lock", "lock")),
    (
        "mode",
        "mode",
        (),
        "climate.ac",
        "off",
        {"mode": 2},
        ("climate", "set_hvac_mode"),
    ),
    (
        "temperature",
        "temperature",
        (),
        "climate.ac",
        "cool",
        {"target": 250},
        ("climate", "set_temperature"),
    ),
    (
        "motorization",
        "motorization",
        (),
        "cover.curtain",
        "open",
        {"operationMode": 1},
        ("cover", "open_cover"),
    ),
    (
        "motorization",
        "motorization",
        (),
        "cover.curtain",
        "open",
        {"operationMode": 2},
        ("cover", "close_cover"),
    ),
    (
        "motorization",
        "motorization",
        (),
        "cover.curtain",
        "open",
        {"operationMode": 3},
        ("cover", "stop_cover"),
    ),
    (
        "motorization",
        "motorization",
        (),
        "cover.curtain",
        "open",
        {"targetPercent": 55},
        ("cover", "set_cover_position"),
    ),
    (
        "clothesHanger",
        "clothesHanger",
        (),
        "cover.airer",
        "closed",
        {"action": 1},
        ("cover", "close_cover"),
    ),
    (
        "clothesHanger",
        "clothesHanger",
        (),
        "cover.airer",
        "open",
        {"action": 0},
        ("cover", "open_cover"),
    ),
    (
        "clothesHanger",
        "clothesHanger",
        (),
        "light.airer_light",
        "off",
        {"lighting": True},
        ("light", "turn_on"),
    ),
    (
        "clothesHanger",
        "clothesHanger",
        (),
        "switch.airer_disinfect",
        "off",
        {"disinfect": True},
        ("switch", "turn_on"),
    ),
    (
        "dailyStatus",
        "dailyStatus",
        (),
        "switch.fridge",
        "off",
        {"dailyStatus": True},
        ("switch", "turn_on"),
    ),
    (
        "action",
        "action",
        (("mapping", {"0": "on", "1": "off"}),),
        "switch.dishwasher",
        "off",
        {"action": 0},
        ("switch", "turn_on"),
    ),
]


@pytest.mark.parametrize(
    "sid,st,contract,entity_id,entity_state,payload,expected", _COMMAND_CASES
)
def test_cloud_payload_reaches_the_bound_entity(
    sid, st, contract, entity_id, entity_state, payload, expected
) -> None:
    adapter = get_service_adapter(sid)
    hass = _FakeHass({entity_id: _state(entity_state)})
    service = ServiceRef(st, sid, contract)

    _run(adapter.apply_command(hass, service, (entity_id,), payload))

    assert hass.services.calls, f"{sid} produced no HA call for {payload}"
    assert hass.services.calls[0][:2] == expected


# Read-only services must never call an HA service, even if a cloud command
# somehow arrives for them.

@pytest.mark.parametrize(
    "sid,st,contract,entity_id",
    [
        ("status", "status", (("mapping", {"1": "idle"}),), "sensor.dryer"),
        ("leftTime", "time", (), "sensor.dryer"),
        ("lefttime1", "lefttime1", (), "sensor.dishwasher"),
        ("refrigeratorTemperatureC", "refrigeratorTemperatureC", (), "sensor.fridge"),
        ("freezerTemperatureC", "freezerTemperatureC", (), "sensor.freezer"),
    ],
)
def test_read_only_services_never_call_ha(sid, st, contract, entity_id) -> None:
    adapter = get_service_adapter(sid)
    hass = _FakeHass({entity_id: _state("1")})
    service = ServiceRef(st, sid, contract)

    payload = {sid if sid not in {"leftTime", "lefttime1"} else "time": 1}
    _run(adapter.apply_command(hass, service, (entity_id,), payload))

    assert hass.services.calls == []


# Malformed payloads are dropped rather than forwarded to HA.

@pytest.mark.parametrize(
    "sid,st,payload",
    [
        ("switch", "switch", {}),
        ("switch", "switch", {"on": 5}),
        ("switch", "switch", {"on": "1"}),
        ("mode", "mode", {"mode": 99}),
        ("mode", "mode", {"mode": "cool"}),
        ("temperature", "temperature", {"target": "250"}),
        ("motorization", "motorization", {"operationMode": 9}),
        ("action", "action", {"action": 7}),
    ],
)
def test_malformed_payloads_are_ignored(sid: str, st: str, payload) -> None:
    adapter = get_service_adapter(sid)
    entity = {
        "switch": "switch.s",
        "mode": "climate.ac",
        "temperature": "climate.ac",
        "motorization": "cover.c",
        "action": "switch.d",
    }[sid]
    hass = _FakeHass({entity: _state("off")})
    contract = (("mapping", {"0": "on", "1": "off"}),) if sid == "action" else ()

    _run(adapter.apply_command(hass, ServiceRef(st, sid, contract), (entity,), payload))

    assert hass.services.calls == []
