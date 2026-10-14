"""OneConnect Bridge 网关运行时与配对代理."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import secrets
import socket
from queue import Empty, Queue
from pathlib import Path
import threading
import time
from typing import Callable
from urllib.request import urlopen

from .const import (
    DEFAULT_DEVICE_TYPE,
    DEFAULT_MANUFACTURER,
    DEFAULT_MODEL,
    DEFAULT_PRODUCT_ID,
    DEFAULT_PROTOCOL_TYPE,
    DISCOVERY_MULTICAST,
    DISCOVERY_PORT,
    GATEWAY_DEVICE_TYPE_NAME,
    GATEWAY_MANUFACTURER,
    GATEWAY_NAME,
    GATEWAY_STATUS_ERROR,
    GATEWAY_STATUS_OFFLINE,
    GATEWAY_STATUS_ONLINE,
    GATEWAY_STATUS_PAIRING,
    GATEWAY_STATUS_REGISTERING,
    MAX_GATEWAY_INSTANCES,
    PIN_TTL_SECONDS,
    VIRTUAL_HARDWARE_VERSION,
)
from .business.child_manager import ChildManager
from .business.models import GatewayPlan
from .core.cloud_runtime import (
    CloudM2MClient,
    normalize_certificate,
    read_ca_certificates,
)
from .core.coap import DeviceDescriptor, ServiceDescriptor
from .core.pairing_runtime import LocalPairingResponder, PairingResult
from .core.speke import CloudSetupAuthInfo, CloudSetupBindingInfo
from .storage import GatewayStore

HUAWEI_ROOT_CA_URL = (
    "https://h5hosting-drcn.dbankcdn.cn/cch5/crl/pki_CA_RootCa/RootCa.cer"
)
MANIFEST_PATH = Path(__file__).with_name("manifest.json")
_LOGGER = logging.getLogger(__name__)
RECONNECT_INITIAL_DELAY_SECONDS = 5.0
RECONNECT_MAX_DELAY_SECONDS = 600.0
TERMINAL_CLOUD_ERRCODES = frozenset({3, 5, 6})

def _read_integration_version() -> str:
    """Read the installed integration version from its manifest."""

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise ValueError("manifest.json has no valid integration version")
    return version.strip()

@dataclass(frozen=True)
class GatewayConfig:
    """Stable configuration supplied by one Home Assistant config entry."""

    config_dir: str
    interface_ip: str
    mac: str
    serial: str
    product_id: str = DEFAULT_PRODUCT_ID
    device_type: str = DEFAULT_DEVICE_TYPE
    manufacturer: str = DEFAULT_MANUFACTURER
    model: str = DEFAULT_MODEL
    protocol_type: int = DEFAULT_PROTOCOL_TYPE

class PairingBusyError(RuntimeError):
    """Raised when another gateway already owns the local pairing listener."""

class PairingBroker:
    """Own the single local UDP 5683 listener used by active pairing."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: GatewayRuntime | None = None
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, runtime: "GatewayRuntime") -> None:
        with self._lock:
            if self._active is not None and self._active is not runtime:
                raise PairingBusyError("another gateway is currently pairing")
            if self._active is runtime:
                return
            self._active = runtime
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"oneconnect_bridge-pairing-{runtime.serial}",
                daemon=True,
            )
            self._thread.start()

    def unregister(self, runtime: "GatewayRuntime") -> None:
        with self._lock:
            if self._active is not runtime:
                return
            self._stop.set()
            sock = self._socket
            thread = self._thread
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            if self._active is runtime:
                self._active = None
                self._socket = None
                self._thread = None

    def _run(self) -> None:
        with self._lock:
            runtime = self._active
        if runtime is None:
            return

        sock: socket.socket | None = None
        pairing_complete = False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", DISCOVERY_PORT))
            membership = socket.inet_aton(DISCOVERY_MULTICAST) + socket.inet_aton(
                runtime.config.interface_ip
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.settimeout(0.5)
            with self._lock:
                self._socket = sock
            runtime.on_pairing_listener_started()

            while not self._stop.is_set():
                try:
                    packet, remote = sock.recvfrom(16384)
                except TimeoutError:
                    # PIN 过期但 App 已建立 SPEKE 会话时不得杀监听：CloudSetup 尚未到达
                    if runtime.pairing_expired and not runtime.pairing_in_progress:
                        _LOGGER.debug("Gateway %s pairing PIN expired", runtime.serial)
                        runtime.on_pairing_expired()
                        break
                    continue
                except OSError:
                    break

                response, result = runtime.handle_pairing_datagram(packet, remote[0])
                _LOGGER.debug(
                    "Gateway %s pairing packet from %s len=%s kind=%s",
                    runtime.serial,
                    remote[0],
                    len(packet),
                    result.kind,
                )
                if response is not None:
                    try:
                        sock.sendto(response, remote)
                    except OSError:
                        break
                if runtime.consume_pairing_complete():
                    pairing_complete = True
                    _LOGGER.debug("Gateway %s pairing complete", runtime.serial)
                    break
        except Exception as error:  # noqa: BLE001 - surfaced through gateway status
            runtime.on_pairing_error(error)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._lock:
                if self._socket is sock:
                    self._socket = None
                if self._active is runtime:
                    self._active = None
                    self._thread = None
            if pairing_complete:
                runtime.on_pairing_listener_stopped()
                runtime.start_cloud_from_binding()

class GatewayRuntime:
    """Runtime for one independently identified Huawei parent gateway."""

    def __init__(self, config: GatewayConfig, broker: PairingBroker) -> None:
        self.config = config
        self.serial = config.serial
        self.store = GatewayStore(config.config_dir, config.serial)
        self.broker = broker
        self._lock = threading.RLock()
        self._listeners: list[Callable[[], None]] = []
        self._status = GATEWAY_STATUS_OFFLINE
        self._pin: str | None = None
        self._pin_expires_at = 0.0
        self._pairing_complete_pending = False
        self._pairing_responder: LocalPairingResponder | None = None
        self._binding: CloudSetupBindingInfo | None = None
        self._cloud_stop = threading.Event()
        self._cloud_thread: threading.Thread | None = None
        self._stopped = False
        self._child_manager: ChildManager | None = None
        self._entity_bridge = None
        self._active_client: CloudM2MClient | None = None
        self._integration_version = ""
        self._reload_queue: Queue[dict[str, object]] = Queue()

    def set_gateway_plan(self, plan: GatewayPlan | None) -> None:
        """Attach the compiled child plan for this gateway instance."""

        self._child_manager = (
            ChildManager(self.store, plan) if plan is not None else None
        )
        if self._child_manager is not None and self._entity_bridge is not None:
            self._child_manager.set_entity_bridge(self._entity_bridge)

    @property
    def child_count(self) -> int:
        """Return the number of child-device entries in the active plan."""

        manager = self._child_manager
        plan = manager.plan if manager is not None else None
        return len(plan.children) if plan is not None else 0

    def reload_plan(self, plan: GatewayPlan | None, *, timeout: float = 120.0) -> None:
        """Apply one mapping plan through this gateway's cloud worker."""

        with self._lock:
            client = self._active_client
            thread = self._cloud_thread
        if client is None or thread is None or not thread.is_alive():
            self.set_gateway_plan(plan)
            return
        request: dict[str, object] = {"event": threading.Event(), "plan": plan}
        self._reload_queue.put(request)
        event = request["event"]
        assert isinstance(event, threading.Event)
        if not event.wait(timeout):
            raise TimeoutError(f"gateway {self.serial} child reload timed out")
        error = request.get("error")
        if isinstance(error, Exception):
            raise error

    def _process_reload_requests(self, client: CloudM2MClient) -> None:
        while True:
            try:
                request = self._reload_queue.get_nowait()
            except Empty:
                return
            event = request["event"]
            plan = request["plan"]
            assert isinstance(event, threading.Event)
            old_manager = self._child_manager
            old_callback = client.on_child_command
            try:
                new_manager = ChildManager(self.store, plan) if plan is not None else None
                if new_manager is not None and self._entity_bridge is not None:
                    new_manager.set_entity_bridge(self._entity_bridge)
                if new_manager is not None:
                    access_token = client.online_access_token
                    if access_token is None:
                        raise RuntimeError("parent cloud session has no access token")
                    # 单 child 注册失败由 ChildManager 容错记录（error 日志）；
                    # 整体仍采纳新 plan——否则 mapping 落盘与 runtime 永久不一致，
                    # 后续任何 reload（含从 UI 删除坏 child）都会重复失败死锁
                    if not new_manager.register_on_session(
                        client,
                        access_token,
                        integration_version=self._integration_version,
                        hardware_version=VIRTUAL_HARDWARE_VERSION,
                    ):
                        _LOGGER.warning(
                            "Gateway %s child mapping applied with registration failures "
                            "(see child error logs)",
                            self.serial,
                        )
                else:
                    access_token = client.online_access_token
                    if access_token is None:
                        raise RuntimeError("parent cloud session has no access token")
                    client.on_child_command = None
                if old_manager is not None:
                    old_manager.prune_removed_children(client, access_token, plan)
                self._child_manager = new_manager
                _LOGGER.info("Gateway %s child mapping reload completed", self.serial)
            except Exception as error:  # noqa: BLE001 - preserve old plan
                client.on_child_command = old_callback
                request["error"] = error
                _LOGGER.error("Gateway %s child mapping reload failed: %s", self.serial, error)
            finally:
                event.set()

    def set_entity_bridge(self, entity_bridge) -> None:
        """Attach the HA adapter used by child state/control handling."""

        self._entity_bridge = entity_bridge
        if self._child_manager is not None:
            self._child_manager.set_entity_bridge(entity_bridge)

    def enqueue_child_state(self, child_key: str, state: dict[str, dict[str, object]]) -> bool:
        """Queue a state snapshot produced by Home Assistant."""

        if self._child_manager is not None:
            return self._child_manager.enqueue_state_snapshot(child_key, state)
        return False

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def pin(self) -> str | None:
        with self._lock:
            if self._pin and time.monotonic() < self._pin_expires_at:
                return self._pin
            return None

    @property
    def pairing_expired(self) -> bool:
        with self._lock:
            return bool(self._pin and time.monotonic() >= self._pin_expires_at)

    @property
    def pairing_in_progress(self) -> bool:
        """App 已通过 PIN 开始 SPEKE 会话；此时 PIN 过期也不得中断配对."""
        with self._lock:
            responder = self._pairing_responder
        return responder is not None and responder.has_active_session

    @property
    def descriptor(self) -> DeviceDescriptor:
        return DeviceDescriptor(
            serial_number=self.serial,
            device_type=self.config.device_type,
            manufacturer=self.config.manufacturer,
            product_id=self.config.product_id,
            protocol_type=self.config.protocol_type,
            services=(
                ServiceDescriptor("netInfo", "netInfo"),
                ServiceDescriptor("policy", "kitAuth"),
                ServiceDescriptor("speke", "speke"),
                ServiceDescriptor("cloudSetup", "cloudSetup"),
            ),
        )

    @property
    def device_info(self) -> dict[str, str]:
        return {
            "name": GATEWAY_NAME,
            "manufacturer": GATEWAY_MANUFACTURER,
            "model": self.config.model,
            "device_type": GATEWAY_DEVICE_TYPE_NAME,
            "product_id": self.config.product_id,
        }

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                continue

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self._status = status
        if error:
            _LOGGER.error("Gateway %s entered %s: %s", self.serial, status, error)
        self._notify()

    def start(self) -> None:
        """Start bound cloud service or the temporary pairing service."""

        self._stopped = False
        state = self.store.read_json("cloud/material.json")
        if state and isinstance(state.get("devId"), str) and state.get("secret"):
            self.start_cloud_from_state(state)
            return

        binding = self._load_binding()
        if binding is not None:
            self._binding = binding
            self.start_cloud_from_binding()
            return

        self.start_pairing()

    def has_bound_material(self) -> bool:
        """Return whether this gateway has enough material for bound login."""

        state = self.store.read_json("cloud/material.json")
        return bool(
            state
            and isinstance(state.get("devId"), str)
            and isinstance(state.get("secret"), str)
            and state["devId"]
            and state["secret"]
        )

    def start_pairing(self) -> None:
        if self._stopped:
            self._stopped = False
        self._create_pairing_ticket()
        self._set_status(GATEWAY_STATUS_PAIRING)
        try:
            self.broker.register(self)
        except PairingBusyError as error:
            self._set_status(GATEWAY_STATUS_ERROR, str(error))

    def stop(self) -> None:
        self._stopped = True
        self.broker.unregister(self)
        self._cloud_stop.set()
        thread = self._cloud_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10.0)
        self._clear_pairing_ticket()
        self._set_status(GATEWAY_STATUS_OFFLINE)

    def handle_pairing_datagram(
        self, packet: bytes, peer_host: str
    ) -> tuple[bytes | None, PairingResult]:
        responder = self._pairing_responder
        if responder is None:
            return None, PairingResult(kind="pairing_not_ready", packet_length=len(packet))
        return responder.handle(packet, peer_host)

    def on_pairing_listener_started(self) -> None:
        self._notify()

    def on_pairing_listener_stopped(self) -> None:
        self._notify()

    def on_pairing_error(self, error: Exception) -> None:
        self._set_status(GATEWAY_STATUS_ERROR, f"pairing listener: {error}")

    def on_pairing_expired(self) -> None:
        self._clear_pairing_ticket()
        self._set_status(GATEWAY_STATUS_OFFLINE, "pairing PIN expired")

    def consume_pairing_complete(self) -> bool:
        with self._lock:
            value = self._pairing_complete_pending
            self._pairing_complete_pending = False
            return value

    def start_cloud_from_binding(self) -> None:
        if self._binding is None:
            self._set_status(GATEWAY_STATUS_ERROR, "CloudSetup binding material is missing")
            return
        self._start_cloud(bound_state=None, binding=self._binding)

    def start_cloud_from_state(self, state: dict[str, object]) -> None:
        self._start_cloud(bound_state=state, binding=None)

    def _start_cloud(
        self,
        *,
        bound_state: dict[str, object] | None,
        binding: CloudSetupBindingInfo | None,
    ) -> None:
        with self._lock:
            if self._cloud_thread is not None and self._cloud_thread.is_alive():
                return
            self._cloud_stop.clear()
        self._clear_pairing_ticket()
        self._set_status(GATEWAY_STATUS_REGISTERING)

        def worker() -> None:
            try:
                integration_version = _read_integration_version()
            except Exception as error:  # noqa: BLE001 - surfaced as gateway state
                self._set_status(GATEWAY_STATUS_ERROR, f"invalid integration manifest: {error}")
                return
            self._integration_version = integration_version
            reconnect_delay = RECONNECT_INITIAL_DELAY_SECONDS
            while not self._cloud_stop.is_set():
                ca_path = _ensure_huawei_trust_store(self.store)
                session_state = bound_state
                if bound_state is not None:
                    session_state = self.store.read_json("cloud/material.json") or bound_state
                client = CloudM2MClient(
                    binding=binding,
                    serial_number=self.serial,
                    mac=self.config.mac,
                    interface_ip=self.config.interface_ip,
                    material_path=self.store.path("cloud/material.json"),
                    on_online=lambda: self._set_status(GATEWAY_STATUS_ONLINE),
                    on_cloud_ready=lambda ready_client, access_token: self._on_cloud_ready(
                        ready_client,
                        access_token,
                        integration_version,
                    ),
                    on_worker_tick=self._process_reload_requests,
                    device_model=self.config.model,
                    device_type=self.config.device_type,
                    manufacturer=self.config.manufacturer,
                    product_id=self.config.product_id,
                    protocol_type=self.config.protocol_type,
                    firmware_version=integration_version,
                    hardware_version=VIRTUAL_HARDWARE_VERSION,
                    software_version=integration_version,
                    cloud_primary_url=(
                        session_state.get("cloudPrimaryUrl")
                        if session_state
                        else None
                    ),
                    cloud_standby_url=(
                        session_state.get("cloudStandbyUrl")
                        if session_state
                        else None
                    ),
                    ca_json=ca_path,
                )
                self._active_client = client
                if session_state is None:
                    result = client.run(stop_event=self._cloud_stop)
                else:
                    result = client.run_bound(session_state, self._cloud_stop)
                self._active_client = None
                if self._cloud_stop.is_set():
                    self._set_status(GATEWAY_STATUS_OFFLINE)
                    break
                if result.registered:
                    self._set_status(GATEWAY_STATUS_ONLINE)
                    break

                if bound_state is None:
                    _LOGGER.error(
                        "Gateway %s cloud session failed at %s: %s",
                        self.serial,
                        result.stage,
                        result.error,
                    )
                    self._set_status(
                        GATEWAY_STATUS_ERROR,
                        result.error or f"cloud stage {result.stage}",
                    )
                    break

                if result.errcode in TERMINAL_CLOUD_ERRCODES:
                    _LOGGER.error(
                        "Gateway %s cloud identity was rejected at %s "
                        "(errcode=%s): %s",
                        self.serial,
                        result.stage,
                        result.errcode,
                        result.error,
                    )
                    self._set_status(
                        GATEWAY_STATUS_ERROR,
                        result.error or f"cloud errcode {result.errcode}",
                    )
                    break

                _LOGGER.warning(
                    "Gateway %s cloud session failed at %s; retrying in %.1fs: %s",
                    self.serial,
                    result.stage,
                    reconnect_delay,
                    result.error,
                )
                self._set_status(GATEWAY_STATUS_OFFLINE)
                if self._cloud_stop.wait(reconnect_delay):
                    self._set_status(GATEWAY_STATUS_OFFLINE)
                    break
                reconnect_delay = min(
                    reconnect_delay * 2,
                    RECONNECT_MAX_DELAY_SECONDS,
                )

        self._cloud_thread = threading.Thread(
            target=worker,
            name=f"oneconnect_bridge-cloud-{self.serial}",
            daemon=True,
        )
        self._cloud_thread.start()

    def _on_cloud_ready(
        self,
        client: CloudM2MClient,
        access_token: str,
        integration_version: str,
    ) -> None:
        manager = self._child_manager
        if manager is None:
            return
        manager.register_on_session(
            client,
            access_token,
            integration_version=integration_version,
            hardware_version=VIRTUAL_HARDWARE_VERSION,
        )

    def _create_pairing_ticket(self) -> None:
        pin = f"{secrets.randbelow(100_000_000):08d}"
        expires = time.monotonic() + PIN_TTL_SECONDS
        with self._lock:
            self._pin = pin
            self._pin_expires_at = expires
            self._pairing_complete_pending = False
            self._pairing_responder = LocalPairingResponder(
                self.descriptor,
                pin,
                discovery_response_message_id=lambda: 0x2774,
                on_cloud_setup_record=self._on_cloud_setup_record,
            )

    def _clear_pairing_ticket(self) -> None:
        with self._lock:
            self._pin = None
            self._pin_expires_at = 0.0
            self._pairing_responder = None
        self._notify()

    def _on_cloud_setup_record(
        self,
        info: CloudSetupBindingInfo | CloudSetupAuthInfo,
        encrypted_request: bytes,
        plaintext: bytes,
    ) -> None:
        del encrypted_request, plaintext
        if not isinstance(info, CloudSetupBindingInfo):
            self._set_status(GATEWAY_STATUS_ERROR, "CloudSetup returned auth-only material")
            return
        self._binding = info
        _LOGGER.debug("Gateway %s received CloudSetup binding for devId %s", self.serial, info.dev_id)
        self.store.write_json(
            "cloud/material.json",
            {
                "schema": "oneconnect_bridge.cloud-material.v2",
                "devId": info.dev_id,
                "psk": info.psk.hex().upper(),
                "code": info.code,
                "cloudPrimaryUrl": info.cloud_primary_url,
                "cloudStandbyUrl": info.cloud_standby_url,
            },
        )
        with self._lock:
            self._pairing_complete_pending = True

    def _load_binding(self) -> CloudSetupBindingInfo | None:
        value = self.store.read_json("cloud/material.json")
        if not value:
            return None
        try:
            return CloudSetupBindingInfo(
                dev_id=str(value["devId"]),
                psk=bytes.fromhex(str(value["psk"])),
                code=str(value["code"]),
                cloud_primary_url=str(value["cloudPrimaryUrl"]),
                cloud_standby_url=(
                    str(value["cloudStandbyUrl"])
                    if value.get("cloudStandbyUrl")
                    else None
                ),
            )
        except (KeyError, ValueError, TypeError):
            return None

class GatewayLimitError(RuntimeError):
    """网关实例数达到固定上限时抛出."""


class GatewayManager:
    """Own all configured gateway instances in one HA process."""

    def __init__(self) -> None:
        self.broker = PairingBroker()
        self._runtimes: dict[str, GatewayRuntime] = {}

    def create(self, config: GatewayConfig) -> GatewayRuntime:
        if config.serial in self._runtimes:
            return self._runtimes[config.serial]
        if len(self._runtimes) >= MAX_GATEWAY_INSTANCES:
            raise GatewayLimitError(
                f"OneConnect Bridge supports at most {MAX_GATEWAY_INSTANCES} "
                "gateway instances"
            )
        runtime = GatewayRuntime(config, self.broker)
        self._runtimes[config.serial] = runtime
        return runtime

    def remove(self, runtime: GatewayRuntime) -> None:
        self._runtimes.pop(runtime.serial, None)

    def get(self, serial: str) -> GatewayRuntime | None:
        return self._runtimes.get(serial)

def _ensure_huawei_trust_store(store: GatewayStore) -> Path | None:
    path = store.path("certificates/huawei_root_ca_trust.json")
    if path.is_file():
        try:
            read_ca_certificates(path)
            return path
        except Exception as error:  # noqa: BLE001 - stale cache is replaced below
            _LOGGER.warning("Invalid cached Huawei CA store %s: %s", path, error)
            try:
                path.unlink()
            except OSError:
                pass
    try:
        with urlopen(HUAWEI_ROOT_CA_URL, timeout=20) as response:
            raw = response.read()
        certificate = normalize_certificate(raw)
        store.write_json(
            "certificates/huawei_root_ca_trust.json",
            {
                "schema": "oneconnect_bridge.tls-trust.v1",
                "certificate_url": HUAWEI_ROOT_CA_URL,
                "certificates": [certificate],
            },
        )
        read_ca_certificates(path)
        return path
    except Exception as error:  # noqa: BLE001 - system CA fallback is valid
        _LOGGER.error("Unable to obtain a valid Huawei CA certificate: %s", error)
        return None