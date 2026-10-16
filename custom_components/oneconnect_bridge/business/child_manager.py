"""OneConnect Bridge 子设备注册与持久化."""
from __future__ import annotations

import logging
import secrets
from threading import RLock
from typing import Any

from ..const import MAX_CHILD_DEVICES_PER_GATEWAY
from ..core.cloud_runtime import (
    CloudM2MClient,
    CloudRuntimeError,
    OnlineChildCommand,
)
from ..storage import GatewayStore
from .child_protocol import (
    ChildIdentity,
    ChildMaterial,
    ChildProtocolError,
    build_batch_bind_request,
    build_batch_delete_request,
    build_child_command_response,
    build_data_sync_request,
    build_dev_info_sync_request,
    build_room_update_request,
    build_status_sync_request,
    decode_child_command,
    material_from_storage,
    parse_batch_bind_response,
)
from .models import GatewayPlan

_LOGGER = logging.getLogger(__name__)

class ChildManager:
    """Register desired children through the already authenticated parent session."""

    def __init__(self, store: GatewayStore, plan: GatewayPlan) -> None:
        self.store = store
        self.plan = plan
        self._materials: dict[str, tuple[object, ChildIdentity, ChildMaterial]] = {}
        self._entity_bridge = None
        self._data_sequence = 0x2E
        self._lock = RLock()
        self._client: CloudM2MClient | None = None
        self._access_token: str | None = None
        self._pending_state: dict[str, dict[str, dict[str, object]]] = {}

    def set_entity_bridge(self, entity_bridge) -> None:
        """Attach the HA adapter used for initial state and commands."""

        self._entity_bridge = entity_bridge

    def register_on_session(
        self,
        client: CloudM2MClient,
        access_token: str,
        *,
        integration_version: str,
        hardware_version: str,
    ) -> bool:
        """Reconcile all declared children before parent heartbeat starts."""

        if len(self.plan.children) > MAX_CHILD_DEVICES_PER_GATEWAY:
            _LOGGER.error(
                "Gateway %s child mapping has %d children; maximum is %d",
                client.serial_number,
                len(self.plan.children),
                MAX_CHILD_DEVICES_PER_GATEWAY,
            )
            return False

        client.on_child_command = self.handle_cloud_command
        with self._lock:
            self._client = client
            self._access_token = access_token

        success = True
        for child in self.plan.children:
            try:
                self._register_one(
                    client,
                    access_token,
                    child,
                    integration_version=integration_version,
                    hardware_version=hardware_version,
                )
            except Exception as error:  # noqa: BLE001 - one child must not kill parent
                success = False
                _LOGGER.error(
                    "Child %s for gateway %s failed during registration: %s",
                    child.key,
                    client.serial_number,
                    error,
                )
        self.prune_orphans(client, access_token)
        return success

    def prune_orphans(self, client: CloudM2MClient, access_token: str) -> None:
        """删除存储中有而计划中没有的云子设备：计划是唯一事实，先云后本地（父会话就绪后调用)."""

        plan_keys = {child.key for child in self.plan.children}
        stored = self.store.read_child_materials()
        for key in sorted(set(stored) - plan_keys):
            material_data = stored[key]
            if not isinstance(material_data, dict) or material_data.get("devId") is None:
                self.store.remove_child(key)
                continue
            try:
                material = material_from_storage(material_data)
            except (KeyError, ValueError, TypeError):
                self.store.remove_child(key)
                continue
            response = client.request(
                "child_batch_delete_removed",
                build_batch_delete_request(
                    access_token=access_token,
                    token=secrets.token_bytes(2),
                    material=material,
                ),
            )
            if response.get("errcode") != 0:
                _LOGGER.warning(
                    "Orphan child %s delete returned %s",
                    key,
                    response.get("errcode"),
                )
                continue
            self.store.remove_child(key)
            _LOGGER.info(
                "Pruned orphan child %s from gateway %s", key, client.serial_number
            )

    def _register_one(
        self,
        client: CloudM2MClient,
        access_token: str,
        child,
        *,
        integration_version: str,
        hardware_version: str,
    ) -> None:
        stored_identity = self.store.read_json(f"children/{child.key}/identity.json")
        if (
            stored_identity is not None
            and stored_identity.get("product_id") != child.product.prod_id
        ):
            if child.on_product_change != "replace":
                raise ChildProtocolError(
                    f"stored identity for {child.key} changed product from "
                    f"{stored_identity.get('product_id')} to {child.product.prod_id}; "
                    "set lifecycle.on_product_change to replace"
                )
            self._replace_one(
                client,
                access_token,
                child,
                integration_version=integration_version,
                hardware_version=hardware_version,
                old_identity=stored_identity,
            )
            return

        identity = self._load_or_create_identity(child)
        material_path = f"children/{child.key}/material.json"
        stored = self.store.read_json(material_path)
        material: ChildMaterial | None = None
        if stored is not None:
            material = material_from_storage(stored)
            if material.serial_number != identity.serial_number or material.udid != identity.udid:
                raise ChildProtocolError(
                    f"stored material for {child.key} does not match its identity"
                )

        if material is None:
            response = client.request(
                "child_batch_bind",
                build_batch_bind_request(
                    access_token=access_token,
                    token=secrets.token_bytes(2),
                    identity=identity,
                    plan=child,
                    firmware_version=integration_version,
                    hardware_version=hardware_version,
                    software_version=integration_version,
                ),
            )
            material = parse_batch_bind_response(response, identity=identity)
            self.store.write_json(
                material_path,
                material.as_storage(
                    product_id=identity.product_id,
                    sub_product_id=identity.sub_product_id,
                ),
            )

        self._activate_child(
            client,
            access_token,
            child,
            identity,
            material,
            integration_version=integration_version,
            hardware_version=hardware_version,
        )

    def _activate_child(
        self,
        client: CloudM2MClient,
        access_token: str,
        child,
        identity: ChildIdentity,
        material: ChildMaterial,
        *,
        integration_version: str,
        hardware_version: str,
    ) -> None:
        """Publish identity and initial state for one already-bound child."""

        with self._lock:
            self._materials[material.dev_id] = (child, identity, material)
        self._flush_pending_state(child.key, client)

        dev_info = client.request(
            "child_dev_info_sync",
            build_dev_info_sync_request(
                access_token=access_token,
                token=secrets.token_bytes(2),
                identity=identity,
                material=material,
                plan=child,
                firmware_version=integration_version,
                hardware_version=hardware_version,
                software_version=integration_version,
            ),
        )
        if dev_info.get("errcode") != 0:
            raise CloudRuntimeError(
                f"child {child.key} devInfoSync errcode {dev_info.get('errcode')}"
            )

        # 实测 Python/C 在子设备受理时 statusSync 返回 1000：保留应答供诊断，不拒绝
        status = client.request(
            "child_status_sync",
            build_status_sync_request(
                access_token=access_token,
                token=secrets.token_bytes(2),
                material=material,
            ),
        )
        _LOGGER.info(
            "Child %s registered for gateway %s (devInfoSync=%s, statusSync=%s)",
            child.key,
            client.serial_number,
            dev_info.get("errcode"),
            status.get("errcode"),
        )
        if child.room:
            try:
                room_response = client.request(
                    "child_room_updated",
                    build_room_update_request(
                        access_token=access_token,
                        token=secrets.token_bytes(2),
                        material=material,
                        room=child.room,
                    ),
                )
                errcode = room_response.get("errcode")
                if errcode == 1000:
                    # 实测子设备受理类应答返回 1000（同 statusSync）：已受理，非错误
                    _LOGGER.debug(
                        "Child %s room update accepted (async, 1000) room=%s",
                        child.key,
                        child.room,
                    )
                elif errcode != 0:
                    _LOGGER.warning(
                        "Child %s room update returned %s",
                        child.key,
                        errcode,
                    )
                else:
                    _LOGGER.debug(
                        "Child %s room update accepted room=%s",
                        child.key,
                        child.room,
                    )
            except Exception as error:  # noqa: BLE001 - room metadata must not kill child
                _LOGGER.warning(
                    "Child %s room update failed: %s",
                    child.key,
                    error,
                )
        if self._entity_bridge is not None:
            initial_state = self._entity_bridge.read_child_state(child)
            _LOGGER.debug(
                "Child %s initial HA state before dataSync: %s",
                child.key,
                initial_state,
            )
            for service_id, data in initial_state.items():
                try:
                    _LOGGER.debug(
                        "Child %s sending initial dataSync sid=%s data=%s",
                        child.key,
                        service_id,
                        data,
                    )

                    response = client.request(
                        f"child_data_sync_{service_id}",
                        build_data_sync_request(
                            access_token=access_token,
                            token=secrets.token_bytes(2),
                            child=child,
                            material=material,
                            service_id=service_id,
                            data=data,
                            sequence=self._next_data_sequence(),
                        ),
                    )
                    if response.get("errcode") != 0:
                        _LOGGER.warning(
                            "Child %s initial %s dataSync returned %s",
                            child.key,
                            service_id,
                            response.get("errcode"),
                        )
                    else:
                        _LOGGER.debug(
                            "Child %s initial dataSync accepted sid=%s",
                            child.key,
                            service_id,
                        )
                except Exception as error:  # noqa: BLE001 - state is best effort
                    _LOGGER.warning(
                        "Child %s initial %s state sync failed: %s",
                        child.key,
                        service_id,
                        error,
                    )

    def _replace_one(
        self,
        client: CloudM2MClient,
        access_token: str,
        child,
        *,
        integration_version: str,
        hardware_version: str,
        old_identity: dict[str, Any],
    ) -> None:
        """Register the new model first, then remove the old cloud child."""

        old_material_data = self.store.read_json(f"children/{child.key}/material.json")
        if old_material_data is None:
            raise ChildProtocolError(
                f"cannot replace {child.key}: old child material is missing"
            )
        old_material = material_from_storage(old_material_data)

        replacement_identity_path = f"children/{child.key}/replacement/identity.json"
        replacement_material_path = f"children/{child.key}/replacement/material.json"
        pending_identity = self.store.read_json(replacement_identity_path)
        if pending_identity is None:
            new_identity = ChildIdentity(
                serial_number=secrets.token_hex(12).upper(),
                udid=secrets.token_hex(32),
                product_id=child.product.prod_id,
                sub_product_id=child.product.sub_prod_id,
            )
            pending_identity = {
                "schema": "oneconnect_bridge.child-identity.v1",
                "product_id": new_identity.product_id,
                "sub_product_id": new_identity.sub_product_id,
                "serial_number": new_identity.serial_number,
                "udid": new_identity.udid,
            }
            self.store.write_json(replacement_identity_path, pending_identity)
        else:
            new_identity = ChildIdentity(
                serial_number=str(pending_identity["serial_number"]),
                udid=str(pending_identity["udid"]),
                product_id=str(pending_identity["product_id"]),
                sub_product_id=str(pending_identity["sub_product_id"]),
            )
        if new_identity.product_id != child.product.prod_id:
            raise ChildProtocolError(
                f"replacement identity for {child.key} targets {new_identity.product_id}"
            )

        pending_material_data = self.store.read_json(replacement_material_path)
        if pending_material_data is None:
            bind_response = client.request(
                "child_batch_bind_replacement",
                build_batch_bind_request(
                    access_token=access_token,
                    token=secrets.token_bytes(2),
                    identity=new_identity,
                    plan=child,
                    firmware_version=integration_version,
                    hardware_version=hardware_version,
                    software_version=integration_version,
                ),
            )
            new_material = parse_batch_bind_response(bind_response, identity=new_identity)
            self.store.write_json(
                replacement_material_path,
                new_material.as_storage(
                    product_id=new_identity.product_id,
                    sub_product_id=new_identity.sub_product_id,
                ),
            )
        else:
            new_material = material_from_storage(pending_material_data)

        self._activate_child(
            client,
            access_token,
            child,
            new_identity,
            new_material,
            integration_version=integration_version,
            hardware_version=hardware_version,
        )

        delete_response = client.request(
            "child_batch_delete_replaced",
            build_batch_delete_request(
                access_token=access_token,
                token=secrets.token_bytes(2),
                material=old_material,
            ),
        )
        if delete_response.get("errcode") != 0:
            self.store.write_json(
                f"children/{child.key}/replacement.json",
                {
                    "schema": "oneconnect_bridge.replacement.v1",
                    "stage": "old_delete_pending",
                    "old_product_id": old_identity.get("product_id"),
                    "new_product_id": new_identity.product_id,
                    "old_dev_id": old_material.dev_id,
                    "new_dev_id": new_material.dev_id,
                    "errcode": delete_response.get("errcode"),
                },
            )
            raise CloudRuntimeError(
                f"old child delete returned {delete_response.get('errcode')}"
            )

        self.store.write_json(
            f"children/{child.key}/identity.json",
            pending_identity,
        )
        self.store.write_json(
            f"children/{child.key}/material.json",
            new_material.as_storage(
                product_id=new_identity.product_id,
                sub_product_id=new_identity.sub_product_id,
            ),
        )
        for relative in (replacement_identity_path, replacement_material_path,
                         f"children/{child.key}/replacement.json"):
            path = self.store.path(relative)
            if path.is_file():
                path.unlink()
        _LOGGER.info(
            "Child %s replaced product %s with %s",
            child.key,
            old_identity.get("product_id"),
            new_identity.product_id,
        )

    def prune_removed_children(
        self,
        client: CloudM2MClient,
        access_token: str,
        desired_plan: GatewayPlan | None,
    ) -> None:
        """Delete old child entries absent from the newly loaded YAML plan."""

        desired_keys = (
            {child.key for child in desired_plan.children}
            if desired_plan is not None
            else set()
        )
        old_children = {child.key: child for child in self.plan.children}
        for child_key in sorted(set(old_children) - desired_keys):
            material_record = next(
                (
                    record
                    for record in self._materials.values()
                    if record[0].key == child_key
                ),
                None,
            )
            if material_record is None:
                material_data = self.store.read_json(
                    f"children/{child_key}/material.json"
                )
                if material_data is None:
                    # 该 child 从未 batchBind 成功（云端本就无设备）：
                    # 清掉本地残留后继续，若抛错会死锁全部 reload（含删除操作）
                    self.store.remove_child(child_key)
                    _LOGGER.info(
                        "Removed child %s had no cloud material; local traces cleared",
                        child_key,
                    )
                    continue
                material = material_from_storage(material_data)
            else:
                material = material_record[2]
            response = client.request(
                "child_batch_delete_removed",
                build_batch_delete_request(
                    access_token=access_token,
                    token=secrets.token_bytes(2),
                    material=material,
                ),
            )
            if response.get("errcode") != 0:
                raise CloudRuntimeError(
                    f"removed child {child_key} delete returned "
                    f"{response.get('errcode')}"
                )
            self.store.remove_child(child_key)
            _LOGGER.info(
                "Removed child %s from cloud and gateway storage", child_key
            )

    def handle_cloud_command(
        self, client: CloudM2MClient, message
    ) -> OnlineChildCommand | None:
        """Convert one child M2M command into HA work and an ACK."""

        try:
            command = decode_child_command(
                message, known_dev_ids=set(self._materials)
            )
        except ChildProtocolError as error:
            _LOGGER.warning("Unable to decode child cloud command: %s", error)
            return None
        if command is None:
            return None
        if client.online_access_token is None:
            return None
        child, _identity, _material = self._materials[command.dev_id]
        if self._entity_bridge is not None:
            def post_ack(ready_client: CloudM2MClient) -> None:
                try:
                    self._entity_bridge.apply_child_command(
                        child, command.service_id, command.payload
                    )
                    state = self._entity_bridge.read_child_state(child)
                    self.enqueue_state_snapshot(
                        child.key, state, client=ready_client
                    )
                except Exception as error:  # noqa: BLE001 - ACK already returned
                    _LOGGER.error(
                        "HA command handling failed for child %s: %s",
                        child.key,
                        error,
                    )
        else:
            post_ack = None
        response = build_child_command_response(
            command=command,
            access_token=client.online_access_token,
            sequence=client.next_child_response_sequence(),
        )
        _LOGGER.debug(
            "Child command ACK ready child=%s sid=%s",
            child.key,
            command.service_id,
        )
        return OnlineChildCommand(response=response, post_ack=post_ack)

    def enqueue_state_snapshot(
        self,
        child_key: str,
        state: dict[str, dict[str, object]],
        *,
        client: CloudM2MClient | None = None,
    ) -> bool:
        """Queue an HA-originated state snapshot on the parent cloud worker."""

        with self._lock:
            active_client = client or self._client
            access_token = self._access_token
            child = next((item for item in self.plan.children if item.key == child_key), None)
            material_record = next(
                (record for record in self._materials.values() if record[0].key == child_key),
                None,
            )
        if active_client is None or not access_token or child is None or material_record is None:
            with self._lock:
                self._pending_state[child_key] = state
            _LOGGER.debug(
                "Skipping HA state snapshot child=%s: cloud child session is not ready",
                child_key,
            )
            return False
        material = material_record[2]
        requests = []
        for service_id, data in state.items():
            requests.append(
                build_data_sync_request(
                    access_token=access_token,
                    token=secrets.token_bytes(2),
                    child=child,
                    material=material,
                    service_id=service_id,
                    data=data,
                    sequence=self._next_data_sequence(),
                )
            )
        active_client.enqueue_child_state_requests(requests)
        _LOGGER.debug(
            "Queued HA state snapshot child=%s services=%s",
            child_key,
            list(state),
        )
        return True

    def _flush_pending_state(self, child_key: str, client: CloudM2MClient) -> None:
        with self._lock:
            state = self._pending_state.pop(child_key, None)
        if state:
            self.enqueue_state_snapshot(child_key, state, client=client)

    def _next_data_sequence(self) -> bytes:
        value = bytes((self._data_sequence,))
        self._data_sequence = (self._data_sequence + 1) & 0xFF
        return value

    def _load_or_create_identity(self, child) -> ChildIdentity:
        relative = f"children/{child.key}/identity.json"
        stored = self.store.read_json(relative)
        if stored is None:
            identity = ChildIdentity(
                serial_number=secrets.token_hex(12).upper(),
                udid=secrets.token_hex(32),
                product_id=child.product.prod_id,
                sub_product_id=child.product.sub_prod_id,
            )
            self.store.write_json(
                relative,
                {
                    "schema": "oneconnect_bridge.child-identity.v1",
                    "product_id": identity.product_id,
                    "sub_product_id": identity.sub_product_id,
                    "serial_number": identity.serial_number,
                    "udid": identity.udid,
                },
            )
            return identity

        try:
            identity = ChildIdentity(
                serial_number=str(stored["serial_number"]),
                udid=str(stored["udid"]),
                product_id=str(stored["product_id"]),
                sub_product_id=str(stored["sub_product_id"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ChildProtocolError(f"stored identity for {child.key} is incomplete") from error
        if identity.product_id != child.product.prod_id:
            raise ChildProtocolError(
                f"child {child.key} changed product from {identity.product_id} "
                f"to {child.product.prod_id}; remove the old child first"
            )
        return identity