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
# switch1 (multi-key switch Profiles, default contract from ZG2R)
# --------------------------------------------------------------------------

def test_switch1_reports_int_and_bool_encodings() -> None:
    adapter = get_service_adapter("switch1")
    hass = _FakeHass({"switch.sonoff_1": _state("on")})
    int_service = ServiceRef("switch", "switch1")
    bool_service = ServiceRef("binarySwitch", "switch1", (("on_type", "bool"),))

    assert _run(adapter.read_state(hass, int_service, ("switch.sonoff_1",))) == {"on": 1}
    assert _run(adapter.read_state(hass, bool_service, ("switch.sonoff_1",))) == {"on": True}


def test_switch1_applies_turn_on_and_turn_off() -> None:
    adapter = get_service_adapter("switch1")
    hass = _FakeHass({"switch.sonoff_1": _state("off")})
    service = ServiceRef("switch", "switch1")

    _run(adapter.apply_command(hass, service, ("switch.sonoff_1",), {"on": 1}))
    _run(adapter.apply_command(hass, service, ("switch.sonoff_1",), {"on": 0}))

    assert [call[1] for call in hass.services.calls] == ["turn_on", "turn_off"]


def test_switch1_rejects_unsupported_entities_and_contracts() -> None:
    adapter = get_service_adapter("switch1")
    # Read-only domains are accepted now and simply have no command path.
    adapter.validate_entities(("sensor.temperature",))
    with pytest.raises(ValueError):
        adapter.validate_entities(("cover.curtain",))
    with pytest.raises(ValueError):
        adapter.validate_entities(("switch.a", "switch.b"))
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("light", "switch1"))
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("switch", "switch1", (("factor", 2),)))


# --------------------------------------------------------------------------
# mode (default contract from A0E0 air conditioner)
# --------------------------------------------------------------------------

def test_mode_maps_the_default_air_conditioner_table() -> None:
    adapter = get_service_adapter("mode")
    hass = _FakeHass({"climate.ac": _state("cool")})
    service = ServiceRef("mode", "mode")

    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {"mode": 2}


def test_mode_maps_heat_cool_onto_auto_and_accepts_select_entities() -> None:
    adapter = get_service_adapter("mode")
    hass = _FakeHass(
        {
            "climate.ac": _state("heat_cool"),
            "select.washer_program": _state("cotton"),
        }
    )
    service = ServiceRef("mode", "mode")

    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {"mode": 1}
    assert _run(adapter.read_state(hass, service, ("select.washer_program",))) == {}


def test_mode_applies_set_hvac_mode_and_select_option() -> None:
    adapter = get_service_adapter("mode")
    hass = _FakeHass({"climate.ac": _state("off"), "select.program": _state("off")})
    service = ServiceRef("mode", "mode")

    _run(adapter.apply_command(hass, service, ("climate.ac",), {"mode": 3}))
    _run(adapter.apply_command(hass, service, ("select.program",), {"mode": 2}))

    assert hass.services.calls[0][:2] == ("climate", "set_hvac_mode")
    assert hass.services.calls[0][2]["hvac_mode"] == "heat"
    assert hass.services.calls[1][:2] == ("select", "select_option")
    assert hass.services.calls[1][2]["option"] == "cool"


def test_mode_supports_a_profile_specific_mapping_override() -> None:
    adapter = get_service_adapter("mode")
    hass = _FakeHass({"select.mode": _state("eco")})
    service = ServiceRef("mode", "mode", (("mapping", {"7": "eco"}),))

    assert _run(adapter.read_state(hass, service, ("select.mode",))) == {"mode": 7}
    _run(adapter.apply_command(hass, service, ("select.mode",), {"mode": 7}))
    assert hass.services.calls[0][2]["option"] == "eco"


def test_mode_rejects_unsupported_service_types_and_entities() -> None:
    adapter = get_service_adapter("mode")
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("switch", "mode"))
    with pytest.raises(ValueError):
        adapter.validate_entities(("light.bedroom",))


# --------------------------------------------------------------------------
# temperature (default contract from A0E0, tenths of a degree)
# --------------------------------------------------------------------------

def test_temperature_scales_to_tenths_of_a_degree() -> None:
    adapter = get_service_adapter("temperature")
    # A0E0 declares step 1.0, so the default contract quantizes whole degrees
    # before scaling: 25.5 degC becomes 26 degC and uploads as 260 tenths.
    hass = _FakeHass({"climate.ac": _state("cool", temperature=25.5, current_temperature=28)})
    service = ServiceRef("temperature", "temperature")

    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {
        "target": 260,
        "current": 280,
    }


def test_temperature_keeps_fractional_degrees_without_a_step() -> None:
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"climate.ac": _state("cool", temperature=25.5)})
    service = ServiceRef("temperature", "temperature", (("step", 0.1),))

    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {"target": 255}


def test_temperature_supports_a_whole_degree_factor() -> None:
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"climate.ac": _state("cool", temperature=25)})
    service = ServiceRef("temperature", "temperature", (("factor", 1),))

    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {"target": 25}


def test_temperature_applies_the_inverse_scale() -> None:
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"climate.ac": _state("cool")})
    service = ServiceRef("temperature", "temperature")

    _run(adapter.apply_command(hass, service, ("climate.ac",), {"target": 250}))

    assert hass.services.calls[0][:2] == ("climate", "set_temperature")
    assert hass.services.calls[0][2]["temperature"] == 25.0


def test_temperature_clamps_values_outside_the_contract_limits() -> None:
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"climate.ac": _state("cool", temperature=99)})
    # min/max are Profile ranges in whole degrees, so 99 degC clamps to 30 degC
    # and is uploaded as 300 tenths.
    service = ServiceRef("temperature", "temperature", (("min", 16), ("max", 30)))

    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {"target": 300}
    _run(adapter.apply_command(hass, service, ("climate.ac",), {"target": 990}))
    assert hass.services.calls[0][2]["temperature"] == 30.0


def test_temperature_accepts_set_point_domains() -> None:
    adapter = get_service_adapter("temperature")
    for entity in ("climate.ac", "sensor.setpoint", "number.setpoint", "input_number.sp"):
        adapter.validate_entities((entity,))
    with pytest.raises(ValueError):
        adapter.validate_entities(("light.bulb",))
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("mode", "temperature"))


def test_temperature_supports_a_renamed_characteristic() -> None:
    # A0VG fridge exposes `refrigerator.target` rather than `temperature.target`.
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"sensor.setpoint": _state("4")})
    service = ServiceRef(
        "temperature",
        "refrigerator",
        (("characteristic", "target"), ("factor", 1), ("min", 1), ("max", 9)),
    )

    assert _run(adapter.read_state(hass, service, ("sensor.setpoint",))) == {
        "target": 4
    }


def test_temperature_reads_a_plain_sensor_without_scaling() -> None:
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"sensor.setpoint": _state("-18")})
    service = ServiceRef(
        "temperature",
        "freezer",
        (("factor", 1), ("min", -23), ("max", -15)),
    )

    assert _run(adapter.read_state(hass, service, ("sensor.setpoint",))) == {
        "target": -18
    }


def test_temperature_writes_a_number_set_point() -> None:
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"number.setpoint": _state("4")})
    service = ServiceRef("temperature", "refrigerator", (("factor", 1),))

    _run(adapter.apply_command(hass, service, ("number.setpoint",), {"target": 6}))

    assert hass.services.calls[0][:2] == ("number", "set_value")
    assert hass.services.calls[0][2]["value"] == 6.0


# --------------------------------------------------------------------------
# motorization (default contract from Dooya 1004 curtain)
# --------------------------------------------------------------------------

def test_motorization_reports_current_percent() -> None:
    adapter = get_service_adapter("motorization")
    hass = _FakeHass({"cover.curtain": _state("open", current_position=64)})
    service = ServiceRef("motorization", "motorization")

    assert _run(adapter.read_state(hass, service, ("cover.curtain",))) == {
        "currentPercent": 64
    }


def test_motorization_reports_fully_closed_as_zero() -> None:
    # HA covers use 0..100, so a fully closed cover must upload 0 and not the
    # Dooya Profile's documented minimum of 1.
    adapter = get_service_adapter("motorization")
    hass = _FakeHass({"cover.curtain": _state("closed", current_position=0)})
    service = ServiceRef("motorization", "motorization")

    assert _run(adapter.read_state(hass, service, ("cover.curtain",))) == {
        "currentPercent": 0
    }


def test_motorization_can_opt_into_the_profile_minimum() -> None:
    adapter = get_service_adapter("motorization")
    hass = _FakeHass({"cover.curtain": _state("closed", current_position=0)})
    service = ServiceRef("motorization", "motorization", (("min", 1),))

    assert _run(adapter.read_state(hass, service, ("cover.curtain",))) == {
        "currentPercent": 1
    }


def test_motorization_maps_open_close_and_stop() -> None:
    adapter = get_service_adapter("motorization")
    hass = _FakeHass({"cover.curtain": _state("open")})
    service = ServiceRef("motorization", "motorization")

    _run(adapter.apply_command(hass, service, ("cover.curtain",), {"operationMode": 1}))
    _run(adapter.apply_command(hass, service, ("cover.curtain",), {"operationMode": 2}))
    _run(adapter.apply_command(hass, service, ("cover.curtain",), {"operationMode": 3}))

    assert [call[1] for call in hass.services.calls] == [
        "open_cover",
        "close_cover",
        "stop_cover",
    ]


def test_motorization_maps_target_percent_to_position() -> None:
    adapter = get_service_adapter("motorization")
    hass = _FakeHass({"cover.curtain": _state("open")})
    service = ServiceRef("motorization", "motorization")

    _run(adapter.apply_command(hass, service, ("cover.curtain",), {"targetPercent": 40}))

    assert hass.services.calls[0][:2] == ("cover", "set_cover_position")
    assert hass.services.calls[0][2]["position"] == 40


def test_motorization_ignores_the_ota_action_characteristic() -> None:
    adapter = get_service_adapter("motorization")
    hass = _FakeHass({"cover.curtain": _state("open")})
    service = ServiceRef("motorization", "motorization")

    # `action` is the OTA trigger on this Profile and must not move the cover.
    _run(adapter.apply_command(hass, service, ("cover.curtain",), {"action": 1}))

    assert hass.services.calls == []


def test_motorization_rejects_non_cover_entities() -> None:
    adapter = get_service_adapter("motorization")
    with pytest.raises(ValueError):
        adapter.validate_entities(("light.curtain",))
    with pytest.raises(ValueError):
        adapter.validate_service(ServiceRef("switch", "motorization"))


# --------------------------------------------------------------------------
# additional contract variants and edge cases
# --------------------------------------------------------------------------

def test_temperature_honours_a_step_contract() -> None:
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"climate.ac": _state("cool", temperature=23.4)})
    # 0.5 degC step: 23.4 degC rounds to 23.5 degC and uploads as 235 tenths.
    service = ServiceRef("temperature", "temperature", (("step", 0.5),))

    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {"target": 235}


def test_temperature_ignores_unavailable_entities() -> None:
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"climate.ac": _state("unavailable")})
    service = ServiceRef("temperature", "temperature")

    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {}


def test_motorization_clamps_positions_to_the_contract_range() -> None:
    adapter = get_service_adapter("motorization")
    hass = _FakeHass({"cover.curtain": _state("open", current_position=140)})
    # Some Profiles report 0..100 with 0 meaning fully closed.
    service = ServiceRef("motorization", "motorization", (("min", 0), ("max", 100)))

    assert _run(adapter.read_state(hass, service, ("cover.curtain",))) == {
        "currentPercent": 100
    }
    _run(adapter.apply_command(hass, service, ("cover.curtain",), {"targetPercent": 140}))
    assert hass.services.calls[0][2]["position"] == 100


def test_motorization_ignores_unavailable_covers() -> None:
    adapter = get_service_adapter("motorization")
    hass = _FakeHass({"cover.curtain": _state("unavailable")})
    service = ServiceRef("motorization", "motorization")

    assert _run(adapter.read_state(hass, service, ("cover.curtain",))) == {}


def test_switch1_ignores_unavailable_switches() -> None:
    adapter = get_service_adapter("switch1")
    hass = _FakeHass({"switch.sonoff_1": _state("unavailable")})
    service = ServiceRef("switch", "switch1")

    assert _run(adapter.read_state(hass, service, ("switch.sonoff_1",))) == {}


def test_mode_ignores_unavailable_entities() -> None:
    adapter = get_service_adapter("mode")
    hass = _FakeHass({"climate.ac": _state("unavailable")})
    service = ServiceRef("mode", "mode")

    assert _run(adapter.read_state(hass, service, ("climate.ac",))) == {}


def test_registry_exposes_every_new_adapter() -> None:
    for service_id in ("switch1", "mode", "temperature", "motorization"):
        assert get_service_adapter(service_id).service_id == service_id


def test_temperature_parses_huawei_display_strings() -> None:
    # The cloud renders fridge set-points as '+3℃或34℉' style strings rather
    # than bare numbers; the first signed number is the Celsius value.
    adapter = get_service_adapter("temperature")
    service = ServiceRef(
        "temperature",
        "refrigerator",
        (("characteristic", "target"), ("factor", 1), ("min", 1), ("max", 9)),
    )

    for raw, expected in (
        ("+3℃或34℉", 3),
        ("+9℃或40℉", 9),
        ("-18℃或0℉", 1),   # clamped into the 1..9 contract regardless
    ):
        hass = _FakeHass({"sensor.sp": _state(raw)})
        assert _run(adapter.read_state(hass, service, ("sensor.sp",))) == {
            "target": expected
        }


def test_temperature_parses_a_negative_freezer_display_string() -> None:
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"sensor.sp": _state("-18℃或0℉")})
    service = ServiceRef(
        "temperature",
        "freezer",
        (("factor", 1), ("min", -23), ("max", -15)),
    )

    assert _run(adapter.read_state(hass, service, ("sensor.sp",))) == {"target": -18}


def test_temperature_drops_a_string_without_any_number() -> None:
    adapter = get_service_adapter("temperature")
    hass = _FakeHass({"sensor.sp": _state("unknown-ish")})
    service = ServiceRef("temperature", "refrigerator", (("factor", 1),))

    assert _run(adapter.read_state(hass, service, ("sensor.sp",))) == {}
