"""Huawei `fan.gear` service adapter for air-conditioner wind speed.

Default contract source: prodId `2FAT` (协议空调 MC3-Ac), Profile
https://smarthome-drcn.dbankcdn.com/device/guide/2FAT/2FAT.json
whose `fan.gear` characteristic is an `enum` with ten steps:

| gear | meaning (zh) | typical HA fan_mode |
| ---- | ------------ | ------------------- |
| 0    | 自动         | auto                |
| 1    | 超低风       | 超低 / lowest       |
| 2    | 低风         | 低风 / low          |
| 3    | 中风         | 中风 / medium       |
| 4    | 中高风       | 中高                |
| 5    | 高风         | 高风 / high         |
| 6    | 强风         | 强风                |
| 7    | 超强风       | 超高 / turbo        |
| 8    | 停止         | off                 |
| 9    | 静音         | 静音 / silent       |

Home Assistant exposes `fan_mode` as localised strings, so the adapter maps by
exact string first and then by a case-insensitive substring match on the
declared table. The whole table is overridable through `contract.mapping`.

Verified against the live entity of a Mitsubishi MIAS unit whose `fan_modes`
attribute is `['自动','静音','超低','低风','中风','高风','超高']`; all seven
values round-trip through the table below.
"""

from __future__ import annotations

from ..models import ServiceRef


_DEFAULT_MAPPING = {
    "0": "自动",
    "1": "超低",
    "2": "低风",
    "3": "中风",
    "4": "中高",
    "5": "高风",
    "6": "强风",
    "7": "超高",
    "8": "停止",
    "9": "静音",
}

# Common English spellings, used when the HA entity reports fan_mode in English.
_ALIASES = {
    "auto": "0",
    "automatic": "0",
    "silent": "9",
    "quiet": "9",
    "lowest": "1",
    "low": "2",
    "mid": "3",
    "medium": "3",
    "midhigh": "4",
    "high": "5",
    "strong": "6",
    "turbo": "7",
    "max": "7",
    "off": "8",
}


class FanServiceAdapter:
    service_id = "fan"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "fan":
            raise ValueError("fan requires st: fan")
        unknown = set(dict(service.contract)) - {"mapping", "characteristic", "numeric"}
        if unknown:
            raise ValueError(
                "fan has unsupported contract keys: " + ", ".join(sorted(unknown))
            )
        override = service.contract_value("mapping")
        if override is not None:
            if not isinstance(override, dict) or not override:
                raise ValueError("fan contract mapping must be a non-empty mapping")
            if not _mapping(service):
                raise ValueError("fan contract mapping must not be empty")
        if _numeric(service):
            # numeric speed Profiles carry an int 1..100 instead of an enum.
            mapping = _mapping(service)
            for key, value in mapping.items():
                if not key.isdigit():
                    raise ValueError(
                        "fan numeric mapping keys must be integers, got " + key
                    )
                if isinstance(value, str) and value.isdigit():
                    raise ValueError(
                        "fan numeric mapping values must be fan_mode labels"
                    )

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("fan currently requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in {"climate", "fan"}:
            raise ValueError("fan requires a climate or fan entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        fan_mode = state.attributes.get("fan_mode")
        if not isinstance(fan_mode, str) or not fan_mode:
            return {}
        gear = _lookup(service, fan_mode)
        if gear is None:
            return {}
        numeric = _numeric(service)
        if numeric:
            speed = _NUMERIC_SPEED.get(str(gear))
            if speed is None:
                return {}
            return {_characteristic(service): speed}
        return {_characteristic(service): int(gear)}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        value = payload.get(_characteristic(service))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if _numeric(service):
            target = None
            best = None
            for key, speed in _NUMERIC_SPEED.items():
                distance = abs(float(speed) - float(value))
                if best is None or distance < best:
                    best = distance
                    target = _mapping(service).get(key)
            if target is None:
                return
        else:
            target = _mapping(service).get(str(int(value)))
            if target is None:
                return
        domain = entity_ids[0].partition(".")[0]
        await hass.services.async_call(
            domain,
            "set_fan_mode",
            {"entity_id": entity_ids[0], "fan_mode": target},
            blocking=True,
        )


ADAPTER = FanServiceAdapter()


# fan_mode label -> percentage used by `fan.speed` Profiles (int 1..100).
_NUMERIC_SPEED = {
    "0": 60,   # 自动
    "1": 25,   # 超低
    "2": 40,   # 低风
    "3": 55,   # 中风
    "4": 68,   # 中高
    "5": 80,   # 高风
    "6": 88,   # 强风
    "7": 95,   # 超高
    "8": 1,    # 停止
    "9": 15,   # 静音
    "10": 32,  # 中低风
    "11": 100, # 超高风
}


def _numeric(service: ServiceRef) -> bool:
    return bool(service.contract_value("numeric", False))


def _characteristic(service: ServiceRef) -> str:
    value = service.contract_value("characteristic", "gear")
    if isinstance(value, str) and value:
        return value
    return "gear"


def _mapping(service: ServiceRef) -> dict[str, str]:
    override = service.contract_value("mapping")
    if not isinstance(override, dict) or not override:
        return dict(_DEFAULT_MAPPING)
    cleaned = {}
    for key, value in override.items():
        if isinstance(key, str) and isinstance(value, str) and key and value:
            cleaned[key] = value
    return cleaned or dict(_DEFAULT_MAPPING)


def _lookup(service: ServiceRef, fan_mode: str) -> str | None:
    """Resolve one HA fan_mode string to a Huawei gear value."""
    table = _mapping(service)
    # 1) exact match against the declared table
    for gear, label in table.items():
        if label == fan_mode:
            return gear
    # 2) exact match against the English aliases
    alias = _ALIASES.get(fan_mode.strip().lower())
    if alias is not None and alias in table:
        return alias
    # 3) substring match, longest label first so '超低' wins over '低'
    for gear, label in sorted(table.items(), key=lambda item: -len(item[1])):
        if label and label in fan_mode:
            return gear
    return None
