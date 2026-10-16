"""OneConnect Bridge 子设备云协议载荷."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from ..core.m2m import (
    ACCESS_TOKEN_ID,
    BRIDGE_COAP_CONTENT,
    COAP_POST,
    DEVICE_ID,
    REQUEST_ID,
    SEQUENCE_NUMBER,
    URI_PATH,
    BridgeCloudMessage,
    BridgeCloudRequest,
    CoapTcpOption,
    encode_bridge_compact_json,
)
from .models import ChildPlan

class ChildProtocolError(ValueError):
    """Raised when a child payload is outside the observed contract."""

@dataclass(frozen=True)
class ChildIdentity:
    serial_number: str
    udid: str
    product_id: str
    sub_product_id: str

    @property
    def mac(self) -> str:
        return self.serial_number[:12]

    def as_dev_info(
        self,
        plan: ChildPlan,
        *,
        firmware_version: str,
        hardware_version: str,
        software_version: str,
        include_device_name: bool = False,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "sn": self.serial_number,
            "udid": self.udid,
            "subProdId": self.sub_product_id,
            "model": plan.product.model,
            "devType": plan.product.device_type,
            "manu": plan.product.manufacturer,
            "prodId": self.product_id,
            "mac": self.mac,
            "hiv": "1.0",
            "fwv": firmware_version,
            "hwv": hardware_version,
            "swv": software_version,
        }
        if include_device_name:
            value["deviceName"] = plan.friendly_name or plan.product.device_name
        value["protType"] = plan.product.protocol_type
        value["setupType"] = plan.product.setup_type
        return value

@dataclass(frozen=True)
class ChildMaterial:
    dev_id: str
    auth_code: str
    auth_code_id: str
    timeout: int
    psk: str
    secret: str
    serial_number: str
    udid: str

    def as_storage(self, *, product_id: str, sub_product_id: str) -> dict[str, object]:
        return {
            "schema": "oneconnect_bridge.child-material.v1",
            "product_id": product_id,
            "sub_product_id": sub_product_id,
            "devId": self.dev_id,
            "authCode": self.auth_code,
            "authCodeId": self.auth_code_id,
            "timeout": self.timeout,
            "psk": self.psk,
            "secret": self.secret,
            "serial_number": self.serial_number,
            "udid": self.udid,
        }

@dataclass(frozen=True)
class ChildCommand:
    dev_id: str
    service_id: str
    request_id: bytes
    context: bytes
    payload: dict[str, object]
    message: BridgeCloudMessage

def decode_child_command(
    message: BridgeCloudMessage, *, known_dev_ids: set[str]
) -> ChildCommand | None:
    """Decode a child M2M command, returning None for parent messages."""

    paths = [
        option.value.decode("utf-8", errors="strict")
        for option in message.options
        if option.number == URI_PATH
    ]
    if len(paths) != 2 or paths[0] not in known_dev_ids:
        return None
    option_values = {option.number: option.value for option in message.options}
    request_id = option_values.get(REQUEST_ID)
    device_id = option_values.get(DEVICE_ID)
    context = option_values.get(SEQUENCE_NUMBER - 1)
    if request_id is None or device_id is None or context is None:
        raise ChildProtocolError("child command is missing context options")
    if device_id.decode("utf-8", errors="strict") != paths[0]:
        raise ChildProtocolError("child command device id does not match URI path")
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChildProtocolError("child command payload is not JSON") from error
    if not isinstance(payload, dict):
        raise ChildProtocolError("child command payload is not an object")
    return ChildCommand(
        dev_id=paths[0],
        service_id=paths[1],
        request_id=request_id,
        context=context,
        payload=payload,
        message=message,
    )

def build_child_command_response(
    *, command: ChildCommand, access_token: str, sequence: int
) -> bytes:
    """Build the observed 2.05 child command ACK."""

    options = (
        CoapTcpOption.text(ACCESS_TOKEN_ID, access_token),
        CoapTcpOption(REQUEST_ID, command.request_id),
        CoapTcpOption(DEVICE_ID, command.dev_id.encode("utf-8")),
        CoapTcpOption(SEQUENCE_NUMBER - 1, command.context),
        CoapTcpOption(SEQUENCE_NUMBER, bytes((sequence,))),
    )
    output = bytearray((0x58, BRIDGE_COAP_CONTENT))
    output.extend(command.message.token)
    previous = 0
    for option in sorted(options, key=lambda item: item.number):
        delta_nibble, delta_extension = _encode_extended(option.number - previous)
        length_nibble, length_extension = _encode_extended(len(option.value))
        output.append((delta_nibble << 4) | length_nibble)
        output.extend(delta_extension)
        output.extend(length_extension)
        output.extend(option.value)
        previous = option.number
    payload = encode_bridge_compact_json(
        {"errcode": 0, "devicedelay": 0, "devicectltype": 0}
    )
    output.append(0xFF)
    output.extend(payload)
    return len(output).to_bytes(2, "big") + output

def _encode_extended(value: int) -> tuple[int, bytes]:
    if value < 0:
        raise ChildProtocolError("child ACK option value cannot be negative")
    if value < 13:
        return value, b""
    if value < 269:
        return 13, bytes((value - 13,))
    if value < 65805:
        return 14, (value - 269).to_bytes(2, "big")
    raise ChildProtocolError("child ACK option value is too large")

def build_batch_bind_request(
    *, access_token: str, token: bytes, identity: ChildIdentity, plan: ChildPlan,
    firmware_version: str, hardware_version: str, software_version: str,
) -> BridgeCloudRequest:
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=("device", "v3", "batchBind"),
        sequence=b"",
        access_token=access_token,
        payload=encode_bridge_compact_json(
            [{
                "devInfo": identity.as_dev_info(
                    plan,
                    firmware_version=firmware_version,
                    hardware_version=hardware_version,
                    software_version=software_version,
                    include_device_name=True,
                )
            }]
        ),
    )

def build_dev_info_sync_request(
    *, access_token: str, token: bytes, identity: ChildIdentity,
    material: ChildMaterial, plan: ChildPlan,
    firmware_version: str, hardware_version: str, software_version: str,
) -> BridgeCloudRequest:
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=(".sys", "devInfoSync"),
        sequence=b"",
        access_token=access_token,
        payload=encode_bridge_compact_json(
            [{
                "devId": material.dev_id,
                "devInfo": identity.as_dev_info(
                    plan,
                    firmware_version=firmware_version,
                    hardware_version=hardware_version,
                    software_version=software_version,
                ),
                "services": [],
            }]
        ),
    )

def build_status_sync_request(
    *, access_token: str, token: bytes, material: ChildMaterial,
) -> BridgeCloudRequest:
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=("device", "v3", "statusSync"),
        sequence=b"",
        access_token=access_token,
        payload=encode_bridge_compact_json(
            [{"devId": material.dev_id, "status": "online"}]
        ),
    )

def build_room_update_request(
    *, access_token: str, token: bytes, material: ChildMaterial, room: str,
) -> BridgeCloudRequest:
    """Build the observed roomUpdated event emitted after child binding."""

    if not room:
        raise ChildProtocolError("room update requires a non-empty room name")
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        # 上游 1013 实锤：roomUpdated 事件走 .sys/deviceEvent
        paths=(".sys", "deviceEvent"),
        sequence=b"",
        access_token=access_token,
        payload=encode_bridge_compact_json(
            {
                "header": {
                    "category": "room",
                    "subCategory": "roomUpdated",
                },
                "body": {
                    "rooms": [
                        {
                            "name": room,
                            "members": [{"devId": material.dev_id}],
                        }
                    ]
                },
            }
        ),
    )

def build_batch_delete_request(
    *, access_token: str, token: bytes, material: ChildMaterial,
) -> BridgeCloudRequest:
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=(".sys", "batchDelDevices"),
        sequence=b"",
        access_token=access_token,
        payload=encode_bridge_compact_json({"devIds": [material.dev_id]}),
    )

def build_data_sync_request(
    *, access_token: str, token: bytes, child: ChildPlan,
    material: ChildMaterial, service_id: str, data: dict[str, object],
    sequence: bytes,
) -> BridgeCloudRequest:
    if service_id not in {service.service_id for service in child.services}:
        raise ChildProtocolError(f"service is not selected for child: {service_id}")
    return BridgeCloudRequest(
        code=COAP_POST,
        token=token,
        paths=("device", "v3", "dataSync"),
        sequence=sequence,
        access_token=access_token,
        payload=encode_bridge_compact_json(
            [{
                "devId": material.dev_id,
                "sn": material.serial_number,
                "services": [{"sid": service_id, "data": data}],
            }]
        ),
    )

def parse_batch_bind_response(
    payload: dict[str, object], *, identity: ChildIdentity,
) -> ChildMaterial:
    if payload.get("errcode") != 0:
        raise ChildProtocolError(f"batchBind rejected: {payload.get('errcode')!r}")
    failures = payload.get("failureList")
    successes = payload.get("successList")
    if not isinstance(failures, list) or failures:
        # failureList 内容是云端拒绝原因的唯一线索，必须完整带入异常消息
        raise ChildProtocolError(f"batchBind returned a failureList: {failures!r}")
    if not isinstance(successes, list) or len(successes) != 1:
        raise ChildProtocolError("batchBind did not return one success item")
    item = successes[0]
    if not isinstance(item, dict):
        raise ChildProtocolError("batchBind success item is not an object")
    if any(not isinstance(item.get(key), str) or not item[key] for key in (
        "devId", "psk", "secret", "sn", "udid"
    )):
        raise ChildProtocolError("batchBind success item is missing identity material")
    if item["sn"] != identity.serial_number or item["udid"] != identity.udid:
        raise ChildProtocolError("batchBind returned material for another child")
    info = item.get("authCodeInfo")
    if not isinstance(info, dict):
        raise ChildProtocolError("batchBind authCodeInfo is missing")
    if not isinstance(info.get("authCode"), str) or not isinstance(
        info.get("authCodeId"), str
    ):
        raise ChildProtocolError("batchBind authCodeInfo is incomplete")
    timeout = info.get("timeout")
    if not isinstance(timeout, int) or timeout <= 0:
        raise ChildProtocolError("batchBind authCodeInfo timeout is invalid")
    return ChildMaterial(
        dev_id=item["devId"],
        auth_code=info["authCode"],
        auth_code_id=info["authCodeId"],
        timeout=timeout,
        psk=item["psk"],
        secret=item["secret"],
        serial_number=item["sn"],
        udid=item["udid"],
    )

def material_from_storage(value: dict[str, Any]) -> ChildMaterial:
    try:
        material = ChildMaterial(
            dev_id=str(value["devId"]),
            auth_code=str(value["authCode"]),
            auth_code_id=str(value["authCodeId"]),
            timeout=int(value["timeout"]),
            psk=str(value["psk"]),
            secret=str(value["secret"]),
            serial_number=str(value["serial_number"]),
            udid=str(value["udid"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ChildProtocolError("stored child material is incomplete") from error
    if not material.dev_id or not material.serial_number or not material.udid:
        raise ChildProtocolError("stored child material has an empty identity")
    return material