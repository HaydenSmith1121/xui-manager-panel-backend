from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import urllib.parse
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .billing import bytes_from_gb, usage_totals
from .db import Database
from .provisioning import ProvisioningService
from .subscription import Response, build_base64_subscription, build_clash_subscription, build_singbox_subscription
from .usage_sync import UsageSyncService
from .worker import PeriodicSyncWorker
from .xui_api import XuiClient



DEFAULT_STORE_PLANS = [
    {
        "name": "入门套餐",
        "quota_gb": 30,
        "duration_days": 30,
        "price_cents": 990,
        "description": "轻量使用，适合偶尔连接。",
    },
    {
        "name": "日常套餐",
        "quota_gb": 100,
        "duration_days": 30,
        "price_cents": 1990,
        "description": "日常使用，兼顾多设备连接。",
    },
    {
        "name": "畅享套餐",
        "quota_gb": 300,
        "duration_days": 30,
        "price_cents": 3990,
        "description": "高频使用，流量空间更充足。",
    },
]

class XuiManagerApp:
    def __init__(
        self,
        db_path: str | Path,
        static_dir: str | Path | None = None,
        client_factory=XuiClient,
        now: Callable[[], float] | None = None,
    ):
        self.db = Database(db_path)
        self.db.init_schema()
        self.static_dir = Path(static_dir or Path(__file__).resolve().parents[1] / "static")
        self.client_factory = client_factory
        clock = now or time.time
        self.provisioning = ProvisioningService(self.db, client_factory, now=clock)
        self.usage_sync = UsageSyncService(self.db, self.provisioning, client_factory, now=clock)


    def public_store_plans(self) -> list[dict[str, Any]]:
        self.ensure_default_store_plans()
        return self.db.list_plans(enabled_only=True)

    def ensure_default_store_plans(self) -> None:
        existing = self.db.list_plans()
        existing_names = {str(plan.get("name") or "").strip() for plan in existing}
        existing_quotas = {
            round(int(plan.get("quota_bytes") or 0) / 1024 / 1024 / 1024)
            for plan in existing
            if (plan.get("product_type") or "subscription") == "subscription"
        }
        for spec in DEFAULT_STORE_PLANS:
            if spec["name"] in existing_names or spec["quota_gb"] in existing_quotas:
                continue
            self.db.create_plan(
                spec["name"],
                spec["quota_gb"],
                spec["duration_days"],
                [],
                False,
                True,
                spec["price_cents"],
                "subscription",
                "套餐",
                spec["description"],
                "",
            )

    def handle_json(self, method: str, path: str, headers: dict[str, str], body: str) -> Response:
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return self.json_response({"error": "Invalid JSON"}, 400)
        try:
            if method == "GET" and path == "/api/plans":
                return self.json_response({"plans": self.public_store_plans()})
            if method == "GET" and path == "/api/tutorials":
                return self.json_response({"tutorials": self.db.list_tutorials(enabled_only=True)})
            if method == "GET" and path == "/api/nodes/status":
                return self.json_response({"nodes": [public_node_status(node) for node in self.db.list_nodes(enabled_only=True)]})
            if method == "POST" and path == "/api/register":
                user = self.db.register_user(payload["email"], payload["password"])
                session = self.db.create_session(user["id"])
                return self.json_response(
                    {"user": self.user_summary(user, headers)},
                    headers={"Set-Cookie": cookie_header(session)},
                )
            if method == "POST" and path == "/api/login":
                user = self.db.authenticate(payload["email"], payload["password"])
                if not user:
                    return self.json_response({"error": "Invalid email or password"}, 401)
                session = self.db.create_session(user["id"])
                return self.json_response({"user": self.user_summary(user, headers)}, headers={"Set-Cookie": cookie_header(session)})
            if method == "POST" and path == "/api/logout":
                self.db.delete_session(session_token(headers))
                return self.json_response({"logged_out": True}, headers={"Set-Cookie": expired_cookie_header()})
            if method == "GET" and path == "/api/me":
                user = self.user_from_headers(headers)
                return self.json_response({"user": self.user_summary(user, headers) if user else None})
            if method == "POST" and path == "/api/me/password":
                user = self.user_from_headers(headers)
                if not user:
                    return self.json_response({"error": "请先登录"}, 401)
                guard = self.require_mutation_headers(headers)
                if guard:
                    return guard
                updated = self.db.update_password(user["id"], payload["current_password"], payload["new_password"])
                return self.json_response({"user": self.user_summary(updated, headers)})
            if method == "GET" and path == "/api/checkin":
                user = self.user_from_headers(headers)
                if not user or user.get("role") != "user":
                    return self.json_response({"error": "请先登录"}, 401)
                return self.json_response({"checkin": self.db.checkin_status(user["id"])})
            if method == "POST" and path == "/api/checkin":
                user = self.user_from_headers(headers)
                if not user or user.get("role") != "user":
                    return self.json_response({"error": "请先登录"}, 401)
                guard = self.require_mutation_headers(headers)
                if guard:
                    return guard
                result = self.db.perform_checkin(user["id"])
                return self.json_response(
                    {
                        "reward_bytes": result["record"]["reward_bytes"],
                        "checkin": result["checkin"],
                        "user": self.user_summary(result["user"], headers),
                    }
                )
            if method == "GET" and path == "/api/tickets":
                user = self.user_from_headers(headers)
                if not user or user.get("role") != "user":
                    return self.json_response({"error": "请先登录"}, 401)
                return self.json_response({"tickets": self.db.list_user_tickets(user["id"])})
            if method == "POST" and path == "/api/tickets":
                user = self.user_from_headers(headers)
                if not user or user.get("role") != "user":
                    return self.json_response({"error": "请先登录"}, 401)
                guard = self.require_mutation_headers(headers)
                if guard:
                    return guard
                ticket = self.db.create_ticket(user["id"], payload["subject"], payload["message"])
                return self.json_response({"ticket": ticket})
            if method == "POST" and path == "/api/tickets/reply":
                user = self.user_from_headers(headers)
                if not user or user.get("role") != "user":
                    return self.json_response({"error": "请先登录"}, 401)
                guard = self.require_mutation_headers(headers)
                if guard:
                    return guard
                ticket = self.db.reply_user_ticket(int(payload["ticket_id"]), user["id"], payload["message"])
                return self.json_response({"ticket": ticket})
            if method == "GET" and path == "/api/balance/transactions":
                user = self.user_from_headers(headers)
                if not user or user.get("role") != "user":
                    return self.json_response({"error": "请先登录"}, 401)
                return self.json_response({"transactions": self.db.list_balance_transactions(user["id"])})
            if method == "POST" and path == "/api/recharge":
                user = self.user_from_headers(headers)
                if not user:
                    return self.json_response({"error": "请先登录后再兑换礼品卡"}, 401)
                guard = self.require_mutation_headers(headers)
                if guard:
                    return guard
                recharged = self.db.redeem_recharge_card(user["id"], payload["code"])
                return self.json_response({"user": self.user_summary(recharged, headers)})
            if method == "POST" and path in {"/api/purchases", "/api/applications"}:
                user = self.user_from_headers(headers)
                if not user or user.get("role") != "user":
                    return self.json_response({"error": "请先登录后再购买套餐"}, 401)
                guard = self.require_mutation_headers(headers)
                if guard:
                    return guard
                purchased = self.db.purchase_plan(user["id"], int(payload["plan_id"]))
                data: dict[str, Any] = {"user": self.user_summary(purchased, headers)}
                data["provisioning"] = self.provisioning.activate_purchased_plan(purchased["id"])
                data["errors"] = self.provisioning.failure_details_for_user(purchased["id"])
                return self.json_response(data)
            if path.startswith("/api/admin/"):
                user = self.require_admin(headers)
                if isinstance(user, Response):
                    return user
                if method == "POST":
                    guard = self.require_mutation_headers(headers)
                    if guard:
                        return guard
                return self.handle_admin(method, path, headers, payload)
        except KeyError as exc:
            return self.json_response({"error": f"Missing field: {exc.args[0]}"}, 400)
        except ValueError as exc:
            return self.json_response({"error": str(exc)}, 400)
        except PermissionError as exc:
            return self.json_response({"error": str(exc)}, 403)
        return self.json_response({"error": "Not found"}, 404)

    def handle_admin(self, method: str, path: str, headers: dict[str, str], payload: dict[str, Any]) -> Response:
        if method == "GET" and path == "/api/admin/users":
            return self.json_response({"users": [self.admin_user_summary(user, headers) for user in self.db.list_users()]})
        if method == "POST" and path == "/api/admin/users/balance":
            admin = self.user_from_headers(headers)
            adjusted = self.db.adjust_user_balance(
                int(payload["user_id"]),
                yuan_to_cents(payload["amount_yuan"]),
                payload["note"],
                admin["id"],
            )
            return self.json_response({"user": self.admin_user_summary(adjusted, headers)})
        if method == "POST" and path == "/api/admin/users/note":
            noted = self.db.update_user_note(
                int(payload["user_id"]), payload.get("note", ""), bool(payload.get("is_priority", False))
            )
            return self.json_response({"user": self.admin_user_summary(noted, headers)})
        if method == "GET" and path == "/api/admin/recharge-cards":
            return self.json_response({"cards": self.db.list_recharge_cards()})
        if method == "POST" and path == "/api/admin/recharge-cards":
            admin = self.user_from_headers(headers)
            secret = recharge_card_secret()
            cards = self.db.create_recharge_cards(
                yuan_to_cents(payload["amount_yuan"]), int(payload.get("count", 1)), admin["id"], secret
            )
            return self.json_response({"cards": cards})
        if method == "POST" and path == "/api/admin/recharge-cards/reveal":
            return self.json_response(self.db.reveal_recharge_card(int(payload["id"]), recharge_card_secret()))
        if method == "POST" and path == "/api/admin/users/approve":
            existing = self.db.get_user(int(payload["user_id"]))
            if not existing:
                raise ValueError("user not found")
            if existing["status"] == "active" and not bool(payload.get("renew", False)):
                user = existing
            else:
                user = self.db.approve_user(int(payload["user_id"]))
                if bool(payload.get("renew", False)) and bool(payload.get("reset_usage", False)):
                    self.db.reset_managed_usage(user["id"])
            provisioning = self.provisioning.provision_user(user["id"])
            return self.json_response(
                {
                    "user": self.admin_user_summary(user, headers),
                    "provisioning": provisioning,
                    "errors": self.provisioning.failure_details_for_user(user["id"]),
                }
            )
        if method == "POST" and path == "/api/admin/users/provision/retry":
            user_id = int(payload["user_id"])
            return self.json_response(
                {
                    "provisioning": self.provisioning.retry_user(user_id),
                    "errors": self.provisioning.failure_details_for_user(user_id),
                }
            )
        if method == "POST" and path == "/api/admin/users/reconcile":
            user_id = int(payload["user_id"])
            return self.json_response(
                {
                    "reconcile": self.provisioning.reconcile_user(user_id, bool(payload.get("apply", False))),
                    "errors": self.provisioning.failure_details_for_user(user_id),
                }
            )
        if method == "POST" and path == "/api/admin/users/status":
            user_id = int(payload["user_id"])
            status = payload["status"]
            user = self.db.update_user_status(user_id, status)
            data: dict[str, Any] = {"user": self.user_summary(user, headers)}
            if status in {"active", "disabled"}:
                data["provisioning"] = self.provisioning.set_user_enabled(user_id, status == "active")
                data["errors"] = self.provisioning.failure_details_for_user(user_id)
            return self.json_response(data)
        if method == "POST" and path == "/api/admin/users/delete":
            result = self.provisioning.delete_user(int(payload["user_id"]))
            if result["deleted"]:
                return self.json_response(result)
            first = result["errors"][0]
            result["error"] = (
                f"用户与节点清理失败：{first['panel_name']} inbound {first['inbound_id']}：{first['error']}"
            )
            return self.json_response(result, 502)
        if method == "GET" and path == "/api/admin/plans":
            return self.json_response({"plans": self.db.list_plans()})
        if method == "POST" and path == "/api/admin/plans":
            args = (
                payload["name"],
                float(payload["quota_gb"]),
                int(payload["duration_days"]),
                tags_from_payload(payload.get("allowed_tags")),
                False,
                bool(payload.get("enabled", True)),
                yuan_to_cents(payload.get("price_yuan", 0)),
                payload.get("product_type", "subscription"),
                payload.get("category", "套餐"),
                payload.get("description", ""),
                payload.get("purchase_notice", ""),
            )
            if payload.get("id"):
                return self.json_response({"plan": self.db.update_plan(int(payload["id"]), *args)})
            return self.json_response({"id": self.db.create_plan(*args)})
        if method == "POST" and path == "/api/admin/plans/delete":
            self.db.delete_plan(int(payload["id"]))
            return self.json_response({"deleted": True})
        if method == "GET" and path == "/api/admin/tutorials":
            return self.json_response({"tutorials": self.db.list_tutorials()})
        if method == "POST" and path == "/api/admin/tutorials":
            tutorial = self.db.save_tutorial(
                payload["title"],
                payload.get("platform", "通用"),
                payload["content"],
                payload.get("image_url", ""),
                bool(payload.get("enabled", True)),
                int(payload.get("sort_order", 0)),
                int(payload["id"]) if payload.get("id") else None,
            )
            return self.json_response({"tutorial": tutorial})
        if method == "POST" and path == "/api/admin/tutorials/delete":
            self.db.delete_tutorial(int(payload["id"]))
            return self.json_response({"deleted": True})
        if method == "GET" and path == "/api/admin/panels":
            return self.json_response({"panels": [public_panel(panel) for panel in self.db.list_panels()]})
        if method == "POST" and path == "/api/admin/panels":
            password = payload.get("password", "")
            if payload.get("id") and not password:
                existing = next((panel for panel in self.db.list_panels() if panel["id"] == int(payload["id"])), None)
                if not existing:
                    raise ValueError("panel not found")
                password = existing["password"]
            args = (
                payload["name"],
                payload["base_url"],
                payload.get("username", ""),
                password,
                payload.get("subscription_url", ""),
                bool(payload.get("verify_tls", True)),
                bool(payload.get("enabled", True)),
            )
            if payload.get("id"):
                return self.json_response({"panel": public_panel(self.db.update_panel(int(payload["id"]), *args))})
            return self.json_response({"id": self.db.create_panel(*args)})
        if method == "POST" and path == "/api/admin/panels/inbounds":
            panel = next((item for item in self.db.list_panels() if item["id"] == int(payload["panel_id"])), None)
            if not panel:
                raise ValueError("panel not found")
            client = self.client_factory(panel["base_url"], panel.get("username", ""), panel.get("password", ""), bool(panel.get("verify_tls", True)))
            try:
                client.login()
                inbounds = [public_inbound(item) for item in client.list_inbounds()]
            except Exception as exc:  # noqa: BLE001
                return self.json_response({"error": f"X-UI inbounds fetch failed: {panel_error(exc, panel)}"}, 502)
            return self.json_response({"inbounds": inbounds})
        if method == "POST" and path == "/api/admin/panels/test":
            panel = next((item for item in self.db.list_panels() if item["id"] == int(payload["panel_id"])), None)
            if not panel:
                raise ValueError("panel not found")
            client = self.client_factory(panel["base_url"], panel.get("username", ""), panel.get("password", ""), bool(panel.get("verify_tls", True)))
            try:
                client.login()
                inbound_count = len(client.list_inbounds())
            except Exception as exc:  # noqa: BLE001
                return self.json_response({"ok": False, "error": f"X-UI panel test failed: {panel_error(exc, panel)}"}, 502)
            return self.json_response({"ok": True, "inbound_count": inbound_count})
        if method == "POST" and path == "/api/admin/panels/delete":
            self.db.delete_panel(int(payload["id"]))
            return self.json_response({"deleted": True})
        if method == "GET" and path == "/api/admin/nodes":
            return self.json_response({"nodes": self.db.list_nodes()})
        if method == "POST" and path == "/api/admin/nodes":
            panel_id = int(payload["panel_id"]) if payload.get("panel_id") else None
            args = (
                payload["name"],
                payload["source_url"],
                float(payload.get("rate", 1)),
                tags_from_payload(payload.get("tags")),
                bool(payload.get("enabled", True)),
                panel_id,
                int(payload.get("inbound_id") or 0),
                payload.get("mode", "static"),
            )
            if payload.get("id"):
                return self.json_response({"node": self.db.update_node(int(payload["id"]), *args)})
            return self.json_response({"id": self.db.create_node(*args)})
        if method == "POST" and path == "/api/admin/nodes/delete":
            self.db.delete_node(int(payload["id"]))
            return self.json_response({"deleted": True})
        if method == "POST" and path == "/api/admin/nodes/status":
            node = self.db.update_node_status(int(payload["id"]), payload.get("status", "unknown"), payload.get("latency_ms"))
            return self.json_response({"node": node})
        if method == "POST" and path == "/api/admin/usage":
            upload = bytes_from_gb(float(payload.get("upload_gb") or 0))
            download = bytes_from_gb(float(payload.get("download_gb") or 0))
            self.db.record_usage(int(payload["user_id"]), int(payload["node_id"]), upload, download)
            user = self.db.get_user(int(payload["user_id"]))
            return self.json_response({"user": self.user_summary(user, headers)})
        if method == "POST" and path == "/api/admin/sync-usage":
            return self.json_response(self.usage_sync.sync_all())
        if method == "GET" and path == "/api/admin/settings":
            return self.json_response({"settings": self.admin_settings()})
        if method == "POST" and path == "/api/admin/settings":
            for key, value in payload.items():
                if key == "subscription_title":
                    value = str(value or "").strip()
                self.db.set_setting(str(key), value)
            return self.json_response({"settings": self.admin_settings()})
        if method == "GET" and path == "/api/admin/checkin/settings":
            return self.json_response({"settings": self.db.checkin_settings()})
        if method == "POST" and path == "/api/admin/checkin/settings":
            return self.json_response({"settings": self.db.save_checkin_settings(payload)})
        if method == "GET" and path == "/api/admin/tickets":
            return self.json_response({"tickets": self.db.list_admin_tickets()})
        if method == "POST" and path == "/api/admin/tickets/reply":
            admin = self.user_from_headers(headers)
            ticket = self.db.reply_ticket(
                int(payload["ticket_id"]),
                admin["id"] if admin else None,
                payload["message"],
                payload.get("status", "open"),
            )
            return self.json_response({"ticket": ticket})
        return self.json_response({"error": "Not found"}, 404)

    def admin_settings(self) -> dict[str, Any]:
        return {
            "sync_interval_seconds": self.db.get_setting("sync_interval_seconds", "300"),
            "subscription_title": self.db.get_setting("subscription_title", ""),
        }

    def subscription(self, token: str, format_name: str = "clash") -> Response:
        builders = {
            "clash": build_clash_subscription,
            "base64": build_base64_subscription,
            "singbox": build_singbox_subscription,
        }
        builder = builders.get(format_name)
        if not builder:
            return Response(404, "not found\n", {"Content-Type": "text/plain; charset=utf-8"})
        return builder(self.db, token)

    def static_response(self, path: str) -> Response:
        if path in {"", "/"}:
            target = self.static_dir / "index.html"
        else:
            target = (self.static_dir / path.lstrip("/")).resolve()
            if not str(target).startswith(str(self.static_dir.resolve())):
                return Response(403, "forbidden\n", {"Content-Type": "text/plain; charset=utf-8"})
        if not target.exists() or not target.is_file():
            return Response(404, "not found\n", {"Content-Type": "text/plain; charset=utf-8"})
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return Response(200, target.read_text(encoding="utf-8"), {"Content-Type": content_type + "; charset=utf-8"})

    def user_from_headers(self, headers: dict[str, str]) -> dict[str, Any] | None:
        return self.db.get_session_user(session_token(headers))

    def require_admin(self, headers: dict[str, str]) -> dict[str, Any] | Response:
        user = self.user_from_headers(headers)
        if not user or user.get("role") != "admin":
            return self.json_response({"error": "Admin required"}, 403)
        return user

    def require_mutation_headers(self, headers: dict[str, str]) -> Response | None:
        content_type = headers.get("Content-Type", "")
        if content_type and "application/json" not in content_type.lower():
            return self.json_response({"error": "JSON required"}, 415)
        origin = headers.get("Origin") or ""
        referer = headers.get("Referer") or ""
        source = origin or referer
        if not source:
            return None
        source_host = urllib.parse.urlparse(source).netloc
        target_host = headers.get("X-Forwarded-Host") or headers.get("Host") or ""
        if source_host and target_host and source_host.lower() != target_host.lower():
            if self.is_allowed_cors_origin(origin or source):
                return None
            return self.json_response({"error": "Cross-origin request rejected"}, 403)
        return None

    def allowed_cors_origins(self) -> set[str]:
        raw = os.environ.get("CORS_ALLOWED_ORIGINS") or os.environ.get("FRONTEND_ORIGIN") or ""
        return {origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()}

    def is_allowed_cors_origin(self, origin: str) -> bool:
        normalized = origin.strip().rstrip("/")
        allowed = self.allowed_cors_origins()
        return bool(normalized and ("*" in allowed or normalized in allowed))

    def cors_headers(self, request_headers: dict[str, str]) -> dict[str, str]:
        origin = (request_headers.get("Origin") or "").strip().rstrip("/")
        if not origin or not self.is_allowed_cors_origin(origin):
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }

    def user_summary(self, user: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        data = public_user(user)
        totals = usage_totals(self.db, user["id"])
        used = totals["upload"] + totals["download"]
        quota = int(user.get("quota_bytes") or 0)
        data["used_bytes"] = used
        data["upload_bytes"] = totals["upload"]
        data["download_bytes"] = totals["download"]
        data["remaining_bytes"] = max(quota - used, 0) if quota else 0
        data["subscription_url"] = subscription_url(user["token"], headers or {}) if user.get("token") else ""
        data["subscription_urls"] = subscription_urls(user["token"], headers or {}) if user.get("token") else {}
        return data

    def admin_user_summary(self, user: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        data = self.user_summary(user, headers)
        data["admin_note"] = user.get("admin_note", "")
        data["is_priority"] = bool(user.get("is_priority", False))
        data["provisioning"] = self.provisioning.status_for_user(user["id"])
        data["provisioning_errors"] = self.provisioning.failure_details_for_user(user["id"])
        return data

    def json_response(self, payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> Response:
        final_headers = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            final_headers.update(headers)
        return Response(status, json.dumps(payload, ensure_ascii=False) + "\n", final_headers)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in user.items()
        if key not in {"password_hash", "admin_note", "is_priority"}
    }


def public_panel(panel: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in panel.items()
        if key != "password"
    } | {"has_password": bool(panel.get("password"))}


def public_node_status(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node["id"],
        "name": node["name"],
        "rate": node.get("rate", 1),
        "tags": node.get("tags", []),
        "enabled": bool(node.get("enabled", False)),
        "status": node.get("status") or "unknown",
        "latency_ms": node.get("latency_ms"),
        "last_checked_at": int(node.get("last_checked_at") or 0),
    }


def public_inbound(inbound: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(inbound.get("id") or 0),
        "remark": str(inbound.get("remark") or ""),
        "port": int(inbound.get("port") or 0),
        "protocol": str(inbound.get("protocol") or ""),
        "enabled": bool(inbound.get("enable", inbound.get("enabled", True))),
    }


def panel_error(exc: Exception, panel: dict[str, Any]) -> str:
    message = str(exc or "request failed") or exc.__class__.__name__
    for secret in (panel.get("password"), panel.get("username")):
        if secret:
            message = message.replace(str(secret), "[redacted]")
    return message[:200]


def cookie_header(session: str) -> str:
    return f"session={session}; {cookie_attributes()}"


def expired_cookie_header() -> str:
    return f"session=; Max-Age=0; {cookie_attributes()}"


def cookie_attributes() -> str:
    same_site = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax").strip() or "Lax"
    secure = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on"}
    attrs = f"Path=/; HttpOnly; SameSite={same_site}"
    if secure:
        attrs += "; Secure"
    return attrs


def session_token(headers: dict[str, str]) -> str:
    jar = cookies.SimpleCookie(headers.get("Cookie", ""))
    morsel = jar.get("session")
    return morsel.value if morsel else ""


def tags_from_payload(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def yuan_to_cents(value: Any) -> int:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("金额格式不正确") from exc
    return int(amount * 100)


def subscription_url(token: str, headers: dict[str, str]) -> str:
    host = headers.get("X-Forwarded-Host") or headers.get("Host")
    if not host:
        return f"/sub/clash/{token}"
    proto = headers.get("X-Forwarded-Proto") or "http"
    return f"{proto}://{host}/sub/clash/{token}"


def subscription_urls(token: str, headers: dict[str, str]) -> dict[str, str]:
    clash = subscription_url(token, headers)
    base = clash.rsplit("/sub/clash/", 1)[0]
    return {
        "clash": clash,
        "base64": f"{base}/sub/base64/{token}",
        "singbox": f"{base}/sub/singbox/{token}",
    }


def recharge_card_secret() -> str:
    return os.environ.get("RECHARGE_CARD_SECRET", "").strip()


def create_app(db_path: str | Path) -> XuiManagerApp:
    return XuiManagerApp(db_path)


def make_handler(app: XuiManagerApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.write_response(Response(204, "", {}), include_body=False)

        def do_HEAD(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/api/"):
                self.write_response(app.handle_json("GET", path, self.header_map(), ""), include_body=False)
                return
            self.write_response(app.static_response(path), include_body=False)

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/sub/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    self.write_response(Response(404, "not found\n", {"Content-Type": "text/plain; charset=utf-8"}))
                    return
                format_name = parts[1]
                token = path.rsplit("/", 1)[-1]
                self.write_response(app.subscription(token, format_name))
                return
            if path.startswith("/api/"):
                self.write_response(app.handle_json("GET", path, self.header_map(), ""))
                return
            self.write_response(app.static_response(path))

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            self.write_response(app.handle_json("POST", path, self.header_map(), body))

        def write_response(self, response: Response, include_body: bool = True) -> None:
            data = response.body.encode("utf-8")
            self.send_response(response.status)
            response_headers = {**app.cors_headers(self.header_map()), **response.headers}
            for key, value in response_headers.items():
                self.send_header(key, str(value))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if include_body:
                self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            try:
                sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))
            except Exception:
                return

        def header_map(self) -> dict[str, str]:
            return {key: value for key, value in self.headers.items()}

    return Handler


def run() -> None:
    data_dir = Path(os.environ.get("XUI_MANAGER_DATA", "/opt/xui-manager-panel/data"))
    app = XuiManagerApp(data_dir / "app.db")
    admin_email = os.environ.get("ADMIN_EMAIL")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if admin_email and admin_password:
        app.db.seed_admin(admin_email, admin_password)
    host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("LISTEN_PORT", "25888"))
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"xui-manager-panel listening on {host}:{port}")
    worker = PeriodicSyncWorker(app.usage_sync, lambda: app.db.get_setting("sync_interval_seconds", "300"))
    worker.start()
    try:
        server.serve_forever()
    finally:
        worker.stop()


if __name__ == "__main__":
    run()
