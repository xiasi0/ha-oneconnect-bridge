"""OneConnect Bridge 局域网配对应答."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable

from .coap import (
    COAP_ACKNOWLEDGEMENT,
    COAP_CONFIRMABLE,
    COAP_CONTENT,
    COAP_POST,
    CoapMessage,
    CoapProtocolError,
    DeviceDescriptor,
    is_cloud_setup_request,
)
from .speke import (
    CloudSetupAuthInfo,
    CloudSetupBindingInfo,
    CloudSetupCodec,
    CloudSetupProtocolError,
    SpekeProtocolError,
    SpekeServer,
    decode_cloud_setup_binding_info,
    decode_cloud_setup_auth_info,
)

CloudSetupRecord = Callable[[CloudSetupBindingInfo | CloudSetupAuthInfo, bytes, bytes], None]

@dataclass(frozen=True)
class PairingResult:
    """Payload-free audit record for a local pairing datagram."""

    kind: str
    packet_length: int
    resource: str | None = None
    peer_host: str | None = None
    wire_format: str | None = None
    failure_stage: str | None = None
    decoded_payload_kind: str | None = None
    decoded_root_keys: tuple[str, ...] | None = None
    decoded_data_keys: tuple[str, ...] | None = None

def _summarize_cloud_setup_shape(
    plaintext: bytes,
) -> tuple[str, tuple[str, ...] | None, tuple[str, ...] | None]:
    """Return only JSON structure, never a pairing value or credential."""

    try:
        root = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "not_json", None, None
    if not isinstance(root, dict):
        return type(root).__name__, None, None
    data = root.get("data")
    data_keys = tuple(sorted(data)) if isinstance(data, dict) else None
    return "object", tuple(sorted(root)), data_keys

class LocalPairingResponder:
    """应答实测的本地 HiLink 配对资源."""

    def __init__(
        self,
        descriptor: DeviceDescriptor,
        pin: str | bytes,
        discovery_response_message_id: Callable[[], int],
        on_cloud_setup_record: CloudSetupRecord | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._pin = pin
        self._discovery_response_message_id = discovery_response_message_id
        self._on_cloud_setup_record = on_cloud_setup_record
        self._sessions: dict[str, SpekeServer] = {}

    @property
    def has_active_session(self) -> bool:
        """App 已开始 SPEKE 即视为配对进行中（PIN 已验证，CloudSetup 在路上）."""
        return bool(self._sessions)

    def handle(self, packet: bytes, peer_host: str) -> tuple[bytes | None, PairingResult]:
        """Process one CoAP datagram without exposing its authenticated body."""

        try:
            request = CoapMessage.decode(packet)
        except CoapProtocolError:
            return None, PairingResult(kind="invalid_coap", packet_length=len(packet))

        if is_cloud_setup_request(request):
            response = self._descriptor.unbound_cloud_setup_response(
                request,
                response_message_id=self._discovery_response_message_id(),
            )
            return response.encode(), PairingResult(
                kind="cloud_setup_discovery_response",
                packet_length=len(packet),
                resource=request.path,
                peer_host=peer_host,
            )

        if request.message_type != COAP_CONFIRMABLE or request.code != COAP_POST:
            return None, PairingResult(
                kind="unsupported_request",
                packet_length=len(packet),
                resource=request.path,
                peer_host=peer_host,
            )

        if request.path == "SPEKE/v2":
            return self._handle_speke(request, peer_host)
        if request.path == "CloudSetup/v2":
            return self._handle_cloud_setup(request, peer_host)
        return None, PairingResult(
            kind="unsupported_request",
            packet_length=len(packet),
            resource=request.path,
            peer_host=peer_host,
        )

    def _handle_speke(
        self, request: CoapMessage, peer_host: str
    ) -> tuple[bytes | None, PairingResult]:
        session = self._sessions.get(peer_host)
        if session is None:
            session = SpekeServer(self._pin)
            self._sessions[peer_host] = session
        try:
            payload = session.process(request.payload)
        except SpekeProtocolError:
            return None, PairingResult(
                kind="invalid_speke",
                packet_length=len(request.encode()),
                resource=request.path,
                peer_host=peer_host,
            )
        return self._ack(request, payload), PairingResult(
            kind="speke_response",
            packet_length=len(request.encode()),
            resource=request.path,
            peer_host=peer_host,
        )

    def _handle_cloud_setup(
        self, request: CoapMessage, peer_host: str
    ) -> tuple[bytes | None, PairingResult]:
        session = self._sessions.get(peer_host)
        if session is None or not session.established:
            return None, PairingResult(
                kind="cloud_setup_before_speke",
                packet_length=len(request.encode()),
                resource=request.path,
                peer_host=peer_host,
            )
        codec = CloudSetupCodec(session.data_key)
        if request.payload[:1] == b"\x00":
            decrypt = codec.decrypt_raw
            encrypt = codec.encrypt_raw
            wire_format = "raw_aes_gcm"
        else:
            decrypt = codec.decrypt
            encrypt = codec.encrypt
            wire_format = "base64_aes_gcm"
        try:
            plaintext = decrypt(request.payload)
        except CloudSetupProtocolError:
            return None, PairingResult(
                kind="invalid_cloud_setup",
                packet_length=len(request.encode()),
                resource=request.path,
                peer_host=peer_host,
                wire_format=wire_format,
                failure_stage="decrypt_rejected",
            )
        try:
            try:
                accepted_info = decode_cloud_setup_binding_info(plaintext)
            except CloudSetupProtocolError:
                accepted_info = decode_cloud_setup_auth_info(plaintext)
        except CloudSetupProtocolError:
            # 官方 SDK 对此认证明文实测回加密 {"errcode":-1}；其余畸形不归并到该错误
            if plaintext == b'{"data":{}}':
                return self._ack(request, encrypt(b'{"errcode":-1}')), PairingResult(
                    kind="known_invalid_cloud_setup",
                    packet_length=len(request.encode()),
                    resource=request.path,
                    peer_host=peer_host,
                    wire_format=wire_format,
                )
            payload_kind, root_keys, data_keys = _summarize_cloud_setup_shape(plaintext)
            return None, PairingResult(
                kind="invalid_cloud_setup",
                packet_length=len(request.encode()),
                resource=request.path,
                peer_host=peer_host,
                wire_format=wire_format,
                failure_stage="auth_info_schema_rejected",
                decoded_payload_kind=payload_kind,
                decoded_root_keys=root_keys,
                decoded_data_keys=data_keys,
            )
        if self._on_cloud_setup_record is not None:
            self._on_cloud_setup_record(accepted_info, request.payload, plaintext)
        return self._ack(request, encrypt(b'{"errcode":0}')), PairingResult(
            kind="cloud_setup_accepted",
            packet_length=len(request.encode()),
            resource=request.path,
            peer_host=peer_host,
            wire_format=wire_format,
        )

    @staticmethod
    def _ack(request: CoapMessage, payload: bytes) -> bytes:
        """Build the observed ACK/2.05 response framing for a POST request."""

        return CoapMessage(
            message_type=COAP_ACKNOWLEDGEMENT,
            code=COAP_CONTENT,
            message_id=request.message_id,
            token=request.token,
            payload=payload,
        ).encode()