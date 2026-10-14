"""OneConnect Bridge 私有 CoAP/TCP 帧协议."""
from __future__ import annotations

from dataclasses import dataclass
import json

COAP_POST = 0x02
URI_PATH = 11
ACCESS_TOKEN_ID = 2049
REQUEST_ID = 2050
DEVICE_ID = 2051
USER_ID = 2052
SEQUENCE_NUMBER = 2053
COAP_PAYLOAD_MARKER = 0xFF
BRIDGE_COAP_TCP_HEADER = 0x92
BRIDGE_COAP_CONTENT = 0x45

class CoapTcpProtocolError(ValueError):
    """Raised when a private bridge CoAP/TCP message is malformed."""

@dataclass(frozen=True)
class CoapTcpOption:
    """One option in a bridge CoAP/TCP message."""

    number: int
    value: bytes

    @classmethod
    def text(cls, number: int, value: str) -> "CoapTcpOption":
        return cls(number=number, value=value.encode("utf-8"))

@dataclass(frozen=True)
class BridgeCloudMessage:
    """Bridge-binary CoAP/TCP response carried inside a two-byte frame."""

    code: int
    token: bytes
    options: tuple[CoapTcpOption, ...]
    payload: bytes

    @classmethod
    def decode(cls, packet: bytes) -> "BridgeCloudMessage":
        if len(packet) < 2:
            raise CoapTcpProtocolError("bridge response is missing its frame length")
        frame_length = int.from_bytes(packet[:2], "big")
        if frame_length != len(packet) - 2:
            raise CoapTcpProtocolError("bridge response frame length does not match")
        data = packet[2:]
        if len(data) < 4:
            raise CoapTcpProtocolError("bridge response is shorter than its CoAP header")
        token_length = data[0] & 0x0F
        if token_length > 8 or 2 + token_length > len(data):
            raise CoapTcpProtocolError("bridge response token length is invalid")
        code = data[1]
        token = data[2 : 2 + token_length]
        options, payload = _decode_options_and_payload(data[2 + token_length :])
        return cls(code=code, token=token, options=tuple(options), payload=payload)

@dataclass(frozen=True)
class BridgeCloudRequest:
    """Raw request framing observed in the official HiLink bridge binary."""

    code: int
    token: bytes
    paths: tuple[str, ...]
    sequence: bytes
    payload: bytes
    access_token: str | None = None

    def encode(self) -> bytes:
        if not 0 <= self.code <= 0xFF:
            raise CoapTcpProtocolError("bridge CoAP code must fit one byte")
        if len(self.token) != 2:
            raise CoapTcpProtocolError("captured bridge cloud tokens are two bytes")
        if not self.paths:
            raise CoapTcpProtocolError("bridge cloud request requires a URI path")
        if len(self.payload) > 0xFFFFFFFF:
            raise CoapTcpProtocolError("bridge payload exceeds the four-byte length")

        header = bytearray((BRIDGE_COAP_TCP_HEADER, self.code))
        header.extend(self.token)
        previous = 0
        for path in self.paths:
            option = CoapTcpOption.text(URI_PATH, path)
            _append_bridge_option(header, previous, option)
            previous = option.number
        if self.access_token is not None:
            if not self.access_token:
                raise CoapTcpProtocolError("bridge access token cannot be empty")
            option = CoapTcpOption.text(ACCESS_TOKEN_ID, self.access_token)
            _append_bridge_option(header, previous, option)
            previous = option.number
        _append_bridge_option(
            header,
            previous,
            CoapTcpOption(SEQUENCE_NUMBER, self.sequence),
        )
        header.append(COAP_PAYLOAD_MARKER)
        header.extend(len(self.payload).to_bytes(4, "big"))
        if len(header) > 0xFFFF:
            raise CoapTcpProtocolError("bridge header exceeds its two-byte length")
        return len(header).to_bytes(2, "big") + bytes(header) + self.payload

@dataclass(frozen=True)
class BridgeDeviceInfo:
    """Gateway descriptor values observed in the bridge activation request."""

    serial_number: str
    mac: str
    model: str = "DL-01W"
    device_type: str = "A17"
    manufacturer: str = "002"
    product_id: str = "ZG37"
    protocol_type: int = 1
    firmware_version: str = "1.0.0"
    hardware_version: str = "1.0.0"
    software_version: str = "1.0.0"

    def as_activation_payload(self) -> dict[str, object]:
        return {
            "sn": self.serial_number,
            "model": self.model,
            "devType": self.device_type,
            "manu": self.manufacturer,
            "prodId": self.product_id,
            "mac": self.mac,
            "hiv": "1.0",
            "fwv": self.firmware_version,
            "hwv": self.hardware_version,
            "swv": self.software_version,
            "protType": self.protocol_type,
            "subProdId": "00",
        }

    def as_dev_info_sync_payload(self) -> dict[str, object]:
        """Build the gateway devInfo shape captured after cloud login."""

        return {
            "sn": self.serial_number,
            "model": self.model,
            "devType": self.device_type,
            "manu": self.manufacturer,
            "prodId": self.product_id,
            "mac": self.mac,
            "hiv": "1.0",
            "fwv": self.firmware_version,
            "hwv": self.hardware_version,
            "swv": self.software_version,
            "protType": self.protocol_type,
            "setupType": 0,
            "subProdId": "00",
        }

def encode_bridge_json(payload: dict[str, object]) -> bytes:
    """Serialize activation JSON with the captured comma spacing."""

    return json.dumps(payload, separators=(", ", ":"), ensure_ascii=False).encode(
        "utf-8"
    )

def encode_bridge_compact_json(payload: object) -> bytes:
    """Serialize compact JSON used by post-activation bridge requests."""

    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )

BRIDGE_GATEWAY_SYNC_SERVICES: tuple[tuple[str, str], ...] = (
    ("netInfo", "netInfo"),
    ("policy", "kitAuth"),
    ("devOta", "update"),
    ("netInfo", "netInfo"),
    ("homeGatewayCap", "homeGatewayCap"),
    ("timer", "timer"),
    ("delay", "delay"),
    ("timeZone", "timeZone"),
)

def build_bridge_dev_info_sync_request(
    *,
    dev_id: str,
    access_token: str,
    device_info: BridgeDeviceInfo,
    token: bytes,
    sequence_number: int,
) -> BridgeCloudRequest:
    """Build the post-login gateway ``.sys/devInfoSync`` request."""

    if not dev_id or not access_token:
        raise CoapTcpProtocolError("bridge devInfoSync requires devId and access token")
    if not 0 <= sequence_number <= 0xFF:
        raise CoapTcpProtocolError("bridge sequence number must fit one byte")
    payload = [
        {
            "devId": dev_id,
            "devInfo": device_info.as_dev_info_sync_payload(),
            "services": [
                {"st": service_type, "sid": service_id}
                for service_type, service_id in BRIDGE_GATEWAY_SYNC_SERVICES
            ],
        }
    ]
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=(".sys", "devInfoSync"),
        sequence=bytes((sequence_number,)),
        payload=encode_bridge_compact_json(payload),
        access_token=access_token,
    )

def build_bridge_activate_request(
    *, code: str, device_info: BridgeDeviceInfo, token: bytes
) -> BridgeCloudRequest:
    if not code:
        raise CoapTcpProtocolError("activation code is required")
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=(".sys", "activate"),
        sequence=b"",
        payload=encode_bridge_json(
            {
                "code": code,
                "devInfo": device_info.as_activation_payload(),
                "snRestrict": {"type": "0"},
            }
        ),
    )

def build_bridge_login_request(
    *, dev_id: str, secret: str, token: bytes
) -> BridgeCloudRequest:
    if not dev_id or not secret:
        raise CoapTcpProtocolError("bridge login requires devId and secret")
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=(".sys", "login"),
        sequence=b"\x01",
        payload=encode_bridge_compact_json(
            {"devId": dev_id, "secret": secret, "powerType": 1, "capSet": "01"}
        ),
    )

def build_bridge_revoke_request(
    *, access_token: str, token: bytes, sequence_number: int
) -> BridgeCloudRequest:
    """Build the observed parent-gateway cloud revocation request."""

    if not access_token:
        raise CoapTcpProtocolError("bridge revoke requires an access token")
    if not 0 <= sequence_number <= 0xFF:
        raise CoapTcpProtocolError("bridge sequence number must fit one byte")
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=(".sys", "revoke"),
        sequence=bytes((sequence_number,)),
        payload=b"{}",
        access_token=access_token,
    )

def build_bridge_grs_request(
    *, dev_id: str, access_token: str, token: bytes, sequence_number: int
) -> BridgeCloudRequest:
    if not dev_id or not access_token:
        raise CoapTcpProtocolError("bridge GRS requires devId and access token")
    if not 0 <= sequence_number <= 0xFF:
        raise CoapTcpProtocolError("bridge sequence number must fit one byte")
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=(".sys", "grs"),
        sequence=bytes((sequence_number,)),
        payload=encode_bridge_compact_json(
            {
                "appName": "smarthome",
                "devId": dev_id,
                "regCountry": "CN",
                "services": [
                    {
                        "serviceName": "com.huawei.cloud.wisedevice",
                        "key": "hilinkDevicePrimaryCloudUrl",
                    },
                    {
                        "serviceName": "com.huawei.cloud.wisedevice",
                        "key": "hilinkDeviceStandbyCloudUrl",
                    },
                    {"serviceName": "com.huawei.cloud.hota", "key": "ROOT"},
                ],
            }
        ),
        access_token=access_token,
    )

def build_bridge_heartbeat_request(
    *,
    access_token: str,
    token: bytes,
    sequence_number: int,
    apply_hb_factor: dict[str, object] | None = None,
) -> BridgeCloudRequest:
    if not access_token:
        raise CoapTcpProtocolError("bridge heartbeat requires an access token")
    if not 0 <= sequence_number <= 0xFF:
        raise CoapTcpProtocolError("bridge sequence number must fit one byte")
    payload = (
        encode_bridge_compact_json({"applyHbFactor": apply_hb_factor})
        if apply_hb_factor is not None
        else b"{}"
    )
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=(".sys", "heartbeat"),
        sequence=bytes((sequence_number,)),
        payload=payload,
        access_token=access_token,
    )

def _append_bridge_option(output: bytearray, previous: int, option: CoapTcpOption) -> None:
    if option.number < previous:
        raise CoapTcpProtocolError("bridge options must be ordered")
    delta_nibble, delta_extension = _encode_extended(option.number - previous)
    length_nibble, length_extension = _encode_extended(len(option.value))
    output.append((delta_nibble << 4) | length_nibble)
    output.extend(delta_extension)
    output.extend(length_extension)
    output.extend(option.value)

def _encode_extended(value: int) -> tuple[int, bytes]:
    if value < 0:
        raise CoapTcpProtocolError("CoAP option values cannot be negative")
    if value < 13:
        return value, b""
    if value < 269:
        return 13, bytes((value - 13,))
    if value < 65805:
        return 14, (value - 269).to_bytes(2, "big")
    raise CoapTcpProtocolError("CoAP option value is too large")

def _decode_extended(nibble: int, packet: bytes, position: int) -> tuple[int, int]:
    if nibble < 13:
        return nibble, position
    if nibble == 13:
        if position >= len(packet):
            raise CoapTcpProtocolError("missing one-byte option extension")
        return packet[position] + 13, position + 1
    if nibble == 14:
        if position + 1 >= len(packet):
            raise CoapTcpProtocolError("missing two-byte option extension")
        return int.from_bytes(packet[position : position + 2], "big") + 269, position + 2
    raise CoapTcpProtocolError("reserved CoAP option nibble")

def _decode_options_and_payload(packet: bytes) -> tuple[list[CoapTcpOption], bytes]:
    options: list[CoapTcpOption] = []
    position = 0
    number = 0
    while position < len(packet):
        if packet[position] == COAP_PAYLOAD_MARKER:
            return options, packet[position + 1 :]
        header = packet[position]
        position += 1
        delta, position = _decode_extended(header >> 4, packet, position)
        length, position = _decode_extended(header & 0x0F, packet, position)
        number += delta
        if position + length > len(packet):
            raise CoapTcpProtocolError("CoAP option exceeds message boundary")
        options.append(CoapTcpOption(number, packet[position : position + length]))
        position += length
    return options, b""

def encode_bridge_message(
    *,
    code: int,
    token: bytes,
    options: tuple[CoapTcpOption, ...],
    payload: bytes,
) -> bytes:
    """Encode a generic bridge CoAP/TCP message."""

    if not 0 <= code <= 0xFF or len(token) > 8:
        raise CoapTcpProtocolError("invalid bridge message header")

    output = bytearray((0x50 | len(token), code))
    output.extend(token)
    previous = 0
    for option in sorted(options, key=lambda item: item.number):
        _append_bridge_option(output, previous, option)
        previous = option.number
    if payload:
        output.append(COAP_PAYLOAD_MARKER)
        output.extend(payload)
    if len(output) > 0xFFFF:
        raise CoapTcpProtocolError("bridge message exceeds its two-byte length")
    return len(output).to_bytes(2, "big") + bytes(output)