"""OneConnect Bridge CoAP 发现."""
from __future__ import annotations

from dataclasses import dataclass
import json

COAP_VERSION = 1
COAP_GET = 0x01
COAP_POST = 0x02
COAP_CONTENT = 0x45
COAP_CONFIRMABLE = 0
COAP_NON_CONFIRMABLE = 1
COAP_ACKNOWLEDGEMENT = 2
URI_PATH = 11
URI_QUERY = 15
PAYLOAD_MARKER = 0xFF

# 公开实现与实机冒烟均用此发现选择符；保留旧 cloudSetup 拼写兼容历史抓包
CLOUD_SETUP_DISCOVERY_VALUES = frozenset(("ohCloudSetup", "cloudSetup"))

class CoapProtocolError(ValueError):
    """Raised when a packet falls outside the small CoAP subset we model."""

@dataclass(frozen=True)
class CoapOption:
    number: int
    value: bytes

    @classmethod
    def text(cls, number: int, value: str) -> "CoapOption":
        return cls(number=number, value=value.encode("utf-8"))

    def as_text(self) -> str:
        return self.value.decode("utf-8")

@dataclass(frozen=True)
class CoapMessage:
    message_type: int
    code: int
    message_id: int
    token: bytes = b""
    options: tuple[CoapOption, ...] = ()
    payload: bytes = b""

    def __post_init__(self) -> None:
        if not 0 <= self.message_type <= 3:
            raise CoapProtocolError("message type must fit the CoAP header")
        if not 0 <= self.code <= 0xFF:
            raise CoapProtocolError("code must fit one byte")
        if not 0 <= self.message_id <= 0xFFFF:
            raise CoapProtocolError("message id must fit two bytes")
        if len(self.token) > 8:
            raise CoapProtocolError("CoAP tokens are at most eight bytes")

    @classmethod
    def decode(cls, packet: bytes) -> "CoapMessage":
        if len(packet) < 4:
            raise CoapProtocolError("packet is shorter than a CoAP header")

        version = packet[0] >> 6
        message_type = (packet[0] >> 4) & 0x03
        token_length = packet[0] & 0x0F
        if version != COAP_VERSION:
            raise CoapProtocolError(f"unsupported CoAP version: {version}")
        if token_length > 8 or len(packet) < 4 + token_length:
            raise CoapProtocolError("invalid CoAP token length")

        index = 4
        token = packet[index : index + token_length]
        index += token_length
        option_number = 0
        options: list[CoapOption] = []
        payload = b""

        while index < len(packet):
            if packet[index] == PAYLOAD_MARKER:
                payload = packet[index + 1 :]
                break
            header = packet[index]
            index += 1
            delta, index = _decode_extended(header >> 4, packet, index)
            length, index = _decode_extended(header & 0x0F, packet, index)
            option_number += delta
            if index + length > len(packet):
                raise CoapProtocolError("option exceeds packet boundary")
            options.append(CoapOption(option_number, packet[index : index + length]))
            index += length

        return cls(
            message_type=message_type,
            code=packet[1],
            message_id=int.from_bytes(packet[2:4], "big"),
            token=token,
            options=tuple(options),
            payload=payload,
        )

    def encode(self) -> bytes:
        ordered_options = tuple(sorted(self.options, key=lambda option: option.number))
        packet = bytearray(
            [
                (COAP_VERSION << 6) | (self.message_type << 4) | len(self.token),
                self.code,
            ]
        )
        packet.extend(self.message_id.to_bytes(2, "big"))
        packet.extend(self.token)

        previous_number = 0
        for option in ordered_options:
            if option.number < previous_number:
                raise CoapProtocolError("options must be in ascending numeric order")
            delta = option.number - previous_number
            delta_nibble, delta_extra = _encode_extended(delta)
            length_nibble, length_extra = _encode_extended(len(option.value))
            packet.append((delta_nibble << 4) | length_nibble)
            packet.extend(delta_extra)
            packet.extend(length_extra)
            packet.extend(option.value)
            previous_number = option.number

        if self.payload:
            packet.append(PAYLOAD_MARKER)
            packet.extend(self.payload)
        return bytes(packet)

    @property
    def path(self) -> str:
        return "/".join(option.as_text() for option in self.options if option.number == URI_PATH)

    @property
    def query(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for option in self.options:
            if option.number != URI_QUERY:
                continue
            key, separator, value = option.as_text().partition("=")
            result[key] = value if separator else ""
        return result

@dataclass(frozen=True)
class ServiceDescriptor:
    service_type: str
    service_id: str

    def as_payload(self) -> dict[str, str]:
        return {"st": self.service_type, "sid": self.service_id}

@dataclass(frozen=True)
class DeviceDescriptor:
    serial_number: str
    device_type: str
    manufacturer: str
    product_id: str
    protocol_type: int
    services: tuple[ServiceDescriptor, ...]

    def unbound_cloud_setup_payload(self) -> dict[str, object]:
        """Return the verified response shape captured before cloud binding."""

        return {
            "errcode": 0,
            "devId": "",
            "devInfo": {
                "sn": self.serial_number,
                "devType": self.device_type,
                "manu": self.manufacturer,
                "prodId": self.product_id,
                "protType": self.protocol_type,
            },
            "services": [service.as_payload() for service in self.services],
        }

    def unbound_cloud_setup_response(
        self, request: CoapMessage, response_message_id: int
    ) -> CoapMessage:
        if not is_cloud_setup_request(request):
            raise CoapProtocolError("request is not a cloudSetup discovery request")
        if request.message_type != COAP_NON_CONFIRMABLE:
            raise NotImplementedError("only the observed non-confirmable discovery request is modelled")
        payload = self._observed_cloud_setup_json().encode("utf-8")
        # 实测应答 message id 与请求不同且分配策略未知，由调用方提供
        return CoapMessage(
            message_type=COAP_NON_CONFIRMABLE,
            code=COAP_CONTENT,
            message_id=response_message_id,
            token=request.token,
            payload=payload,
        )

    def _observed_cloud_setup_json(self) -> str:
        """按抓包实锤序列化 JSON 空白：devInfo/services 逗号后含一个空格（265 字节载荷的兼容事实，非排版偏好)."""

        payload = self.unbound_cloud_setup_payload()
        device_info = json.dumps(payload["devInfo"], separators=(",", ":"), ensure_ascii=False)
        services = json.dumps(payload["services"], separators=(",", ":"), ensure_ascii=False)
        return f'{{"errcode":0,"devId":"","devInfo":{device_info}, "services":{services}}}'

def is_cloud_setup_request(message: CoapMessage) -> bool:
    return (
        message.code == COAP_GET
        and message.path == ".well-known/core"
        and message.query.get("st") in CLOUD_SETUP_DISCOVERY_VALUES
    )

def _decode_extended(nibble: int, packet: bytes, index: int) -> tuple[int, int]:
    if nibble < 13:
        return nibble, index
    if nibble == 13:
        if index >= len(packet):
            raise CoapProtocolError("missing one-byte extended option value")
        return packet[index] + 13, index + 1
    if nibble == 14:
        if index + 1 >= len(packet):
            raise CoapProtocolError("missing two-byte extended option value")
        return int.from_bytes(packet[index : index + 2], "big") + 269, index + 2
    raise CoapProtocolError("reserved option nibble")

def _encode_extended(value: int) -> tuple[int, bytes]:
    if value < 0:
        raise CoapProtocolError("option values cannot be negative")
    if value < 13:
        return value, b""
    if value < 269:
        return 13, bytes([value - 13])
    if value < 65805:
        return 14, (value - 269).to_bytes(2, "big")
    raise CoapProtocolError("option value exceeds the supported CoAP subset")