from __future__ import annotations

import re
import time
from typing import Any, Mapping

from .billing import usage_totals
from .xui_api import XuiClient


class UsageSyncService:
    def __init__(self, db, provisioning, client_factory=XuiClient, now=time.time):
        self.db = db
        self.provisioning = provisioning
        self.client_factory = client_factory
        self.now = now

    def sync_all(self) -> dict[str, Any]:
        result = {"synced": 0, "errors": [], "disabled": 0}
        panels = {int(panel["id"]): panel for panel in self.db.list_panels() if panel.get("enabled")}
        clients_by_panel: dict[int, list[dict[str, Any]]] = {}
        for managed in self.db.list_managed_clients(states=["provisioned"]):
            if managed["panel_id"] in panels:
                clients_by_panel.setdefault(int(managed["panel_id"]), []).append(managed)

        managed_user_ids = {int(item["user_id"]) for items in clients_by_panel.values() for item in items}
        for panel_id, managed_clients in clients_by_panel.items():
            panel = panels[panel_id]
            try:
                remote = self._client_for_panel(panel)
                remote.login()
                inbounds = remote.list_inbounds()
                stats = self._stats_by_email(inbounds)
            except Exception as exc:  # noqa: BLE001
                result["errors"].append({"panel_id": panel_id, "error": self._safe_error(exc, panel)})
                continue

            for managed in managed_clients:
                stat = stats.get(str(managed["remote_email"]).strip().lower())
                if not stat:
                    continue
                self.db.advance_usage_ledger(
                    managed["id"],
                    int(stat.get("up") or 0),
                    int(stat.get("down") or 0),
                    float(managed.get("rate") or 1),
                )
                result["synced"] += 1

        for user_id in sorted(managed_user_ids):
            result["disabled"] += self.enforce_user(user_id).get("disabled", 0)
        return result

    def sync_user(self, user_id: int) -> dict[str, Any]:
        before = set(item["id"] for item in self.db.list_managed_clients(user_id=user_id))
        result = self.sync_all()
        result["user_id"] = int(user_id)
        result["managed_clients"] = len(before)
        return result

    def enforce_user(self, user_id: int) -> dict[str, int]:
        user = self.db.get_user(user_id)
        if not user:
            raise ValueError("user not found")

        totals = usage_totals(self.db, user_id)
        used = totals["upload"] + totals["download"]
        quota = int(user.get("quota_bytes") or 0)
        expire_at = int(user.get("expire_at") or 0)
        should_disable = user.get("status") != "active"
        should_disable = should_disable or bool(expire_at and expire_at <= int(self.now()))
        should_disable = should_disable or bool(quota and used >= quota)

        if not should_disable:
            return {"disabled": 0}

        enabled_clients = [
            item
            for item in self.db.list_managed_clients(user_id=user_id)
            if item.get("desired_enabled") or item.get("remote_enabled")
        ]
        if not enabled_clients:
            return {"disabled": 0}
        self.provisioning.set_user_enabled(user_id, False)
        return {"disabled": len(enabled_clients)}

    def _client_for_panel(self, panel: dict[str, Any]):
        return self.client_factory(
            panel["base_url"],
            panel.get("username", ""),
            panel.get("password", ""),
            bool(panel.get("verify_tls", True)),
        )

    def _stats_by_email(self, inbounds: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for inbound in inbounds:
            for stat in inbound.get("clientStats") or []:
                if not isinstance(stat, Mapping):
                    continue
                email = str(stat.get("email") or "").strip().lower()
                if not email:
                    continue
                current = stats.setdefault(email, {"email": email, "up": 0, "down": 0})
                current["up"] += int(stat.get("up") or 0)
                current["down"] += int(stat.get("down") or 0)
        return stats

    def _safe_error(self, exc: Exception, panel: dict[str, Any] | None = None) -> str:
        message = str(exc or "sync failed")
        if panel:
            for secret in (panel.get("password"), panel.get("username")):
                if secret:
                    message = message.replace(str(secret), "[redacted]")
        message = re.sub(r"(?i)(password|secret|token|cookie)[^\s,;]*", "[redacted]", message)
        return message[:200]
