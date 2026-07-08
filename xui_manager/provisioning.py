from __future__ import annotations

import re
import time
import logging
from typing import Any

from .vless import eligible_managed_nodes, group_managed_targets, validate_target_nodes
from .xui_api import XuiClient


logger = logging.getLogger(__name__)


class ProvisioningService:
    def __init__(self, db, client_factory=XuiClient, now=time.time):
        self.db = db
        self.client_factory = client_factory
        self.now = now

    def provision_user(self, user_id: int) -> dict[str, int]:
        user = self._active_user(user_id)
        targets = self._eligible_targets(user)
        for target in targets:
            self._provision_target(user, target, enabled=True)
        return self.status_for_user(user_id)

    def retry_user(self, user_id: int) -> dict[str, int]:
        return self.provision_user(user_id)

    def activate_purchased_plan(self, user_id: int) -> dict[str, int]:
        self.set_user_enabled(user_id, False)
        return self.provision_user(user_id)

    def set_user_enabled(self, user_id: int, enabled: bool) -> dict[str, int]:
        user = self.db.get_user(user_id)
        if not user:
            raise ValueError("user not found")
        for managed in self.db.list_managed_clients(user_id=user_id):
            self.db.set_managed_client_desired(
                managed["id"],
                enabled=enabled,
                expire_at=managed["desired_expire_at"],
            )
            target = {
                "panel": self._panels_by_id().get(managed["panel_id"]),
                "panel_id": managed["panel_id"],
                "inbound_id": managed["inbound_id"],
                "rate": managed["rate"],
                "flow": managed["flow"],
                "managed": self.db.get_managed_client(managed["id"]),
            }
            if target["panel"] and target["panel"].get("enabled"):
                self._apply_remote(target, enabled=enabled)
        return self.status_for_user(user_id)

    def delete_user(self, user_id: int) -> dict[str, Any]:
        user = self.db.get_user(user_id)
        if not user:
            raise ValueError("user not found")
        if user.get("role") == "admin" or user.get("status") != "disabled":
            raise ValueError("only disabled users can be deleted")
        panels = self._panels_by_id()
        errors: list[dict[str, Any]] = []
        for managed in self.db.list_managed_clients(user_id=user_id):
            panel = panels.get(int(managed["panel_id"]))
            try:
                if not panel:
                    raise ValueError("panel not found")
                remote = self._client_for_panel(panel)
                remote.login()
                remote.delete_vless_client(
                    inbound_id=managed["inbound_id"],
                    client_uuid=managed["client_uuid"],
                    email=managed["remote_email"],
                )
            except Exception as exc:  # noqa: BLE001
                error = self._sanitize_error(str(exc), panel)
                errors.append(
                    {
                        "panel_id": managed["panel_id"],
                        "panel_name": (panel or {}).get("name") or f"Panel {managed['panel_id']}",
                        "inbound_id": managed["inbound_id"],
                        "error": error,
                    }
                )
                logger.warning(
                    "Client deletion failed for panel_id=%s inbound_id=%s managed_client_id=%s: %s",
                    managed["panel_id"],
                    managed["inbound_id"],
                    managed["id"],
                    error,
                )
        if errors:
            return {"deleted": False, "errors": errors}
        self.db.delete_user(user_id)
        return {"deleted": True, "errors": []}

    def reconcile_user(self, user_id: int, apply: bool = False) -> dict[str, Any]:
        user = self._active_user(user_id)
        targets = self._eligible_targets(user)
        if apply:
            for target in targets:
                self._provision_target(user, target, enabled=True)
        return {"targets": len(targets), "apply": bool(apply), **self.status_for_user(user_id)}

    def status_for_user(self, user_id: int) -> dict[str, int]:
        clients = self._current_managed_clients(user_id)
        return {
            "provisioned": sum(1 for item in clients if item["state"] == "provisioned"),
            "failed": sum(1 for item in clients if item["state"] == "failed"),
            "pending": sum(1 for item in clients if item["state"] == "pending"),
        }

    def failure_details_for_user(self, user_id: int) -> list[dict[str, Any]]:
        panels = self._panels_by_id()
        failed = [client for client in self._current_managed_clients(user_id) if client["state"] == "failed"]
        details: list[dict[str, Any]] = []
        for client in failed:
            panel = panels.get(int(client["panel_id"])) or {}
            details.append(
                {
                    "panel_id": client["panel_id"],
                    "panel_name": panel.get("name") or f"Panel {client['panel_id']}",
                    "inbound_id": client["inbound_id"],
                    "remote_email": client["remote_email"],
                    "attempt_count": client["attempt_count"],
                    "last_attempt_at": client["last_attempt_at"],
                    "error": client["last_error"] or "provisioning failed",
                }
            )
        return details

    def _current_managed_clients(self, user_id: int) -> list[dict[str, Any]]:
        keys = self._current_target_keys(user_id)
        return [
            client
            for client in self.db.list_managed_clients(user_id=user_id)
            if (int(client["panel_id"]), int(client["inbound_id"])) in keys
        ]

    def _current_target_keys(self, user_id: int) -> set[tuple[int, int]]:
        user = self.db.get_user(user_id)
        if not user:
            return set()
        plan = self.db.get_plan(user.get("plan_id"))
        if not plan:
            return set()
        panels = self._panels_by_id()
        nodes = eligible_managed_nodes(self.db.list_nodes(enabled_only=True), plan["allowed_tags"])
        return {
            (int(node["panel_id"]), int(node["inbound_id"]))
            for node in nodes
            if panels.get(node["panel_id"], {}).get("enabled")
        }

    def _active_user(self, user_id: int) -> dict[str, Any]:
        user = self.db.get_user(user_id)
        if not user:
            raise ValueError("user not found")
        if user.get("status") != "active":
            raise ValueError("user is not active")
        if not self.db.get_plan(user["plan_id"]):
            raise ValueError("plan not found")
        return user

    def _eligible_targets(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        plan = self.db.get_plan(user["plan_id"])
        panels = self._panels_by_id()
        nodes = eligible_managed_nodes(self.db.list_nodes(enabled_only=True), plan["allowed_tags"])
        nodes = [node for node in nodes if panels.get(node["panel_id"], {}).get("enabled")]
        grouped = group_managed_targets(nodes)
        targets: list[dict[str, Any]] = []
        for (panel_id, inbound_id), target_nodes in grouped.items():
            rate, flow = validate_target_nodes(target_nodes)
            targets.append(
                {
                    "panel": panels[panel_id],
                    "panel_id": panel_id,
                    "inbound_id": inbound_id,
                    "rate": rate,
                    "flow": flow,
                    "nodes": target_nodes,
                }
            )
        return targets

    def _provision_target(self, user: dict[str, Any], target: dict[str, Any], *, enabled: bool) -> None:
        managed = self.db.ensure_managed_client(
            user["id"],
            target["panel_id"],
            target["inbound_id"],
            "vless",
            target["flow"],
            target["rate"],
            user["expire_at"],
        )
        self.db.set_managed_client_desired(managed["id"], enabled=enabled, expire_at=user["expire_at"])
        managed = self.db.get_managed_client(managed["id"])
        target = {**target, "managed": managed}
        self._apply_remote(target, enabled=enabled)

    def _apply_remote(self, target: dict[str, Any], *, enabled: bool) -> None:
        managed = target["managed"]
        try:
            remote = self._client_for_panel(target["panel"])
            remote.login()
            inbound = remote.get_inbound(target["inbound_id"])
            if not inbound:
                raise ValueError("inbound not found")
            if str(inbound.get("protocol") or "").lower() != "vless":
                raise ValueError("inbound protocol is not vless")
            existing = remote.find_client(inbound, managed["remote_email"])
            if existing and existing.get("id") != managed["client_uuid"]:
                raise ValueError("remote client conflict")
            if existing:
                stored = remote.update_vless_client(
                    inbound_id=target["inbound_id"],
                    client_uuid=managed["client_uuid"],
                    email=managed["remote_email"],
                    flow=managed["flow"],
                    expire_at=managed["desired_expire_at"],
                    enabled=enabled,
                )
            else:
                stored = remote.add_vless_client(
                    inbound_id=target["inbound_id"],
                    client_uuid=managed["client_uuid"],
                    email=managed["remote_email"],
                    flow=managed["flow"],
                    expire_at=managed["desired_expire_at"],
                )
                if not enabled:
                    stored = remote.update_vless_client(
                        inbound_id=target["inbound_id"],
                        client_uuid=managed["client_uuid"],
                        email=managed["remote_email"],
                        flow=managed["flow"],
                        expire_at=managed["desired_expire_at"],
                        enabled=False,
                    )
            if stored.get("id") != managed["client_uuid"]:
                raise ValueError("remote verification failed")
            self.db.update_managed_client_result(
                managed["id"],
                state="provisioned",
                remote_enabled=bool(stored.get("enable", enabled)),
                error="",
            )
        except Exception as exc:  # noqa: BLE001
            error = self._sanitize_error(str(exc), target["panel"])
            self.db.update_managed_client_result(
                managed["id"],
                state="failed",
                remote_enabled=False,
                error=error,
            )
            logger.warning(
                "Provisioning failed for panel_id=%s inbound_id=%s managed_client_id=%s: %s",
                target["panel_id"],
                target["inbound_id"],
                managed["id"],
                error,
            )

    def _client_for_panel(self, panel: dict[str, Any]):
        return self.client_factory(
            panel["base_url"],
            panel.get("username", ""),
            panel.get("password", ""),
            bool(panel.get("verify_tls", True)),
        )

    def _panels_by_id(self) -> dict[int, dict[str, Any]]:
        return {int(panel["id"]): panel for panel in self.db.list_panels()}

    def _sanitize_error(self, message: str, panel: dict[str, Any] | None = None) -> str:
        sanitized = str(message or "provisioning failed")
        if panel:
            for secret in (panel.get("password"), panel.get("username")):
                if secret:
                    sanitized = sanitized.replace(str(secret), "[redacted]")
        sanitized = re.sub(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b",
            "[redacted]",
            sanitized,
        )
        sanitized = re.sub(r"(?i)(password|secret|token|cookie)[^\s,;]*", "[redacted]", sanitized)
        return sanitized[:300]
