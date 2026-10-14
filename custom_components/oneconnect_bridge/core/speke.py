"""OneConnect Bridge SPEKE 与 CloudSetup/v2 原语."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import secrets
from typing import Callable, Mapping

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - exercised only without cryptography
    AESGCM = None
    InvalidTag = ValueError

RandomBytes = Callable[[int], bytes]

SPEKE_VERSION = "1.0.0"
SPEKE_OPERATION_CODE = 6
SPEKE_CLIENT_REQUEST = 0x0001
SPEKE_SERVER_RESPONSE = 0x8001
SPEKE_CLIENT_CONFIRM = 0x0002
SPEKE_SERVER_CONFIRM = 0x8002

SESSION_ID_LENGTH = 16
SALT_LENGTH = 16
CHALLENGE_LENGTH = 16
HMAC_LENGTH = 32
SESSION_KEY_LENGTH = 16
MODP_2048_PUBLIC_KEY_LENGTH = 256
MODP_2048_PRIVATE_RANDOM_LENGTH = 24
GCM_VERSION = 0
GCM_IV_LENGTH = 12
GCM_TAG_LENGTH = 16

_BASE_INFO = b"hichain_speke_base_info"
_SESSION_KEY_INFO = b"hichain_speke_sessionkey_info"
_DATA_KEY_INFO = b"hichain_return_key"

# RFC 3526 group 14：公开实现 support256mod 模式所选的同一 2048 位素数
_MODP_2048_PRIME = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF",
    16,
)

class SpekeProtocolError(ValueError):
    """A received local-pairing message violates the verified contract."""

class CloudSetupProtocolError(ValueError):
    """A CloudSetup/v2 payload violates the verified transport contract."""

def _secure_random(length: int) -> bytes:
    return secrets.token_bytes(length)

def _take_random(source: RandomBytes, length: int) -> bytes:
    value = source(length)
    if not isinstance(value, bytes) or len(value) != length:
        raise SpekeProtocolError(f"random source must return exactly {length} bytes")
    return value

def _pin_bytes(pin: str | bytes) -> bytes:
    value = pin.encode("ascii") if isinstance(pin, str) else pin
    if not isinstance(value, bytes) or not value or not value.isascii():
        raise SpekeProtocolError("PIN must be a non-empty ASCII string or bytes")
    return value

def _compact_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def _load_json(payload: bytes | str) -> dict[str, object]:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not isinstance(payload, bytes):
        raise SpekeProtocolError("SPEKE payload must be bytes or text")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SpekeProtocolError("SPEKE payload is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise SpekeProtocolError("SPEKE root must be a JSON object")
    return value

def _read_envelope(payload: bytes | str) -> tuple[str, int, dict[str, object]]:
    root = _load_json(payload)
    session_id = root.get("sessionId")
    security_data = root.get("securityData")
    if not isinstance(session_id, str) or not session_id:
        raise SpekeProtocolError("SPEKE sessionId is missing")
    if not isinstance(security_data, dict):
        raise SpekeProtocolError("SPEKE securityData is missing")
    message = security_data.get("message")
    inner = security_data.get("payload")
    if not isinstance(message, int) or not isinstance(inner, dict):
        raise SpekeProtocolError("SPEKE message or payload is invalid")
    return session_id, message, inner

def _make_envelope(session_id: str, message: int, payload: Mapping[str, object]) -> bytes:
    return _compact_json(
        {
            "sessionId": session_id,
            "securityData": {
                "message": message,
                "payload": dict(payload),
            },
        }
    )

def _version_payload() -> dict[str, str]:
    return {
        "currentVersion": SPEKE_VERSION,
        "minVersion": SPEKE_VERSION,
    }

def _require_version(payload: Mapping[str, object]) -> None:
    version = payload.get("version")
    if not isinstance(version, dict) or version.get("currentVersion") != SPEKE_VERSION:
        raise SpekeProtocolError("unsupported SPEKE currentVersion")

def _decode_hex(value: object, name: str, expected_length: int | None = None) -> bytes:
    if not isinstance(value, str) or not value:
        raise SpekeProtocolError(f"SPEKE {name} is missing")
    try:
        result = bytes.fromhex(value)
    except ValueError as error:
        raise SpekeProtocolError(f"SPEKE {name} is not hexadecimal") from error
    if expected_length is not None and len(result) != expected_length:
        raise SpekeProtocolError(
            f"SPEKE {name} must be {expected_length} bytes, got {len(result)}"
        )
    return result

def _hex(value: bytes) -> str:
    return value.hex().upper()

def _hkdf_sha256(*, salt: bytes, info: bytes, material: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-SHA256, matching the public HiLink key schedule."""

    if length <= 0 or length > 255 * hashlib.sha256().digest_size:
        raise SpekeProtocolError("invalid HKDF output length")
    prk = hmac.digest(salt, material, "sha256")
    output = bytearray()
    block = b""
    for counter in range(1, (length + 31) // 32 + 1):
        block = hmac.digest(prk, block + info + bytes([counter]), "sha256")
        output.extend(block)
    return bytes(output[:length])

@dataclass
class _SpekeContext:
    salt: bytes = field(repr=False)
    local_challenge: bytes = field(repr=False)
    private_exponent: int = field(repr=False)
    public_key: bytes = field(repr=False)
    remote_challenge: bytes | None = field(default=None, repr=False)
    identity_key: bytes | None = field(default=None, repr=False)
    hmac_key: bytes | None = field(default=None, repr=False)
    data_key: bytes | None = field(default=None, repr=False)

def _create_context(pin: bytes, salt: bytes, random_bytes: RandomBytes) -> _SpekeContext:
    if len(salt) != SALT_LENGTH:
        raise SpekeProtocolError("SPEKE salt must be 16 bytes")
    secret = _hkdf_sha256(salt=salt, info=_BASE_INFO, material=pin, length=32)
    generator = pow(int.from_bytes(secret, "big"), 2, _MODP_2048_PRIME)
    local_challenge = _take_random(random_bytes, CHALLENGE_LENGTH)
    private_exponent = int.from_bytes(
        _take_random(random_bytes, MODP_2048_PRIVATE_RANDOM_LENGTH), "big"
    )
    public_value = pow(generator, private_exponent, _MODP_2048_PRIME)
    return _SpekeContext(
        salt=salt,
        local_challenge=local_challenge,
        private_exponent=private_exponent,
        public_key=public_value.to_bytes(MODP_2048_PUBLIC_KEY_LENGTH, "big"),
    )

def _derive_session_keys(context: _SpekeContext, remote_public_key: bytes) -> None:
    if len(remote_public_key) != MODP_2048_PUBLIC_KEY_LENGTH:
        raise SpekeProtocolError("unobserved SPEKE public-key length")
    remote_value = int.from_bytes(remote_public_key, "big")
    if not 2 <= remote_value <= _MODP_2048_PRIME - 2:
        raise SpekeProtocolError("SPEKE peer public key is outside the accepted range")
    shared_key = pow(remote_value, context.private_exponent, _MODP_2048_PRIME).to_bytes(
        MODP_2048_PUBLIC_KEY_LENGTH, "big"
    )
    session_material = _hkdf_sha256(
        salt=context.salt,
        info=_SESSION_KEY_INFO,
        material=shared_key,
        length=SESSION_KEY_LENGTH * 2,
    )
    context.identity_key = session_material[:SESSION_KEY_LENGTH]
    context.hmac_key = session_material[SESSION_KEY_LENGTH:]

def _derive_data_key(context: _SpekeContext) -> bytes:
    if context.identity_key is None:
        raise SpekeProtocolError("SPEKE session key is unavailable")
    context.data_key = _hkdf_sha256(
        salt=context.salt,
        info=_DATA_KEY_INFO,
        material=context.identity_key,
        length=SESSION_KEY_LENGTH,
    )
    return context.data_key

def _key_confirmation(context: _SpekeContext, first: bytes, second: bytes) -> bytes:
    if context.hmac_key is None:
        raise SpekeProtocolError("SPEKE HMAC key is unavailable")
    return hmac.digest(context.hmac_key, first + second, "sha256")

class SpekeServer:
    """The locally discovered HiLink gateway side of the observed SPEKE flow."""

    def __init__(self, pin: str | bytes, *, random_bytes: RandomBytes = _secure_random) -> None:
        self._pin = _pin_bytes(pin)
        self._random_bytes = random_bytes
        self._context = _create_context(
            self._pin,
            _take_random(random_bytes, SALT_LENGTH),
            random_bytes,
        )
        self._session_id: str | None = None
        self._confirmed = False

    @property
    def established(self) -> bool:
        return self._confirmed

    @property
    def data_key(self) -> bytes:
        if not self._confirmed or self._context.data_key is None:
            raise SpekeProtocolError("SPEKE session is not confirmed")
        return self._context.data_key

    def process(self, payload: bytes | str) -> bytes:
        session_id, message, inner = _read_envelope(payload)
        if message == SPEKE_CLIENT_REQUEST:
            return self._process_request(session_id, inner)
        if message == SPEKE_CLIENT_CONFIRM:
            return self._process_confirmation(session_id, inner)
        raise SpekeProtocolError(f"unsupported gateway SPEKE message: {message}")

    def _process_request(self, session_id: str, inner: Mapping[str, object]) -> bytes:
        if self._session_id is not None and session_id != self._session_id:
            raise SpekeProtocolError("SPEKE sessionId changed during negotiation")
        if inner.get("operationCode") != SPEKE_OPERATION_CODE:
            raise SpekeProtocolError("unsupported SPEKE operationCode")
        _require_version(inner)
        self._session_id = session_id
        return _make_envelope(
            session_id,
            SPEKE_SERVER_RESPONSE,
            {
                "version": _version_payload(),
                "challenge": _hex(self._context.local_challenge),
                "salt": _hex(self._context.salt),
                "epk": _hex(self._context.public_key),
            },
        )

    def _process_confirmation(self, session_id: str, inner: Mapping[str, object]) -> bytes:
        if self._session_id is None or session_id != self._session_id:
            raise SpekeProtocolError("SPEKE confirmation does not match the active session")
        remote_challenge = _decode_hex(inner.get("challenge"), "challenge", CHALLENGE_LENGTH)
        remote_public_key = _decode_hex(
            inner.get("epk"), "epk", MODP_2048_PUBLIC_KEY_LENGTH
        )
        remote_confirmation = _decode_hex(inner.get("kcfData"), "kcfData", HMAC_LENGTH)
        self._context.remote_challenge = remote_challenge
        _derive_session_keys(self._context, remote_public_key)
        expected = _key_confirmation(
            self._context, remote_challenge, self._context.local_challenge
        )
        if not hmac.compare_digest(remote_confirmation, expected):
            raise SpekeProtocolError("SPEKE client key confirmation failed")
        server_confirmation = _key_confirmation(
            self._context, self._context.local_challenge, remote_challenge
        )
        _derive_data_key(self._context)
        self._confirmed = True
        return _make_envelope(
            session_id,
            SPEKE_SERVER_CONFIRM,
            {"kcfData": _hex(server_confirmation)},
        )

class CloudSetupCodec:
    """CloudSetup/v2 AES-GCM 载荷编解码器."""

    def __init__(self, data_key: bytes, *, random_bytes: RandomBytes = _secure_random) -> None:
        if len(data_key) != SESSION_KEY_LENGTH:
            raise CloudSetupProtocolError("CloudSetup data key must be 16 bytes")
        self._data_key = data_key
        self._random_bytes = random_bytes

    def decrypt(self, payload: bytes | str) -> bytes:
        """Decrypt the Base64 envelope accepted by the official bridge."""

        if AESGCM is None:
            raise RuntimeError("CloudSetup requires the cryptography package")
        encoded = payload.encode("ascii") if isinstance(payload, str) else payload
        if not isinstance(encoded, bytes):
            raise CloudSetupProtocolError("CloudSetup payload must be bytes or text")
        try:
            frame = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError) as error:
            raise CloudSetupProtocolError("CloudSetup payload is not Base64") from error
        return self._decrypt_frame(frame)

    def decrypt_raw(self, frame: bytes) -> bytes:
        """Decrypt the raw frame used by the public LAN implementation."""

        if AESGCM is None:
            raise RuntimeError("CloudSetup requires the cryptography package")
        if not isinstance(frame, bytes):
            raise CloudSetupProtocolError("CloudSetup raw payload must be bytes")
        return self._decrypt_frame(frame)

    def _decrypt_frame(self, frame: bytes) -> bytes:
        minimum = 1 + GCM_IV_LENGTH + GCM_TAG_LENGTH
        if len(frame) <= minimum or frame[0] != GCM_VERSION:
            raise CloudSetupProtocolError("unsupported CloudSetup AES-GCM frame")
        try:
            return AESGCM(self._data_key).decrypt(
                frame[1 : 1 + GCM_IV_LENGTH],
                frame[1 + GCM_IV_LENGTH :],
                None,
            )
        except InvalidTag as error:
            raise CloudSetupProtocolError("CloudSetup AES-GCM authentication failed") from error

    def encrypt(self, plaintext: bytes | str) -> bytes:
        """Encrypt as the Base64 envelope accepted by the official bridge."""

        return base64.b64encode(self._encrypt_frame(plaintext))

    def encrypt_raw(self, plaintext: bytes | str) -> bytes:
        """Encrypt as the public implementation's raw LAN AES-GCM frame."""

        return self._encrypt_frame(plaintext)

    def _encrypt_frame(self, plaintext: bytes | str) -> bytes:
        if AESGCM is None:
            raise RuntimeError("CloudSetup requires the cryptography package")
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        if not isinstance(data, bytes) or not data:
            raise CloudSetupProtocolError("CloudSetup plaintext must be non-empty bytes or text")
        nonce = _take_random(self._random_bytes, GCM_IV_LENGTH)
        encrypted = AESGCM(self._data_key).encrypt(nonce, data, None)
        return bytes([GCM_VERSION]) + nonce + encrypted

@dataclass(frozen=True)
class CloudSetupAuthInfo:
    """The CloudSetup fields accepted by the public device auth-info handler."""

    dev_id: str
    auth_code: bytes = field(repr=False)
    auth_code_id: str
    uid_hash: str | None = None

@dataclass(frozen=True)
class CloudSetupBindingInfo:
    """Wi-Fi/CloudSetup registration fields used by the public SDK."""

    dev_id: str
    psk: bytes = field(repr=False)
    code: str = field(repr=False)
    cloud_primary_url: str = field(repr=False)
    cloud_standby_url: str | None = field(default=None, repr=False)

def _cloud_setup_data(plaintext: bytes | str) -> dict[str, object]:
    data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    if not isinstance(data, bytes):
        raise CloudSetupProtocolError("CloudSetup plaintext must be bytes or text")
    try:
        root = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CloudSetupProtocolError("CloudSetup plaintext is not valid UTF-8 JSON") from error
    if not isinstance(root, dict) or not isinstance(root.get("data"), dict):
        raise CloudSetupProtocolError("CloudSetup payload is missing data")
    return root["data"]

def decode_cloud_setup_binding_info(plaintext: bytes | str) -> CloudSetupBindingInfo:
    """解析实测的 CloudSetup/v2 Wi-Fi 绑定对象."""

    binding = _cloud_setup_data(plaintext)
    dev_id = binding.get("devId")
    psk_hex = binding.get("psk")
    code = binding.get("code")
    primary_url = binding.get("cloudPrimaryUrl")
    standby_url = binding.get("cloudStandbyUrl")
    if not isinstance(dev_id, str) or not dev_id:
        raise CloudSetupProtocolError("CloudSetup data.devId is missing")
    if not isinstance(psk_hex, str):
        raise CloudSetupProtocolError("CloudSetup data.psk is missing")
    try:
        psk = bytes.fromhex(psk_hex)
    except ValueError as error:
        raise CloudSetupProtocolError("CloudSetup data.psk is not hexadecimal") from error
    if len(psk) != 16:
        raise CloudSetupProtocolError("CloudSetup data.psk must be 16 bytes")
    if not isinstance(code, str) or not code:
        raise CloudSetupProtocolError("CloudSetup data.code is missing")
    if not isinstance(primary_url, str) or not primary_url:
        raise CloudSetupProtocolError("CloudSetup data.cloudPrimaryUrl is missing")
    if standby_url is not None and not isinstance(standby_url, str):
        raise CloudSetupProtocolError("CloudSetup data.cloudStandbyUrl must be text")
    return CloudSetupBindingInfo(
        dev_id=dev_id,
        psk=psk,
        code=code,
        cloud_primary_url=primary_url,
        cloud_standby_url=standby_url,
    )

def decode_cloud_setup_auth_info(plaintext: bytes | str) -> CloudSetupAuthInfo:
    auth = _cloud_setup_data(plaintext)
    dev_id = auth.get("devId")
    auth_code = auth.get("authCode")
    auth_code_id = auth.get("authCodeId")
    uid_hash = auth.get("uidHash")
    if not isinstance(dev_id, str) or not dev_id:
        raise CloudSetupProtocolError("CloudSetup data.devId is missing")
    if not isinstance(auth_code_id, str) or not auth_code_id:
        raise CloudSetupProtocolError("CloudSetup data.authCodeId is missing")
    if uid_hash is not None and not isinstance(uid_hash, str):
        raise CloudSetupProtocolError("CloudSetup data.uidHash must be text")
    try:
        code = bytes.fromhex(auth_code) if isinstance(auth_code, str) else b""
    except ValueError as error:
        raise CloudSetupProtocolError("CloudSetup data.authCode is not hexadecimal") from error
    if len(code) != 16:
        raise CloudSetupProtocolError("CloudSetup data.authCode must be 16 bytes")
    return CloudSetupAuthInfo(
        dev_id=dev_id,
        auth_code=code,
        auth_code_id=auth_code_id,
        uid_hash=uid_hash,
    )