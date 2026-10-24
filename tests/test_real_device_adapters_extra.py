from __future__ import annotations

import asyncio
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

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, object],
        *,
        blocking: bool,
    ) -> None:
        self.calls.append((domain, service, data, blocking))


class _FakeHass:
    def __init__(self, states: dict[str, object]) -> None:
        self.services = _FakeServices()
        self.states = SimpleNamespace(get=states.get)


def _state(value: str, **attributes: object) -> SimpleNamespace:
    return SimpleNamespace(state=value, attributes=attributes)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# boolean single-feature services (A0VG fridge)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "service_id",
    ["intelligenceMode", "dailyStatus", "travelMode", "quickFreezingMode", "quickRefrigeratingMode"],
)
def test_boolean_adapters_report_and_apply(service_id: str) -> None:
    adapter = get_service_adapter(service_id)
    hass = _FakeHass({"switch.fridge": _state("on")})
    service = ServiceRef(service_id, service_id)

    assert _run(adapter.read_state(hass, service, ("switch.fridge",))) == {
        service_id: True
    }

    hass = _FakeHass({"switch.fridge": _state("off")})
    assert _run(adapter.read_state(hass, service, ("switch.fridge",))) == {
        service_id: False
    }

    hass = _FakeHass({"switch.fridge": _state("off")})
    _run(adapter.apply_command(hass, service, ("switch.fridge",), {service_id: True}))
    _run(adapter.apply_command(hass, service, ("switch.fridge",), {service_id: False}))
    assert [call[1] for call in hass.services.calls] == ["turn_on", "turn_off"]


def test_boolean_adapter_supports_an_int_encoding() -> None:
    adapter = get_service_adapter("quickFreezingMode")
    hass = _FakeHass({"switch.fridge": _state("on")})
    service = ServiceRef(
        "quickFreezingMode", "quickFreezingMode", (("on_value", 1), ("off_value", 0))
    )

    assert _run(adapter.read_state(hass, service, ("switch.fridge",))) == {
        "quickFreezingMode": 1
    }
    _run(adapter.apply_command(hass, service, ("switch.fridge",), {"quickFreezingMode": 1}))
    assert hass.services.calls[0][1] == "turn_on"


def test_boolean_adapter_supports_a_renamed_characteristic() -> None:
    adapter = get_service_adapter("dailyStatus")
    hass = _FakeHass({"switch.fridge": _state("on")})
    service = ServiceRef("dailyStatus", "dailyStatus", (("characteristic", "on"),))

    assert _run(adapter.read_state(hass, service, ("switch.fridge",))) == {"on": True}


def test_boolean_adapter_accepts_any_domain_but_rejects_bad_contracts() -> None:
    adapter = get_service_adapter("travelMode")
    # The boolean adapters accept any single valid entity id; a domain without
    # turn_on/turn_off simply has no command path.
    adapter.validate_entities(("sensor.temperature",))
    adapter.validate_entities(("binary_sensor.vacuum",))
    adapter.validate_entities(("humidifier.dehumidifier",))
    with pytest.raises(ValueError):
        adapter.validate_entities(())
    with pytest.raises(ValueError):
        adapter.validate_entities(("switch.a", "switch.b"))
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("travelMode", "travelMode", (("factor", 2),)))


def test_boolean_adapter_understands_declared_on_states() -> None:
    # A binary_sensor that reports `cleaning` while running.
    adapter = get_service_adapter("dailyStatus")
    service = ServiceRef(
        "dailyStatus",
        "dailyStatus",
        (("on_states", ["cleaning"]), ("off_states", ["docked", "idle"])),
    )
    hass = _FakeHass({"binary_sensor.vacuum": _state("cleaning")})
    assert _run(adapter.read_state(hass, service, ("binary_sensor.vacuum",))) == {
        "dailyStatus": True
    }

    hass = _FakeHass({"binary_sensor.vacuum": _state("docked")})
    assert _run(adapter.read_state(hass, service, ("binary_sensor.vacuum",))) == {
        "dailyStatus": False
    }

    # Undeclared states are dropped rather than guessed.
    hass = _FakeHass({"binary_sensor.vacuum": _state("exploded")})
    assert _run(adapter.read_state(hass, service, ("binary_sensor.vacuum",))) == {}


def test_boolean_adapter_has_no_command_path_for_read_only_domains() -> None:
    adapter = get_service_adapter("dailyStatus")
    hass = _FakeHass({"sensor.dryer": _state("on")})
    service = ServiceRef("dailyStatus", "dailyStatus")

    _run(adapter.apply_command(hass, service, ("sensor.dryer",), {"dailyStatus": True}))
    assert hass.services.calls == []


def test_boolean_adapter_ignores_unavailable_entities() -> None:
    adapter = get_service_adapter("travelMode")
    hass = _FakeHass({"switch.fridge": _state("unavailable")})
    service = ServiceRef("travelMode", "travelMode")

    assert _run(adapter.read_state(hass, service, ("switch.fridge",))) == {}


# --------------------------------------------------------------------------
# clothesHanger (Hotata airer, 174P) - one binding, several entities
# --------------------------------------------------------------------------

def test_clothes_hanger_combines_every_characteristic() -> None:
    # cover open means the airer is raised, which is action 0 in Profile 174P.
    adapter = get_service_adapter("clothesHanger")
    hass = _FakeHass(
        {
            "cover.airer": _state("open"),
            "light.airer_light": _state("on"),
            "switch.airer_disinfect": _state("off"),
        }
    )
    service = ServiceRef("clothesHanger", "clothesHanger")

    assert _run(
        adapter.read_state(
            hass, service, ("cover.airer", "light.airer_light", "switch.airer_disinfect")
        )
    ) == {"action": 0, "lighting": True, "disinfect": False}


def test_clothes_hanger_applies_commands_per_domain() -> None:
    adapter = get_service_adapter("clothesHanger")
    hass = _FakeHass(
        {
            "cover.airer": _state("closed"),
            "light.airer_light": _state("off"),
            "switch.airer_disinfect": _state("off"),
        }
    )
    service = ServiceRef("clothesHanger", "clothesHanger")
    entities = ("cover.airer", "light.airer_light", "switch.airer_disinfect")

    _run(adapter.apply_command(hass, service, entities, {"action": 2}))
    _run(adapter.apply_command(hass, service, entities, {"lighting": True}))
    _run(adapter.apply_command(hass, service, entities, {"disinfect": True}))

    assert [c[:2] for c in hass.services.calls] == [
        ("cover", "stop_cover"),
        ("light", "turn_on"),
        ("switch", "turn_on"),
    ]


def test_clothes_hanger_honours_a_fields_contract() -> None:
    adapter = get_service_adapter("clothesHanger")
    hass = _FakeHass(
        {"cover.airer": _state("open"), "light.airer_light": _state("on")}
    )
    service = ServiceRef(
        "clothesHanger", "clothesHanger", (("fields", ["lighting"]),)
    )

    # Only the lighting characteristic is reported.
    assert _run(
        adapter.read_state(hass, service, ("cover.airer", "light.airer_light"))
    ) == {"lighting": True}


def test_clothes_hanger_supports_an_action_mapping_override() -> None:
    adapter = get_service_adapter("clothesHanger")
    hass = _FakeHass({"cover.airer": _state("up")})
    service = ServiceRef(
        "clothesHanger", "clothesHanger", (("mapping", {"0": "up", "1": "down"}),)
    )

    assert _run(adapter.read_state(hass, service, ("cover.airer",))) == {"action": 0}


def test_clothes_hanger_reports_status_from_numeric_sensors() -> None:
    adapter = get_service_adapter("clothesHanger")
    hass = _FakeHass({"sensor.airer_status": _state("1")})
    service = ServiceRef("clothesHanger", "clothesHanger")

    assert _run(adapter.read_state(hass, service, ("sensor.airer_status",))) == {
        "status": 1
    }


def test_clothes_hanger_rejects_unknown_domains_and_contracts() -> None:
    adapter = get_service_adapter("clothesHanger")
    with pytest.raises(ValueError):
        adapter.validate_entities(("climate.airer",))
    with pytest.raises(ValueError):
        adapter.validate_entities(())
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("switch", "clothesHanger"))
    with pytest.raises(ValueError):
        adapter.validate_service(
            ServiceRef("clothesHanger", "clothesHanger", (("fields", ["nope"]),))
        )
    with pytest.raises(ValueError):
        adapter.validate_service(
            ServiceRef("clothesHanger", "clothesHanger", (("factor", 2),))
        )


# --------------------------------------------------------------------------
# enum services with per-product tables (action / status)
# --------------------------------------------------------------------------

def test_enum_action_requires_a_mapping_and_maps_both_ways() -> None:
    adapter = get_service_adapter("action")
    # A2GT dishwasher: 0 enable, 1 stop.
    service = ServiceRef(
        "action", "action", (("mapping", {"0": "on", "1": "off"}),)
    )
    hass = _FakeHass({"switch.dishwasher": _state("on")})

    assert _run(adapter.read_state(hass, service, ("switch.dishwasher",))) == {
        "action": 0
    }

    hass = _FakeHass({"switch.dishwasher": _state("off")})
    assert _run(adapter.read_state(hass, service, ("switch.dishwasher",))) == {
        "action": 1
    }

    hass = _FakeHass({"switch.dishwasher": _state("off")})
    _run(adapter.apply_command(hass, service, ("switch.dishwasher",), {"action": 0}))
    assert hass.services.calls[0][:2] == ("switch", "turn_on")


def test_enum_action_supports_cover_products() -> None:
    adapter = get_service_adapter("action")
    # 2QXU projection screen: 0 open, 1 close, 2 pause.
    service = ServiceRef(
        "action",
        "action",
        (("mapping", {"0": "open", "1": "close", "2": "stop"}), ("service", "noop")),
    )
    hass = _FakeHass({"cover.screen": _state("open")})

    assert _run(adapter.read_state(hass, service, ("cover.screen",))) == {"action": 0}


def test_enum_status_is_read_only_by_contract() -> None:
    adapter = get_service_adapter("status")
    # A2GQ dryer: 1 idle, 3 running, 5 finished.
    service = ServiceRef(
        "status",
        "status",
        (("mapping", {"1": "idle", "3": "running", "5": "finished"}),),
    )
    hass = _FakeHass({"sensor.dryer": _state("running")})

    assert _run(adapter.read_state(hass, service, ("sensor.dryer",))) == {"status": 3}

    # The shared action adapter is writable, so a read-only projection must use
    # the dedicated status adapter and must never call an HA service.
    hass = _FakeHass({"sensor.dryer": _state("running")})
    _run(adapter.apply_command(hass, service, ("sensor.dryer",), {"status": 3}))
    assert hass.services.calls == []


def test_enum_adapters_reject_an_empty_or_unknown_mapping() -> None:
    adapter = get_service_adapter("action")
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("action", "action"))
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("action", "action", (("mapping", {}),)))
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("action", "action", (("factor", 2),)))


def test_enum_adapters_drop_unknown_ha_states() -> None:
    adapter = get_service_adapter("status")
    service = ServiceRef("status", "status", (("mapping", {"1": "idle"}),))
    hass = _FakeHass({"sensor.dryer": _state("exploded")})

    assert _run(adapter.read_state(hass, service, ("sensor.dryer",))) == {}


# --------------------------------------------------------------------------
# leftTime / lefttime1 (A2GQ dryer, A2GT dishwasher)
# --------------------------------------------------------------------------

def test_left_time_reports_clamped_minutes() -> None:
    adapter = get_service_adapter("leftTime")
    hass = _FakeHass({"sensor.dryer": _state("idle", remaining_time=42)})
    # A2GQ: sid=leftTime, st=time, characteristic=time (adapter default).
    service = ServiceRef("time", "leftTime")

    assert _run(adapter.read_state(hass, service, ("sensor.dryer",))) == {"time": 42}

    hass = _FakeHass({"sensor.dryer": _state("idle", remaining_time=9999)})
    assert _run(adapter.read_state(hass, service, ("sensor.dryer",))) == {"time": 1440}


def test_left_time_supports_the_dishwasher_characteristic_name() -> None:
    adapter = get_service_adapter("lefttime1")
    hass = _FakeHass({"sensor.dishwasher": _state("idle", remaining_time=30)})
    # A2GT: sid=lefttime1, st=lefttime1, characteristic=lefttime (adapter default).
    service = ServiceRef("lefttime1", "lefttime1")

    assert _run(adapter.read_state(hass, service, ("sensor.dishwasher",))) == {
        "lefttime": 30
    }


def test_left_time_falls_back_to_the_entity_state() -> None:
    adapter = get_service_adapter("leftTime")
    hass = _FakeHass({"sensor.dryer": _state("55")})
    service = ServiceRef("time", "leftTime")

    assert _run(adapter.read_state(hass, service, ("sensor.dryer",))) == {"time": 55}


def test_left_time_rejects_unsupported_contracts() -> None:
    adapter = get_service_adapter("leftTime")
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("leftTime", "leftTime", (("factor", 2),)))


def test_left_time_ignores_unavailable_entities() -> None:
    adapter = get_service_adapter("leftTime")
    hass = _FakeHass({"sensor.dryer": _state("unavailable")})
    service = ServiceRef("leftTime", "leftTime")

    assert _run(adapter.read_state(hass, service, ("sensor.dryer",))) == {}


# --------------------------------------------------------------------------
# registry completeness
# --------------------------------------------------------------------------

def test_registry_exposes_every_real_device_adapter() -> None:
    expected = {
        "switch",
        "switch1",
        "brightness",
        "cct",
        "colour",
        "mode",
        "temperature",
        "motorization",
        "clothesHanger",
        "intelligenceMode",
        "dailyStatus",
        "travelMode",
        "quickFreezingMode",
        "quickRefrigeratingMode",
        "action",
        "status",
        "leftTime",
        "lefttime1",
    }
    for service_id in expected:
        assert get_service_adapter(service_id).service_id == service_id


# --------------------------------------------------------------------------
# read-only numeric services (A0VG fridge temperatures)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "service_id,minimum",
    [
        ("refrigeratorTemperatureC", -38),
        ("freezerTemperatureC", -23),
        ("vtRoomTemperature", -38),
    ],
)
def test_readonly_number_reports_clamped_degrees(service_id: str, minimum: int) -> None:
    adapter = get_service_adapter(service_id)
    hass = _FakeHass({"sensor.fridge": _state("5")})
    service = ServiceRef(service_id, service_id)

    assert _run(adapter.read_state(hass, service, ("sensor.fridge",))) == {
        service_id: 5
    }

    hass = _FakeHass({"sensor.fridge": _state("-100")})
    assert _run(adapter.read_state(hass, service, ("sensor.fridge",))) == {
        service_id: minimum
    }


def test_readonly_number_supports_a_scale_factor() -> None:
    adapter = get_service_adapter("refrigeratorTemperatureC")
    hass = _FakeHass({"sensor.fridge": _state("5.0")})
    service = ServiceRef(
        "refrigeratorTemperatureC",
        "refrigeratorTemperatureC",
        (("factor", 10), ("min", -380), ("max", 500)),
    )

    assert _run(adapter.read_state(hass, service, ("sensor.fridge",))) == {
        "refrigeratorTemperatureC": 50
    }


def test_readonly_number_never_calls_ha_services() -> None:
    adapter = get_service_adapter("freezerTemperatureC")
    hass = _FakeHass({"sensor.fridge": _state("-18")})
    service = ServiceRef("freezerTemperatureC", "freezerTemperatureC")

    _run(adapter.apply_command(hass, service, ("sensor.fridge",), {"freezerTemperatureC": -18}))

    assert hass.services.calls == []


def test_readonly_number_rejects_writable_domains_and_contracts() -> None:
    adapter = get_service_adapter("vtRoomTemperature")
    with pytest.raises(ValueError):
        adapter.validate_entities(("switch.fridge",))
    with pytest.raises(ValueError):
        adapter.validate_service(
            ServiceRef("vtRoomTemperature", "vtRoomTemperature", (("factor", 0),))
        )
    with pytest.raises(ValueError):
        adapter.validate_service(
            ServiceRef("vtRoomTemperature", "vtRoomTemperature", (("mapping", {}),))
        )


def test_readonly_number_ignores_unavailable_entities() -> None:
    adapter = get_service_adapter("vtRoomTemperature")
    hass = _FakeHass({"sensor.fridge": _state("unavailable")})
    service = ServiceRef("vtRoomTemperature", "vtRoomTemperature")

    assert _run(adapter.read_state(hass, service, ("sensor.fridge",))) == {}


# --------------------------------------------------------------------------
# switch/switch1 with climate entities (A0E0 air conditioners)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("service_id", ["switch", "switch1"])
def test_switch_adapters_cover_climate_power(service_id: str) -> None:
    adapter = get_service_adapter(service_id)
    service = ServiceRef("switch", service_id)

    # Any non-off climate state means the unit is running.
    hass = _FakeHass({"climate.ac": _state("cool")})
    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {"on": 1}

    hass = _FakeHass({"climate.ac": _state("off")})
    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {"on": 0}

    hass = _FakeHass({"climate.ac": _state("off")})
    _run(adapter.apply_command(hass, service, ("climate.ac",), {"on": 1}))
    _run(adapter.apply_command(hass, service, ("climate.ac",), {"on": 0}))
    assert [c[:2] for c in hass.services.calls] == [
        ("climate", "turn_on"),
        ("climate", "turn_off"),
    ]


@pytest.mark.parametrize("service_id", ["switch", "switch1"])
def test_switch_adapters_validate_climate_entities(service_id: str) -> None:
    adapter = get_service_adapter(service_id)
    adapter.validate_entities(("climate.ac",))
    with pytest.raises(ValueError):
        adapter.validate_entities(("cover.curtain",))


# --------------------------------------------------------------------------
# fan (2FAT protocol AC, gear enum)
# --------------------------------------------------------------------------

def test_fan_maps_the_seven_mitsubishi_modes() -> None:
    adapter = get_service_adapter("fan")
    service = ServiceRef("fan", "fan")
    # The live MIAS entity reports these exact Chinese labels.
    cases = {
        "自动": 0, "静音": 9, "超低": 1, "低风": 2, "中风": 3, "高风": 5, "超高": 7,
    }
    for label, gear in cases.items():
        hass = _FakeHass({"climate.ac": _state("cool", fan_mode=label)})
        assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {
            "gear": gear
        }, label


def test_fan_maps_english_fan_modes() -> None:
    adapter = get_service_adapter("fan")
    service = ServiceRef("fan", "fan")
    for label, gear in {"auto": 0, "silent": 9, "low": 2, "medium": 3, "high": 5}.items():
        hass = _FakeHass({"climate.ac": _state("cool", fan_mode=label)})
        assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {
            "gear": gear
        }, label


def test_fan_writes_set_fan_mode() -> None:
    adapter = get_service_adapter("fan")
    hass = _FakeHass({"climate.ac": _state("cool", fan_mode="自动")})
    service = ServiceRef("fan", "fan")

    _run(adapter.apply_command(hass, service, ("climate.ac",), {"gear": 3}))

    assert hass.services.calls[0][:2] == ("climate", "set_fan_mode")
    assert hass.services.calls[0][2]["fan_mode"] == "中风"


def test_fan_supports_a_mapping_override_and_fan_entities() -> None:
    adapter = get_service_adapter("fan")
    service = ServiceRef("fan", "fan", (("mapping", {"0": "turbo"}),))
    hass = _FakeHass({"fan.floor": _state("on", fan_mode="turbo")})

    assert _run(adapter.read_state(hass, service, ("fan.floor",))) == {"gear": 0}
    _run(adapter.apply_command(hass, service, ("fan.floor",), {"gear": 0}))
    assert hass.services.calls[0][:2] == ("fan", "set_fan_mode")


def test_fan_drops_unknown_modes_and_rejects_bad_input() -> None:
    adapter = get_service_adapter("fan")
    service = ServiceRef("fan", "fan")
    hass = _FakeHass({"climate.ac": _state("cool", fan_mode="nonsense")})
    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {}

    with pytest.raises(ValueError):
        adapter.validate_entities(("light.bulb",))
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("mode", "fan"))
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("fan", "fan", (("mapping", {}),)))


def test_temperature_writes_a_half_degree_set_point() -> None:
    # 0.5 ℃ air conditioners: the write path must keep the fraction.
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"climate.ac": _state("cool", temperature=26.0)})
    service = ServiceRef(
        "temperature", "temperature",
        ({"characteristic": "targetFloat", "float": True,
          "min": 16, "max": 35, "step": 0.5, "factor": 1}),
    )

    _run(adapter.apply_command(hass, service, ("climate.ac",), {"targetFloat": 26.5}))

    assert hass.services.calls[0][:2] == ("climate", "set_temperature")
    assert hass.services.calls[0][2]["temperature"] == 26.5


def test_fan_numeric_speed_reads_and_writes_percentages() -> None:
    # Midea Profiles expose fan.speed as an int 1..100 instead of an enum.
    adapter = get_service_adapter("fan")
    service = ServiceRef("fan", "fan", (("numeric", True),))

    hass = _FakeHass({"climate.ac": _state("cool", fan_mode="中风")})
    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {"gear": 55}

    _run(adapter.apply_command(hass, service, ("climate.ac",), {"gear": 80}))
    assert hass.services.calls[0][2]["fan_mode"] == "高风"

    # A value near the midpoint snaps to the closest label.
    _run(adapter.apply_command(hass, service, ("climate.ac",), {"gear": 56}))
    assert hass.services.calls[1][2]["fan_mode"] == "中风"


def test_clothes_hanger_raise_and_lower_directions() -> None:
    # Profile 174P: 0 = 升 (raise -> open_cover), 1 = 降 (lower -> close_cover).
    adapter = get_service_adapter("clothesHanger")
    service = ServiceRef("clothesHanger", "clothesHanger")
    hass = _FakeHass({"cover.airer": _state("closed")})

    # App presses 升 (raise) -> the cover must open, not close.
    _run(adapter.apply_command(hass, service, ("cover.airer",), {"action": 0}))
    assert hass.services.calls[0][:2] == ("cover", "open_cover")

    # App presses 降 (lower) -> the cover must close.
    _run(adapter.apply_command(hass, service, ("cover.airer",), {"action": 1}))
    assert hass.services.calls[1][:2] == ("cover", "close_cover")

    # App presses 暂停 (pause) -> stop.
    _run(adapter.apply_command(hass, service, ("cover.airer",), {"action": 2}))
    assert hass.services.calls[2][:2] == ("cover", "stop_cover")


def test_clothes_hanger_reports_the_airer_direction() -> None:
    adapter = get_service_adapter("clothesHanger")
    service = ServiceRef("clothesHanger", "clothesHanger")

    for ha_state, expected in (("open", 0), ("closed", 1)):
        hass = _FakeHass({"cover.airer": _state(ha_state)})
        assert _run(adapter.read_state(hass, service, ("cover.airer",))) == {
            "action": expected
        }, ha_state


def test_clothes_hanger_reports_the_airer_position_as_status() -> None:
    adapter = get_service_adapter("clothesHanger")
    service = ServiceRef("clothesHanger", "clothesHanger")

    # Fully raised -> status 1 (highest); fully lowered -> 2 (lowest).
    for position, expected in ((100, 1), (0, 2), (50, 0)):
        hass = _FakeHass({"cover.airer": _state("open", current_position=position)})
        data = _run(adapter.read_state(hass, service, ("cover.airer",)))
        assert data.get("status") == expected, position
        assert "action" in data
