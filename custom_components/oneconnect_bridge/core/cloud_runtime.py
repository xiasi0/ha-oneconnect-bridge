"""OneConnect Bridge 云端注册与在线会话."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import secrets
import select
import socket
import ssl
from threading import Event, Lock, Thread
import time
from typing import Callable
from urllib.parse import urlsplit

from .m2m import (
    ACCESS_TOKEN_ID,
    BRIDGE_COAP_CONTENT,
    COAP_POST,
    DEVICE_ID,
    REQUEST_ID,
    SEQUENCE_NUMBER,
    USER_ID,
    URI_PATH,
    BridgeCloudMessage,
    BridgeCloudRequest,
    BridgeDeviceInfo,
    CoapTcpOption,
    build_bridge_activate_request,
    build_bridge_dev_info_sync_request,
    build_bridge_grs_request,
    build_bridge_heartbeat_request,
    build_bridge_login_request,
    encode_bridge_message,
)
from .speke import CloudSetupBindingInfo

_LOGGER = logging.getLogger(__name__)

MAX_PENDING_CHILD_STATE_REQUESTS = 256
MAX_CHILD_STATE_REQUESTS_PER_TICK = 4

class CloudRuntimeError(RuntimeError):
    """A cloud transport or public protocol stage failed."""

    def __init__(self, message: str, *, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode

@dataclass(frozen=True)
class CloudRunResult:
    registered: bool
    stage: str
    errcode: int | None = None
    error: str | None = None

@dataclass(frozen=True)
class OnlineChildCommand:
    """ACK and optional state report produced for one child M2M command."""

    response: bytes
    state_requests: tuple[BridgeCloudRequest, ...] = ()
    post_ack: Callable[["CloudM2MClient"], None] | None = None

class JsonTrace:
    """Optionally persist a complete cloud trace for an explicit debug run."""

    def __init__(self, path: Path | None, *, metadata: dict[str, object]) -> None:
        self.path = path
        self.document: dict[str, object] = {
            "schema": "oneconnect_bridge.real-cloud-trace.v1",
            "metadata": metadata,
            "records": [],
        }
        self._write()

    def record(self, event: str, **data: object) -> None:
        if self.path is None:
            return
        records = self.document["records"]
        assert isinstance(records, list)
        records.append({"timestamp": time.time(), "event": event, **data})
        self._write()

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)

def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")

def _json_object(payload: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None

def _endpoint(raw_url: str) -> tuple[str, int]:
    value = raw_url.strip()
    parsed = urlsplit(value if "://" in value else f"//{value}")
    if not parsed.hostname:
        raise CloudRuntimeError("CloudSetup primary URL has no hostname")
    try:
        port = parsed.port or 5683
    except ValueError as error:
        raise CloudRuntimeError("CloudSetup primary URL has an invalid port") from error
    return parsed.hostname, port

def normalize_certificate(value: str | bytes) -> str:
    """Return one validated certificate in PEM form."""

    if isinstance(value, bytes):
        raw = value
        try:
            text = raw.decode("ascii").lstrip("\ufeff").strip()
        except UnicodeDecodeError:
            return ssl.DER_cert_to_PEM_cert(raw)
    else:
        text = value.lstrip("\ufeff").strip()
        raw = text.encode("ascii", errors="strict")

    if "-----BEGIN CERTIFICATE-----" in text:
        ssl.PEM_cert_to_DER_cert(text)
        return text

    try:
        return ssl.DER_cert_to_PEM_cert(base64.b64decode(raw, validate=True))
    except (ValueError, binascii.Error) as error:
        raise CloudRuntimeError("TLS trust store contains no valid PEM/DER certificate") from error

def read_ca_certificates(path: Path) -> list[str]:
    """Read and validate all certificates from a trust-store JSON file."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("certificates"), list):
        raise CloudRuntimeError("TLS trust JSON has no certificates list")
    certificates = value["certificates"]
    if not certificates or not all(isinstance(item, str) for item in certificates):
        raise CloudRuntimeError("TLS trust JSON contains no usable certificates")
    try:
        return [normalize_certificate(item) for item in certificates]
    except (UnicodeEncodeError, ValueError, binascii.Error) as error:
        raise CloudRuntimeError("TLS trust JSON contains an invalid certificate") from error

def _read_ca_json(path: Path | None) -> str | None:
    if path is None:
        return None
    return "\n".join(read_ca_certificates(path))

class CloudM2MClient:
    """Run the observed bridge registration or bound online session over TLS."""

    def __init__(
        self,
        *,
        binding: CloudSetupBindingInfo | None = None,
        serial_number: str,
        mac: str,
        trace_path: Path | None = None,
        material_path: Path | None = None,
        on_online: Callable[[], None] | None = None,
        on_cloud_ready: Callable[["CloudM2MClient", str], None] | None = None,
        on_child_command: Callable[
            ["CloudM2MClient", BridgeCloudMessage], OnlineChildCommand | None
        ] | None = None,
        on_worker_tick: Callable[["CloudM2MClient"], None] | None = None,
        interface_ip: str | None = None,
        device_model: str = "DL-01W",
        device_type: str = "A17",
        manufacturer: str = "002",
        product_id: str = "ZG37",
        protocol_type: int = 1,
        firmware_version: str = "1.0.0",
        hardware_version: str = "1.0.0",
        software_version: str = "1.0.0",
        cloud_primary_url: str | None = None,
        cloud_standby_url: str | None = None,
        ca_json: Path | None = None,
        insecure_tls: bool = False,
        timeout: float = 30.0,
    ) -> None:
        if binding is None and not cloud_primary_url:
            raise ValueError("CloudSetup binding or cloud primary URL is required")
        self.binding = binding
        self.cloud_primary_url = (
            binding.cloud_primary_url if binding is not None else cloud_primary_url
        )
        self.cloud_standby_url = (
            binding.cloud_standby_url if binding is not None else cloud_standby_url
        )
        self.serial_number = serial_number
        self.interface_ip = interface_ip or ""
        self.device_model = device_model
        self.device_type = device_type
        self.manufacturer = manufacturer
        self.product_id = product_id
        self.protocol_type = protocol_type
        self.firmware_version = firmware_version
        self.hardware_version = hardware_version
        self.software_version = software_version
        mac_hex = mac.replace(":", "").replace("-", "").upper()
        if len(mac_hex) != 12:
            raise ValueError("gateway MAC address must contain six bytes")
        self.mac = ":".join(mac_hex[index : index + 2] for index in range(0, 12, 2))
        self.timeout = timeout
        self.material_path = material_path
        self.on_online = on_online
        self.on_cloud_ready = on_cloud_ready
        self.on_child_command = on_child_command
        self.on_worker_tick = on_worker_tick
        self.trace = JsonTrace(
            trace_path,
            metadata={
                "primary_url": self.cloud_primary_url,
                "standby_url": self.cloud_standby_url,
                "serial_number": serial_number,
                "device_model": device_model,
                "device_type": device_type,
                "manufacturer": manufacturer,
                "product_id": product_id,
                "protocol_type": protocol_type,
                "firmware_version": firmware_version,
                "hardware_version": hardware_version,
                "software_version": software_version,
                "tls_verification": "disabled" if insecure_tls else "enabled",
                "ca_json": str(ca_json) if ca_json is not None else None,
            },
        )
        self.ca_json = ca_json
        self.insecure_tls = insecure_tls
        self._socket: ssl.SSLSocket | None = None
        self._stage = "created"
        self._active_dev_id: str | None = None
        self._online_access_token: str | None = None
        self._online_response_sequence = 0
        self._child_response_sequence = 0
        self._child_state_queue: dict[tuple[str, str], BridgeCloudRequest] = {}
        self._child_state_queue_lock = Lock()
        self._heartbeat_responses: dict[bytes, tuple[int, float, bool]] = {}
        self._heartbeat_misses = 0

    @property
    def online_access_token(self) -> str | None:
        """Return the access token of the active parent cloud session."""

        return self._online_access_token

    def next_child_response_sequence(self) -> int:
        """Allocate the one-byte sequence used by child command ACKs."""

        value = self._child_response_sequence
        self._child_response_sequence = (value + 1) & 0xFF
        return value

    def enqueue_child_state_requests(
        self, requests: tuple[BridgeCloudRequest, ...] | list[BridgeCloudRequest]
    ) -> None:
        """Queue only the newest snapshot for each child service."""

        with self._child_state_queue_lock:
            for request in requests:
                key = self._child_state_request_key(request)
                if key in self._child_state_queue:
                    del self._child_state_queue[key]
                elif len(self._child_state_queue) >= MAX_PENDING_CHILD_STATE_REQUESTS:
                    oldest_key = next(iter(self._child_state_queue))
                    del self._child_state_queue[oldest_key]
                    _LOGGER.warning(
                        "Dropping oldest queued child state snapshot key=%s; "
                        "queue limit=%s",
                        oldest_key,
                        MAX_PENDING_CHILD_STATE_REQUESTS,
                    )
                self._child_state_queue[key] = request

    def _flush_child_state_requests(self) -> None:
        """Send a bounded number of queued snapshots from the cloud worker."""

        for _ in range(MAX_CHILD_STATE_REQUESTS_PER_TICK):
            with self._child_state_queue_lock:
                if not self._child_state_queue:
                    return
                key = next(iter(self._child_state_queue))
                request = self._child_state_queue.pop(key)
            try:
                response = self._request("child_data_sync", request)
                _LOGGER.debug(
                    "Queued child dataSync accepted errcode=%s",
                    response.get("errcode"),
                )
            except CloudRuntimeError as error:
                with self._child_state_queue_lock:
                    self._child_state_queue.setdefault(key, request)
                _LOGGER.warning(
                    "Queued child dataSync failed: %s",
                    error,
                )
                raise

    @staticmethod
    def _child_state_request_key(request: BridgeCloudRequest) -> tuple[str, str]:
        """Return the child/service identity used to coalesce state snapshots."""

        try:
            payload = json.loads(request.payload.decode("utf-8"))
            item = payload[0]
            service = item["services"][0]
            dev_id = item["devId"]
            service_id = service["sid"]
            if isinstance(dev_id, str) and isinstance(service_id, str):
                return dev_id, service_id
        except (IndexError, KeyError, TypeError, ValueError, UnicodeDecodeError):
            pass
        return "__opaque__", request.token.hex()

    def _notify_online(self) -> None:
        if self.on_online is not None:
            self.on_online()

    def run(self, *, stop_event: Event | None = None) -> CloudRunResult:
        try:
            return self._run(stop_event)
        except Exception as error:  # noqa: BLE001 - persisted experiment result
            self.trace.record(
                "cloud_registration_failed",
                stage=self._stage,
                error_type=type(error).__name__,
                error=str(error),
            )
            return CloudRunResult(
                registered=False,
                stage=self._stage,
                errcode=getattr(error, "errcode", None),
                error=f"{type(error).__name__}: {error}",
            )
        finally:
            if self._socket is not None:
                try:
                    self._socket.close()
                finally:
                    self._socket = None

    def _run(self, stop_event: Event | None = None) -> CloudRunResult:
        if self.binding is None:
            raise CloudRuntimeError("activation requires CloudSetup binding information")
        self._stage = "tls_connect"
        self._connect()

        self._stage = "activate"
        activate = build_bridge_activate_request(
            code=self.binding.code,
            device_info=BridgeDeviceInfo(
                serial_number=self.serial_number,
                mac=self.mac,
                model=self.device_model,
                device_type=self.device_type,
                manufacturer=self.manufacturer,
                product_id=self.product_id,
                protocol_type=self.protocol_type,
                firmware_version=self.firmware_version,
                hardware_version=self.hardware_version,
                software_version=self.software_version,
            ),
            token=secrets.token_bytes(2),
        )
        activate_json = self._request("activate", activate)
        activate_errcode = self._require_errcode(activate_json, "activate")
        if activate_errcode != 0:
            self.trace.record(
                "cloud_registration_result",
                registered=False,
                stage="activate",
                errcode=activate_errcode,
                response_keys=sorted(activate_json),
            )
            return CloudRunResult(
                registered=False,
                stage="activate",
                errcode=activate_errcode,
                error=f"cloud activate errcode {activate_errcode}",
            )
        dev_id = activate_json.get("devId")
        secret = activate_json.get("secret")
        if not isinstance(dev_id, str) or not dev_id or not isinstance(secret, str) or not secret:
            raise CloudRuntimeError("activation response is missing devId or secret")
        self._active_dev_id = dev_id
        self.trace.record(
            "cloud_activation_accepted",
            dev_id_length=len(dev_id),
            secret_length=len(secret),
            response_keys=sorted(activate_json),
        )

        self._stage = "login"
        login = build_bridge_login_request(
            dev_id=dev_id,
            secret=secret,
            token=secrets.token_bytes(2),
        )
        login_json = self._request("login", login)
        login_errcode = self._require_errcode(login_json, "login")
        if login_errcode != 0:
            self.trace.record(
                "cloud_registration_result",
                registered=False,
                stage="login",
                errcode=login_errcode,
                response_keys=sorted(login_json),
            )
            return CloudRunResult(
                registered=False,
                stage="login",
                errcode=login_errcode,
                error=f"cloud login errcode {login_errcode}",
            )

        access_token = login_json.get("accessToken")
        refresh_token = login_json.get("refreshToken")
        if not isinstance(access_token, str) or not access_token:
            raise CloudRuntimeError("login response is missing accessToken")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise CloudRuntimeError("login response is missing refreshToken")
        state = {
            "schema": "oneconnect_bridge.bridge-cloud-state.v1",
            "devId": dev_id,
            "secret": secret,
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "timeout": login_json.get("timeout"),
            "timestamp": login_json.get("timestamp"),
            "tz": login_json.get("tz"),
            "heartbeatPolicy": login_json.get("heartbeatPolicy"),
            "cloudPrimaryUrl": self.cloud_primary_url,
            "cloudStandbyUrl": self.cloud_standby_url,
        }
        material_path = self._write_cloud_material(
            {
                "devId": dev_id,
                "secret": secret,
                "cloudPrimaryUrl": self.cloud_primary_url,
                "cloudStandbyUrl": self.cloud_standby_url,
            },
            remove_keys=("psk", "code"),
        )
        self.trace.record(
            "cloud_login_accepted",
            response_keys=sorted(login_json),
            material_file=str(material_path) if material_path is not None else None,
        )
        self.trace.record(
            "cloud_registration_result",
            registered=True,
            stage="login",
            errcode=0,
            response_keys=sorted(login_json),
        )
        self._send_dev_info_sync(dev_id=dev_id, access_token=access_token)
        self._online_access_token = access_token
        if self.on_cloud_ready is not None:
            self.on_cloud_ready(self, access_token)
        self._notify_online()
        if stop_event is not None:
            return self._maintain_online(
                state,
                login_json,
                stop_event,
                next_sequence=2,
            )
        return CloudRunResult(registered=True, stage="login", errcode=0)

    def run_bound(self, state: dict[str, object], stop_event: Event) -> CloudRunResult:
        """Re-login an already registered Python identity and keep it online."""
        try:
            return self._run_bound(state, stop_event)
        except Exception as error:  # noqa: BLE001 - persisted live experiment
            self.trace.record(
                "cloud_online_session_failed",
                stage=self._stage,
                error_type=type(error).__name__,
                error=str(error),
            )
            return CloudRunResult(
                registered=False,
                stage=self._stage,
                errcode=getattr(error, "errcode", None),
                error=f"{type(error).__name__}: {error}",
            )
        finally:
            if self._socket is not None:
                try:
                    self._socket.close()
                finally:
                    self._socket = None

    def _run_bound(self, state: dict[str, object], stop_event: Event) -> CloudRunResult:
        dev_id = state.get("devId")
        secret = state.get("secret")
        if not isinstance(dev_id, str) or not dev_id:
            raise CloudRuntimeError("registered cloud state has no devId")
        if not isinstance(secret, str) or not secret:
            raise CloudRuntimeError("registered cloud state has no secret")
        self._active_dev_id = dev_id
        if not self.cloud_primary_url:
            raise CloudRuntimeError("registered cloud state has no primary URL")

        self._stage = "tls_connect"
        self._connect()

        self._stage = "login"
        login = build_bridge_login_request(
            dev_id=dev_id,
            secret=secret,
            token=secrets.token_bytes(2),
        )
        login_json = self._request("login", login)
        login_errcode = self._require_errcode(login_json, "login")
        if login_errcode != 0:
            raise CloudRuntimeError(
                f"cloud re-login errcode {login_errcode}",
                errcode=login_errcode,
            )
        access_token = login_json.get("accessToken")
        refresh_token = login_json.get("refreshToken")
        policy = login_json.get("heartbeatPolicy")
        if not isinstance(access_token, str) or not access_token:
            raise CloudRuntimeError("login response is missing accessToken")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise CloudRuntimeError("login response is missing refreshToken")
        if not isinstance(policy, dict):
            raise CloudRuntimeError("login response is missing heartbeatPolicy")
        period = policy.get("period")
        timeout = policy.get("timeout")
        retry_times = policy.get("rtTimes")
        if not all(isinstance(value, int) and value > 0 for value in (period, timeout, retry_times)):
            raise CloudRuntimeError("heartbeatPolicy has invalid period/timeout/rtTimes")

        current_state = dict(state)
        current_state.update(
            {
                "accessToken": access_token,
                "refreshToken": refresh_token,
                "timeout": login_json.get("timeout"),
                "timestamp": login_json.get("timestamp"),
                "tz": login_json.get("tz"),
                "heartbeatPolicy": policy,
            }
        )
        material_path = self._write_cloud_material(
            {
                "devId": dev_id,
                "secret": secret,
                "cloudPrimaryUrl": self.cloud_primary_url,
                "cloudStandbyUrl": self.cloud_standby_url,
            }
        )
        self.trace.record(
            "cloud_login_accepted",
            response_keys=sorted(login_json),
            material_file=str(material_path) if material_path is not None else None,
        )
        self._send_dev_info_sync(dev_id=dev_id, access_token=access_token)
        self._online_access_token = access_token
        if self.on_cloud_ready is not None:
            self.on_cloud_ready(self, access_token)
        self._notify_online()

        return self._maintain_online(
            current_state,
            login_json,
            stop_event,
            next_sequence=2,
        )

    def _send_dev_info_sync(self, *, dev_id: str, access_token: str) -> None:
        """Publish the current gateway model after every successful login."""

        self._stage = "dev_info_sync"
        request = build_bridge_dev_info_sync_request(
            dev_id=dev_id,
            access_token=access_token,
            device_info=BridgeDeviceInfo(
                serial_number=self.serial_number,
                mac=self.mac,
                model=self.device_model,
                device_type=self.device_type,
                manufacturer=self.manufacturer,
                product_id=self.product_id,
                protocol_type=self.protocol_type,
                firmware_version=self.firmware_version,
                hardware_version=self.hardware_version,
                software_version=self.software_version,
            ),
            token=secrets.token_bytes(2),
            sequence_number=1,
        )
        response = self._request("dev_info_sync", request)
        errcode = self._require_errcode(response, "dev_info_sync")
        if errcode != 0:
            raise CloudRuntimeError(
                f"cloud devInfoSync errcode {errcode}",
                errcode=errcode,
            )
        self.trace.record(
            "cloud_dev_info_sync_accepted",
            errcode=errcode,
            response_keys=sorted(response),
        )

    def _maintain_online(
        self,
        state: dict[str, object],
        login_json: dict[str, object],
        stop_event: Event,
        *,
        next_sequence: int,
    ) -> CloudRunResult:
        dev_id = state.get("devId")
        access_token = login_json.get("accessToken")
        policy = login_json.get("heartbeatPolicy")
        if not isinstance(dev_id, str) or not dev_id:
            raise CloudRuntimeError("cloud state is missing devId")
        if not isinstance(access_token, str) or not access_token:
            raise CloudRuntimeError("login response is missing accessToken")
        if not isinstance(policy, dict):
            raise CloudRuntimeError("login response is missing heartbeatPolicy")
        period = policy.get("period")
        timeout = policy.get("timeout")
        retry_times = policy.get("rtTimes")
        if not all(isinstance(value, int) and value > 0 for value in (period, timeout, retry_times)):
            raise CloudRuntimeError("heartbeatPolicy has invalid period/timeout/rtTimes")

        self._online_access_token = access_token

        self._stage = "grs"
        grs = build_bridge_grs_request(
            dev_id=dev_id,
            access_token=access_token,
            token=secrets.token_bytes(2),
            sequence_number=next_sequence,
        )
        grs_response = self._request("grs", grs)
        self.trace.record(
            "cloud_grs_accepted",
            errcode=grs_response.get("errcode"),
            response_keys=sorted(grs_response),
        )
        sequence = (next_sequence + 1) & 0xFF

        apply_hb_factor = {
            "period": period,
            "timeout": timeout,
            "rtTimes": retry_times,
        }
        self._stage = "heartbeat_initial"
        first_heartbeat = build_bridge_heartbeat_request(
            access_token=access_token,
            token=secrets.token_bytes(2),
            sequence_number=sequence,
            apply_hb_factor=apply_hb_factor,
        )
        first_response = self._request("heartbeat_initial", first_heartbeat)
        self._require_success_if_present(first_response, "heartbeat")
        self.trace.record(
            "cloud_heartbeat_accepted",
            sequence=sequence,
            response_keys=sorted(first_response),
            errcode=first_response.get("errcode"),
        )
        sequence = (sequence + 1) & 0xFF

        self._stage = "heartbeat"
        self._socket.settimeout(timeout)
        self._heartbeat_responses = {}
        self._heartbeat_misses = 0
        next_heartbeat = time.monotonic() + period

        # 与 C SDK 相同：命令在 socket 可读时立即消费，心跳仅为周期性出站任务
        while not stop_event.is_set():
            if self.on_worker_tick is not None:
                self.on_worker_tick(self)
            self._flush_child_state_requests()
            now = time.monotonic()

            for token, (heartbeat_sequence, sent_at, timed_out) in tuple(
                self._heartbeat_responses.items()
            ):
                if timed_out or now - sent_at < timeout:
                    continue
                self._heartbeat_responses[token] = (
                    heartbeat_sequence,
                    sent_at,
                    True,
                )
                self._heartbeat_misses += 1
                self.trace.record(
                    "cloud_heartbeat_timeout",
                    sequence=heartbeat_sequence,
                    misses=self._heartbeat_misses,
                    retry_times=retry_times,
                )
                if self._heartbeat_misses >= retry_times:
                    raise CloudRuntimeError(
                        "cloud heartbeat reached the retry limit"
                    )

            if now >= next_heartbeat:
                heartbeat = build_bridge_heartbeat_request(
                    access_token=access_token,
                    token=secrets.token_bytes(2),
                    sequence_number=sequence,
                )
                self._send_raw("heartbeat", heartbeat.encode(), heartbeat)
                self._heartbeat_responses[heartbeat.token] = (
                    sequence,
                    time.monotonic(),
                    False,
                )
                sequence = (sequence + 1) & 0xFF
                next_heartbeat = now + period
                continue

            if self._socket is None:
                raise CloudRuntimeError("cloud socket is not connected")

            heartbeat_deadlines = [
                sent_at + timeout
                for _token, (_sequence, sent_at, timed_out)
                in self._heartbeat_responses.items()
                if not timed_out
            ]
            next_deadline = min(heartbeat_deadlines, default=next_heartbeat)
            wait_timeout = min(next_heartbeat - now, next_deadline - now, 0.5)
            readable, _, _ = select.select([self._socket], [], [], wait_timeout)
            if not readable:
                continue

            message, response_wire = self._receive_message("online")
            if self._dispatch_inbound_message(message, response_wire):
                continue

            path = self._uri_path(message)
            self.trace.record(
                "m2m_unmatched_message",
                stage="online",
                code=message.code,
                token_length=len(message.token),
                path=path,
                wire_length=len(response_wire),
            )
            _LOGGER.warning(
                "Ignoring unsupported cloud message during online session: "
                "code=%s path=%s token_length=%s frame_length=%s",
                message.code,
                path,
                len(message.token),
                len(response_wire),
            )
            continue
        return CloudRunResult(registered=True, stage="heartbeat", errcode=0)

    def _connect(self) -> None:
        if not self.cloud_primary_url:
            raise CloudRuntimeError("CloudSetup primary URL is missing")
        candidates = [self.cloud_primary_url]
        if self.cloud_standby_url and self.cloud_standby_url not in candidates:
            candidates.append(self.cloud_standby_url)

        last_error: OSError | ssl.SSLError | None = None
        for index, raw_url in enumerate(candidates):
            host, port = _endpoint(raw_url)
            self.trace.record(
                "tls_connect_start",
                host=host,
                port=port,
                route_index=index,
            )
            raw_socket: socket.socket | None = None
            try:
                context = self._tls_context()
                raw_socket = socket.create_connection(
                    (host, port), timeout=self.timeout
                )
                self._socket = context.wrap_socket(raw_socket, server_hostname=host)
                self._socket.settimeout(self.timeout)
                self.trace.record(
                    "tls_connected",
                    version=self._socket.version(),
                    cipher=self._socket.cipher(),
                    peer_certificate=self._socket.getpeercert(),
                    route_index=index,
                )
                if index:
                    self.trace.record(
                        "cloud_route_failover",
                        route_index=index,
                        host=host,
                        port=port,
                    )
                return
            except (OSError, ssl.SSLError) as error:
                last_error = error
                self.trace.record(
                    "tls_connect_failed",
                    host=host,
                    port=port,
                    route_index=index,
                    error_type=type(error).__name__,
                    error=str(error),
                )
                if self._socket is not None:
                    try:
                        self._socket.close()
                    except OSError:
                        pass
                    self._socket = None
                elif raw_socket is not None:
                    try:
                        raw_socket.close()
                    except OSError:
                        pass

        raise CloudRuntimeError("unable to connect to any configured cloud route") from last_error

    def _write_cloud_material(
        self,
        updates: dict[str, object],
        *,
        remove_keys: tuple[str, ...] = (),
    ) -> Path | None:
        path = self.material_path
        if path is None:
            return None

        material: dict[str, object] = {}
        if path.is_file():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stored = {}
            if isinstance(stored, dict):
                material.update(stored)

        material["schema"] = "oneconnect_bridge.cloud-material.v2"
        for key in ("devId", "secret", "psk", "code", "cloudPrimaryUrl", "cloudStandbyUrl"):
            if key in updates and updates[key] is not None:
                material[key] = updates[key]
        for key in remove_keys:
            material.pop(key, None)

        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary.write_text(json.dumps(material, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
        return path

    def _tls_context(self) -> ssl.SSLContext:
        if self.insecure_tls:
            context = ssl._create_unverified_context()
        else:
            context = ssl.create_default_context()
            ca_data = _read_ca_json(self.ca_json)
            if ca_data is not None:
                context.load_verify_locations(cadata=ca_data)
                # 华为端只发叶子+中间证书、不发公共根：以实测中间证书为
                # partial-chain 信任锚；主机名与叶子签名校验保持开启
                partial_chain = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
                if partial_chain:
                    context.verify_flags |= partial_chain
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _request(
        self,
        stage: str,
        message: BridgeCloudRequest,
        *,
        require_json: bool = True,
    ) -> dict[str, object]:
        wire = message.encode()
        self._send_raw(stage + "_request", wire, message)
        while True:
            response, response_wire = self._receive_message(stage + "_response")
            if self._dispatch_inbound_message(response, response_wire):
                continue
            if response.token != message.token:
                path = self._uri_path(response)
                self.trace.record(
                    "m2m_unmatched_message",
                    stage=stage,
                    code=response.code,
                    token_length=len(response.token),
                    path=path,
                    wire_length=len(response_wire),
                )
                _LOGGER.warning(
                    "Ignoring unmatched cloud response during %s: "
                    "code=%s path=%s token_length=%s frame_length=%s",
                    stage,
                    response.code,
                    self._uri_path(response),
                    len(response.token),
                    len(response_wire),
                )
                continue
            if response.code != 0x45:
                raise CloudRuntimeError(f"{stage} response CoAP code was {response.code}")
            response_json = _json_object(response.payload)
            if response_json is None:
                if not require_json:
                    self.trace.record(
                        stage + "_response_empty_or_opaque",
                        wire_base64=_b64(response_wire),
                        wire_length=len(response_wire),
                    )
                    return {}
                raise CloudRuntimeError(f"{stage} response payload is not a JSON object")
            self.trace.record(
                stage + "_response_json",
                wire_base64=_b64(response_wire),
                wire_length=len(response_wire),
                value=response_json,
            )
            return response_json

    def request(
        self,
        stage: str,
        message: BridgeCloudRequest,
        *,
        require_json: bool = True,
    ) -> dict[str, object]:
        """Send one serialized request from the current parent cloud session."""

        return self._request(stage, message, require_json=require_json)

    @staticmethod
    def _uri_path(message: BridgeCloudMessage) -> list[str]:
        return [
            option.value.decode("utf-8", errors="replace")
            for option in message.options
            if option.number == URI_PATH
        ]

    def _dispatch_inbound_message(
        self, message: BridgeCloudMessage, response_wire: bytes
    ) -> bool:
        """Dispatch asynchronous messages before matching request responses."""

        path = self._uri_path(message)
        if message.code == COAP_POST and path == [".sys", "event"]:
            self._dispatch_cloud_event(message)
            return True
        if self._handle_online_message(message, response_wire):
            return True
        return self._consume_heartbeat_response(message)

    def _consume_heartbeat_response(self, message: BridgeCloudMessage) -> bool:
        if message.code != BRIDGE_COAP_CONTENT:
            return False
        heartbeat = self._heartbeat_responses.get(message.token)
        if heartbeat is None:
            return False
        response = _json_object(message.payload)
        if response is None:
            raise CloudRuntimeError("heartbeat response payload is not a JSON object")
        self._require_success_if_present(response, "heartbeat")
        heartbeat_sequence, _sent_at, _timed_out = self._heartbeat_responses.pop(
            message.token
        )
        self._heartbeat_misses = 0
        self.trace.record(
            "cloud_heartbeat_accepted",
            sequence=heartbeat_sequence,
            response_keys=sorted(response),
            errcode=response.get("errcode"),
        )
        return True

    def _dispatch_cloud_event(self, message: BridgeCloudMessage) -> bool:
        payload = _json_object(message.payload)
        if payload is None:
            self._ignore_cloud_event(message, "payload is not a JSON object")
            return False
        header = payload.get("header")
        body = payload.get("body")
        if not isinstance(header, dict) or not isinstance(body, dict):
            self._ignore_cloud_event(message, "header or body is missing")
            return False
        category = header.get("category")
        notify_type = header.get("notifyType")
        dev_id = body.get("devId")
        if category != "grs" or notify_type != "addressUpdated":
            self._ignore_cloud_event(
                message,
                "unsupported category/type",
                category=category,
                notify_type=notify_type,
            )
            return False
        if not isinstance(dev_id, str) or dev_id != self._active_dev_id:
            self._ignore_cloud_event(message, "device identity did not match")
            return False
        entries = body.get("grs")
        if not isinstance(entries, list):
            self._ignore_cloud_event(message, "GRS route list is missing")
            return False
        known_route_keys = {
            "hilinkDevicePrimaryCloudUrl",
            "hilinkDeviceStandbyCloudUrl",
        }
        route_updates = {
            entry.get("key"): entry.get("address")
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("key"), str)
            and entry.get("key") in known_route_keys
            and isinstance(entry.get("address"), str)
        }
        if not route_updates:
            self._ignore_cloud_event(message, "GRS address update contains no routes")
            return False
        if "hilinkDevicePrimaryCloudUrl" in route_updates:
            self.cloud_primary_url = route_updates["hilinkDevicePrimaryCloudUrl"]
        if "hilinkDeviceStandbyCloudUrl" in route_updates:
            self.cloud_standby_url = route_updates["hilinkDeviceStandbyCloudUrl"]
        self.trace.record(
            "cloud_event_dispatched",
            category=category,
            notify_type=notify_type,
            route_updates=route_updates,
        )
        self._write_cloud_material(
            {
                "cloudPrimaryUrl": route_updates.get("hilinkDevicePrimaryCloudUrl"),
                "cloudStandbyUrl": route_updates.get("hilinkDeviceStandbyCloudUrl"),
            }
        )
        return True

    def _ignore_cloud_event(
        self,
        message: BridgeCloudMessage,
        reason: str,
        category: object | None = None,
        notify_type: object | None = None,
        **details: object,
    ) -> None:
        payload = _json_object(message.payload) or {}
        header = payload.get("header")
        if category is None:
            category = header.get("category") if isinstance(header, dict) else None
        if notify_type is None:
            notify_type = header.get("notifyType") if isinstance(header, dict) else None
        self.trace.record(
            "cloud_event_ignored",
            reason=reason,
            category=category,
            notify_type=notify_type,
            **details,
        )
        _LOGGER.warning(
            "Ignoring cloud event reason=%s category=%s notifyType=%s",
            reason,
            category,
            notify_type,
        )

    def _handle_online_message(
        self, message: BridgeCloudMessage, response_wire: bytes
    ) -> bool:
        """Handle online messages not belonging to a specialized runtime."""

        if self.on_child_command is not None:
            child_action = self.on_child_command(self, message)
            if child_action is not None:
                if self._socket is None:
                    raise CloudRuntimeError("cannot acknowledge child command without socket")
                self._socket.sendall(child_action.response)
                self.trace.record(
                    "child_command_ack_sent",
                    wire_length=len(child_action.response),
                    state_report_count=len(child_action.state_requests),
                )
                if child_action.state_requests:
                    self.enqueue_child_state_requests(child_action.state_requests)
                if child_action.post_ack is not None:
                    def run_post_ack() -> None:
                        try:
                            child_action.post_ack(self)
                        except Exception:  # noqa: BLE001 - ACK already succeeded
                            _LOGGER.exception("Child post-ACK handling failed")

                    Thread(
                        target=run_post_ack,
                        name="oneconnect_bridge-child-post-ack",
                        daemon=True,
                    ).start()
                return True

        del response_wire
        paths = self._uri_path(message)
        is_net_info = message.code == 0x01 and paths == ["netInfo"]
        is_discovery = message.code == COAP_POST and paths == ["discovery"]
        is_rssi_data = message.code == COAP_POST and paths == ["rssiData"]
        if not (is_net_info or is_discovery or is_rssi_data):
            return False

        option_values: dict[int, bytes] = {}
        for option in message.options:
            if option.number in (REQUEST_ID, DEVICE_ID, USER_ID):
                if option.number in option_values:
                    return False
                option_values[option.number] = option.value

        if any(number not in option_values for number in (REQUEST_ID, DEVICE_ID, USER_ID)):
            return False
        if self._online_access_token is None or self._socket is None:
            raise CloudRuntimeError("cannot acknowledge cloud control without online auth")

        if is_discovery:
            if _json_object(message.payload) != {"enable": 1}:
                return False
            try:
                target_dev_id = option_values[DEVICE_ID].decode("utf-8")
            except UnicodeDecodeError:
                return False
            if target_dev_id != self._active_dev_id:
                return False

        sequence = self._online_response_sequence.to_bytes(4, "big")
        self._online_response_sequence = (self._online_response_sequence + 1) & 0xFFFFFFFF
        if is_net_info:
            response = encode_bridge_message(
                code=BRIDGE_COAP_CONTENT,
                token=message.token,
                options=(
                    CoapTcpOption.text(ACCESS_TOKEN_ID, self._online_access_token),
                    CoapTcpOption(REQUEST_ID, option_values[REQUEST_ID]),
                    CoapTcpOption(DEVICE_ID, option_values[DEVICE_ID]),
                    CoapTcpOption(USER_ID, option_values[USER_ID]),
                    CoapTcpOption(SEQUENCE_NUMBER, sequence),
                ),
                payload=json.dumps(
                    {
                        "intensity": 100,
                        "RSSI": 0,
                        "SSID": "",
                        "BSSID": "00:00:00:00:00:00",
                        "IP": self.interface_ip,
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            self._socket.sendall(response)
            self.trace.record(
                "m2m_tx",
                stage="online_net_info_response",
                wire_base64=_b64(response),
                wire_length=len(response),
                header_length=int.from_bytes(response[:2], "big"),
                code=BRIDGE_COAP_CONTENT,
                token_length=len(message.token),
                paths=[],
                sequence_hex=sequence.hex().upper(),
                payload_json={
                    "intensity": 100,
                    "RSSI": 0,
                    "SSID": "",
                    "BSSID": "00:00:00:00:00:00",
                    "IP": self.interface_ip,
                },
            )
            return True

        response = encode_bridge_message(
            code=BRIDGE_COAP_CONTENT,
            token=message.token,
            options=(
                CoapTcpOption.text(URI_PATH, "devControl"),
                CoapTcpOption.text(ACCESS_TOKEN_ID, self._online_access_token),
                CoapTcpOption(REQUEST_ID, option_values[REQUEST_ID]),
                CoapTcpOption(DEVICE_ID, option_values[DEVICE_ID]),
                CoapTcpOption(USER_ID, option_values[USER_ID]),
                CoapTcpOption(SEQUENCE_NUMBER, sequence),
            ),
            payload=b'{"errcode":0}',
        )
        self._socket.sendall(response)
        self.trace.record(
            "m2m_tx",
            stage="online_discovery_response" if is_discovery else "online_control_response",
            wire_base64=_b64(response),
            wire_length=len(response),
            header_length=int.from_bytes(response[:2], "big"),
            code=BRIDGE_COAP_CONTENT,
            token_length=len(message.token),
            paths=["devControl"],
            sequence_hex=sequence.hex().upper(),
            payload_base64=_b64(b'{"errcode":0}'),
            payload_json={"errcode": 0},
        )
        return True

    def _send_raw(self, stage: str, wire: bytes, message: BridgeCloudRequest) -> None:
        if self._socket is None:
            raise CloudRuntimeError("cloud socket is not connected")
        self._socket.sendall(wire)
        payload_json = _json_object(message.payload)
        self.trace.record(
            "m2m_tx",
            stage=stage,
            wire_base64=_b64(wire),
            wire_length=len(wire),
            header_length=int.from_bytes(wire[:2], "big"),
            code=message.code,
            token_length=len(message.token),
            paths=list(message.paths),
            sequence_hex=message.sequence.hex().upper(),
            payload_base64=_b64(message.payload),
            payload_json=payload_json,
        )

    def _receive_message(self, stage: str) -> tuple[BridgeCloudMessage, bytes]:
        header = self._read_exact(2)
        frame_length = int.from_bytes(header, "big")
        if frame_length == 0:
            raise CloudRuntimeError("cloud returned an empty response frame")
        wire = header + self._read_exact(frame_length)
        try:
            message = BridgeCloudMessage.decode(wire)
        except ValueError as error:
            self.trace.record(
                "m2m_rx_unparsed",
                stage=stage,
                wire_base64=_b64(wire),
                wire_length=len(wire),
            )
            raise CloudRuntimeError(f"cannot decode {stage} response: {error}") from error
        self.trace.record(
            "m2m_rx",
            stage=stage,
            wire_base64=_b64(wire),
            wire_length=len(wire),
            code=message.code,
            token_length=len(message.token),
            options=[{"number": option.number, "length": len(option.value)} for option in message.options],
            payload_base64=_b64(message.payload),
        )
        return message, wire

    def _read_exact(self, length: int) -> bytes:
        if self._socket is None:
            raise CloudRuntimeError("cloud socket is not connected")
        result = bytearray()
        while len(result) < length:
            chunk = self._socket.recv(length - len(result))
            if not chunk:
                raise CloudRuntimeError("cloud socket closed before frame completion")
            result.extend(chunk)
        return bytes(result)

    @staticmethod
    def _require_errcode(payload: dict[str, object], stage: str) -> int:
        value = payload.get("errcode")
        if not isinstance(value, int):
            raise CloudRuntimeError(f"{stage} response has no numeric errcode")
        return value

    @staticmethod
    def _require_success_if_present(payload: dict[str, object], stage: str) -> None:
        value = payload.get("errcode")
        if isinstance(value, int) and value != 0:
            raise CloudRuntimeError(f"cloud {stage} errcode {value}")