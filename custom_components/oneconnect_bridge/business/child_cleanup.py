"""OneConnect Bridge 子设备清理."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import secrets
from typing import Any, Mapping

from ..core.cloud_runtime import CloudM2MClient, CloudRuntimeError
from ..core.m2m import build_bridge_login_request, build_bridge_revoke_request
from .child_protocol import (
    build_batch_delete_request,
    material_from_storage,
)

_LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True)
class ChildCleanupResult:
    """Results for child deletion requests sent before parent revoke."""

    deleted: tuple[str, ...]
    failed: tuple[str, ...]

class ChildCleanupClient(CloudM2MClient):
    """Delete children and then revoke their parent over one cloud session."""

    def remove_children_then_revoke(
        self,
        parent_state: dict[str, object],
        child_materials: Mapping[str, dict[str, Any]],
    ) -> ChildCleanupResult:
        """Delete all stored children before revoking the parent gateway."""

        try:
            return self._run_remove_children_then_revoke(
                parent_state,
                child_materials,
            )
        except Exception as error:  # noqa: BLE001 - caller must still remove local data
            self.trace.record(
                "cloud_parent_removal_failed",
                stage=self._stage,
                error_type=type(error).__name__,
                error=str(error),
            )
            if isinstance(error, CloudRuntimeError):
                raise
            raise CloudRuntimeError(f"{type(error).__name__}: {error}") from error
        finally:
            if self._socket is not None:
                try:
                    self._socket.close()
                finally:
                    self._socket = None

    def _run_remove_children_then_revoke(
        self,
        parent_state: dict[str, object],
        child_materials: Mapping[str, dict[str, Any]],
    ) -> ChildCleanupResult:
        parent_dev_id = parent_state.get("devId")
        secret = parent_state.get("secret")
        if not isinstance(parent_dev_id, str) or not parent_dev_id:
            raise CloudRuntimeError("registered cloud state has no devId")
        if not isinstance(secret, str) or not secret:
            raise CloudRuntimeError("registered cloud state has no secret")
        if not self.cloud_primary_url:
            raise CloudRuntimeError("registered cloud state has no primary URL")

        self._active_dev_id = parent_dev_id
        self._stage = "tls_connect"
        self._connect()

        self._stage = "login_for_parent_removal"
        login = self._request(
            "login_for_parent_removal",
            build_bridge_login_request(
                dev_id=parent_dev_id,
                secret=secret,
                token=secrets.token_bytes(2),
            ),
        )
        if self._require_errcode(login, "login_for_parent_removal") != 0:
            raise CloudRuntimeError("cloud re-login for parent removal was rejected")
        access_token = login.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            raise CloudRuntimeError("parent removal login has no accessToken")
        self._online_access_token = access_token

        deleted: list[str] = []
        failed: list[str] = []
        for child_key in sorted(child_materials):
            try:
                material = material_from_storage(child_materials[child_key])
                response = self._request(
                    "child_batch_delete",
                    build_batch_delete_request(
                        access_token=access_token,
                        token=secrets.token_bytes(2),
                        material=material,
                    ),
                )
                errcode = self._require_errcode(response, "child_batch_delete")
                if errcode != 0:
                    raise CloudRuntimeError(
                        f"child deletion returned errcode {errcode}"
                    )
            except Exception as error:  # noqa: BLE001 - parent removal must continue
                failed.append(child_key)
                _LOGGER.warning(
                    "Gateway %s child %s cloud deletion failed: %s",
                    self.serial_number,
                    child_key,
                    error,
                )
                continue
            deleted.append(child_key)
            _LOGGER.debug(
                "Gateway %s child %s deleted from cloud",
                self.serial_number,
                child_key,
            )

        self._stage = "revoke_parent_after_child_cleanup"
        revoke = build_bridge_revoke_request(
            access_token=access_token,
            token=secrets.token_bytes(2),
            sequence_number=2,
        )
        response = self._request(
            "revoke_parent_after_child_cleanup",
            revoke,
            require_json=False,
        )
        errcode = response.get("errcode")
        if isinstance(errcode, int) and errcode not in (0, 6):
            raise CloudRuntimeError(f"cloud parent revoke errcode {errcode}")

        self.trace.record(
            "cloud_parent_removal_accepted",
            deleted_child_count=len(deleted),
            failed_child_count=len(failed),
            revoke_errcode=errcode,
        )
        return ChildCleanupResult(tuple(deleted), tuple(failed))