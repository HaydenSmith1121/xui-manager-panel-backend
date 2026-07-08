import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xui_manager.app import XuiManagerApp


class FakePanelClient:
    def __init__(self, base_url, username, password, verify_tls=True):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.verify_tls = verify_tls

    def login(self):
        if self.password == "bad":
            raise RuntimeError("login failed")

    def list_inbounds(self):
        return [
            {"id": 1, "remark": "primary", "port": 443, "protocol": "vless", "enable": True},
            {"id": 2, "remark": "trojan", "port": 8443, "protocol": "trojan", "enable": False},
        ]

    def get_inbound(self, inbound_id):
        return {
            "id": int(inbound_id),
            "protocol": "vless",
            "settings": json.dumps({"clients": []}),
            "clientStats": [],
        }

    def find_client(self, inbound, email):
        return None

    def add_vless_client(self, *, inbound_id, client_uuid, email, flow, expire_at):
        return {"id": client_uuid, "email": email, "flow": flow, "enable": True}

    def update_vless_client(self, *, inbound_id, client_uuid, email, flow, expire_at, enabled):
        return {"id": client_uuid, "email": email, "flow": flow, "enable": bool(enabled)}

    def delete_vless_client(self, *, inbound_id, client_uuid, email):
        return True


class ExplodingPanelClient(FakePanelClient):
    def login(self):
        raise RuntimeError(f"login failed for {self.username}:{self.password}")


class ManagedAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.secret_patch = patch.dict(os.environ, {"RECHARGE_CARD_SECRET": "managed-test-secret"})
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)
        self.app = XuiManagerApp(Path(self.tmp.name) / "app.db", client_factory=FakePanelClient)
        self.admin = self.app.db.seed_admin("admin@example.com", "password123")
        login = self.app.handle_json(
            "POST",
            "/api/login",
            {},
            json.dumps({"email": "admin@example.com", "password": "password123"}),
        )
        self.headers = {
            "Cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }

    def post_admin(self, path, payload, headers=None):
        merged = dict(self.headers)
        if headers:
            merged.update(headers)
        return self.app.handle_json("POST", path, merged, json.dumps(payload))

    def login_user_headers(self, email, password="secret123"):
        login = self.app.handle_json(
            "POST", "/api/login", {}, json.dumps({"email": email, "password": password})
        )
        return {
            "Cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }

    def test_user_purchases_plan_with_balance_and_receives_provisioning_result(self):
        plan_id = self.app.db.create_plan("Balance Pro", 100, 30, [], False, price_cents=1299)
        user = self.app.db.register_user("buyer@example.com", "secret123")
        self.app.db.adjust_user_balance(user["id"], 2000, "opening credit", self.admin["id"])
        headers = self.login_user_headers(user["email"])

        response = self.app.handle_json(
            "POST", "/api/purchases", headers, json.dumps({"plan_id": plan_id})
        )
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["user"]["balance_cents"], 701)
        self.assertEqual(payload["user"]["status"], "active")
        self.assertIn("provisioning", payload)

    def test_recharge_card_admin_generation_user_redemption_and_history(self):
        user = self.app.db.register_user("recharge@example.com", "secret123")
        generated = self.post_admin("/api/admin/recharge-cards", {"amount_yuan": 25, "count": 2})
        cards = json.loads(generated.body)["cards"]
        headers = self.login_user_headers(user["email"])

        redeemed = self.app.handle_json(
            "POST", "/api/recharge", headers, json.dumps({"code": cards[0]["code"]})
        )
        history = self.app.handle_json("GET", "/api/balance/transactions", headers, "")
        admin_list = self.app.handle_json("GET", "/api/admin/recharge-cards", self.headers, "")

        self.assertEqual(generated.status, 200)
        self.assertEqual(redeemed.status, 200)
        self.assertEqual(json.loads(redeemed.body)["user"]["balance_cents"], 2500)
        self.assertEqual(json.loads(history.body)["transactions"][0]["kind"], "recharge_card")
        self.assertNotIn("code_hash", admin_list.body)
        self.assertNotIn(cards[0]["code"], admin_list.body)

    def test_admin_profile_can_redeem_gift_card(self):
        generated = self.post_admin("/api/admin/recharge-cards", {"amount_yuan": 15, "count": 1})
        card = json.loads(generated.body)["cards"][0]

        response = self.app.handle_json(
            "POST", "/api/recharge", self.headers, json.dumps({"code": card["code"]})
        )
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["user"]["email"], "admin@example.com")
        self.assertEqual(payload["user"]["balance_cents"], 1500)

    def test_admin_can_adjust_balance_and_mark_priority_note(self):
        user = self.app.db.register_user("priority@example.com", "secret123")

        balance = self.post_admin(
            "/api/admin/users/balance",
            {"user_id": user["id"], "amount_yuan": 18.5, "note": "售后补偿"},
        )
        note = self.post_admin(
            "/api/admin/users/note",
            {"user_id": user["id"], "note": "重点续费用户", "is_priority": True},
        )

        self.assertEqual(json.loads(balance.body)["user"]["balance_cents"], 1850)
        noted_user = json.loads(note.body)["user"]
        self.assertEqual(noted_user["admin_note"], "重点续费用户")
        self.assertTrue(noted_user["is_priority"])

    def test_user_summary_exposes_clash_mobile_and_singbox_subscription_urls(self):
        user = self.app.db.register_user("formats@example.com", "secret123")
        headers = self.login_user_headers(user["email"])

        response = self.app.handle_json("GET", "/api/me", headers, "")
        urls = json.loads(response.body)["user"]["subscription_urls"]

        self.assertEqual(urls["clash"], f"http://manager.example.com/sub/clash/{user['token']}")
        self.assertEqual(urls["base64"], f"http://manager.example.com/sub/base64/{user['token']}")
        self.assertEqual(urls["singbox"], f"http://manager.example.com/sub/singbox/{user['token']}")

    def test_approve_returns_provisioning_summary(self):
        plan_id = self.app.db.create_plan("Premium", 100, 30, ["premium"], True)
        user = self.app.db.register_user("user@example.com", "secret123", plan_id)
        panel_id = self.app.db.create_panel("Panel", "https://panel.example.com", "admin", "secret")
        self.app.db.create_node(
            "Managed",
            "vless://template@example.com:443?security=tls#US",
            1,
            ["premium"],
            True,
            panel_id,
            1,
            "managed",
        )

        response = self.post_admin("/api/admin/users/approve", {"user_id": user["id"]})
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["provisioning"], {"provisioned": 1, "failed": 0, "pending": 0})
        self.assertEqual(payload["user"]["status"], "active")

    def test_registration_without_plan_creates_signed_in_unsubscribed_user(self):
        response = self.app.handle_json(
            "POST",
            "/api/register",
            {"Host": "manager.example.com"},
            json.dumps({"email": "new@example.com", "password": "secret123"}),
        )
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["user"]["status"], "unsubscribed")
        self.assertIsNone(payload["user"]["plan_id"])
        self.assertIn("Set-Cookie", response.headers)

    def test_authenticated_user_can_purchase_plan_without_approval(self):
        plan_id = self.app.db.create_plan("Premium", 100, 30, ["premium"], True)
        registration = self.app.handle_json(
            "POST",
            "/api/register",
            {"Host": "manager.example.com"},
            json.dumps({"email": "apply@example.com", "password": "secret123"}),
        )
        headers = {
            "Cookie": registration.headers["Set-Cookie"].split(";", 1)[0],
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }

        response = self.app.handle_json("POST", "/api/purchases", headers, json.dumps({"plan_id": plan_id}))
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["user"]["status"], "active")
        self.assertEqual(payload["user"]["plan_id"], plan_id)
        self.assertIn("provisioning", payload)

    def test_purchase_requires_login_and_allows_existing_user_to_renew(self):
        plan_id = self.app.db.create_plan("Premium", 100, 30, [], True)
        unauthenticated = self.app.handle_json(
            "POST",
            "/api/purchases",
            {"Content-Type": "application/json"},
            json.dumps({"plan_id": plan_id}),
        )
        user = self.app.db.register_user("pending@example.com", "secret123", plan_id)
        session = self.app.db.create_session(user["id"])
        headers = {
            "Cookie": f"session={session}",
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }
        renewed = self.app.handle_json("POST", "/api/purchases", headers, json.dumps({"plan_id": plan_id}))

        self.assertEqual(unauthenticated.status, 401)
        self.assertEqual(renewed.status, 200)
        self.assertEqual(json.loads(renewed.body)["user"]["status"], "active")
        self.assertEqual(self.app.db.get_user(user["id"])["plan_id"], plan_id)

    def test_auto_active_application_runs_provisioning(self):
        plan_id = self.app.db.create_plan("Instant", 100, 30, ["premium"], False)
        panel_id = self.app.db.create_panel("Panel", "https://panel.example.com", "admin", "secret")
        self.app.db.create_node(
            "Managed",
            "vless://template@example.com:443?security=tls#US",
            1,
            ["premium"],
            True,
            panel_id,
            1,
            "managed",
        )

        registration = self.app.handle_json(
            "POST",
            "/api/register",
            {"Host": "manager.example.com"},
            json.dumps({"email": "instant@example.com", "password": "secret123"}),
        )
        headers = {
            "Cookie": registration.headers["Set-Cookie"].split(";", 1)[0],
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }
        response = self.app.handle_json("POST", "/api/applications", headers, json.dumps({"plan_id": plan_id}))
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["user"]["status"], "active")
        self.assertEqual(payload["provisioning"], {"provisioned": 1, "failed": 0, "pending": 0})

    def test_repeated_approve_does_not_renew_without_flag_and_reset_requires_renewal(self):
        plan_id = self.app.db.create_plan("Premium", 100, 30, ["premium"], True)
        user = self.app.db.register_user("user@example.com", "secret123", plan_id)
        first = json.loads(self.post_admin("/api/admin/users/approve", {"user_id": user["id"]}).body)["user"]
        self.app.db.reset_managed_usage(user["id"])

        second = json.loads(
            self.post_admin(
                "/api/admin/users/approve",
                {"user_id": user["id"], "reset_usage": True},
            ).body
        )["user"]

        self.assertEqual(second["expire_at"], first["expire_at"])

        renewed = json.loads(
            self.post_admin(
                "/api/admin/users/approve",
                {"user_id": user["id"], "renew": True, "reset_usage": True},
            ).body
        )["user"]
        self.assertGreaterEqual(renewed["expire_at"], first["expire_at"])

    def test_retry_reconcile_panel_test_and_settings_routes(self):
        panel_id = self.app.db.create_panel("Panel", "https://panel.example.com", "admin", "secret")

        retry = self.post_admin("/api/admin/users/provision/retry", {"user_id": self.admin["id"]})
        preview = self.post_admin("/api/admin/users/reconcile", {"user_id": self.admin["id"], "apply": False})
        panel_test = self.post_admin("/api/admin/panels/test", {"panel_id": panel_id})
        settings_post = self.post_admin("/api/admin/settings", {"sync_interval_seconds": 120, "subscription_title": "良心云"})
        settings_get = self.app.handle_json("GET", "/api/admin/settings", self.headers, "")

        self.assertEqual(retry.status, 400)  # admin has no plan, but route exists and validates cleanly
        self.assertEqual(preview.status, 400)
        self.assertEqual(json.loads(panel_test.body)["ok"], True)
        self.assertEqual(settings_post.status, 200)
        self.assertEqual(json.loads(settings_get.body)["settings"]["sync_interval_seconds"], "120")
        self.assertEqual(json.loads(settings_get.body)["settings"]["subscription_title"], "良心云")

    def test_retry_returns_failed_target_details_without_panel_secrets(self):
        app = XuiManagerApp(Path(self.tmp.name) / "retry-errors.db", client_factory=ExplodingPanelClient)
        admin = app.db.seed_admin("admin@example.com", "password123")
        login = app.handle_json(
            "POST",
            "/api/login",
            {},
            json.dumps({"email": admin["email"], "password": "password123"}),
        )
        headers = {
            "Cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }
        plan_id = app.db.create_plan("Premium", 100, 30, [], True)
        user = app.db.approve_user(app.db.register_user("user@example.com", "secret123", plan_id)["id"])
        panel_id = app.db.create_panel("Korea", "https://panel.example.com", "admin", "stored-secret")
        app.db.create_node(
            "Korea Managed",
            "vless://template@example.com:443?security=tls#KR",
            1,
            [],
            True,
            panel_id,
            1,
            "managed",
        )

        response = app.handle_json(
            "POST",
            "/api/admin/users/provision/retry",
            headers,
            json.dumps({"user_id": user["id"]}),
        )
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["provisioning"], {"provisioned": 0, "failed": 1, "pending": 0})
        self.assertEqual(payload["errors"][0]["panel_name"], "Korea")
        self.assertEqual(payload["errors"][0]["inbound_id"], 1)
        self.assertIn("login failed", payload["errors"][0]["error"])
        self.assertNotIn("stored-secret", response.body)

    def test_panel_list_never_returns_password_and_blank_update_preserves_password(self):
        panel_id = self.app.db.create_panel("Panel", "https://panel.example.com", "admin", "stored-secret")

        listing = self.app.handle_json("GET", "/api/admin/panels", self.headers, "")
        panel = json.loads(listing.body)["panels"][0]
        self.assertNotIn("password", panel)
        self.assertTrue(panel["has_password"])

        update = self.post_admin(
            "/api/admin/panels",
            {
                "id": panel_id,
                "name": "Panel 2",
                "base_url": "https://panel.example.com",
                "username": "admin2",
                "password": "",
                "subscription_url": "",
                "verify_tls": True,
                "enabled": True,
            },
        )

        self.assertEqual(update.status, 200)
        self.assertEqual(self.app.db.list_panels()[0]["password"], "stored-secret")
        self.assertNotIn("password", json.loads(update.body)["panel"])

    def test_panel_inbounds_are_public_and_redacted(self):
        panel_id = self.app.db.create_panel("Panel", "https://panel.example.com", "admin", "secret")

        response = self.post_admin("/api/admin/panels/inbounds", {"panel_id": panel_id})
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["inbounds"][0], {"id": 1, "remark": "primary", "port": 443, "protocol": "vless", "enabled": True})
        self.assertNotIn("secret", response.body)

    def test_panel_test_failure_returns_json_without_secrets(self):
        app = XuiManagerApp(Path(self.tmp.name) / "bad-panel.db", client_factory=ExplodingPanelClient)
        app.db.seed_admin("admin@example.com", "password123")
        login = app.handle_json(
            "POST",
            "/api/login",
            {},
            json.dumps({"email": "admin@example.com", "password": "password123"}),
        )
        headers = {
            "Cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }
        panel_id = app.db.create_panel("Panel", "https://panel.example.com", "admin", "stored-secret")

        response = app.handle_json("POST", "/api/admin/panels/test", headers, json.dumps({"panel_id": panel_id}))
        payload = json.loads(response.body)

        self.assertEqual(response.status, 502)
        self.assertFalse(payload["ok"])
        self.assertIn("X-UI panel test failed", payload["error"])
        self.assertNotIn("stored-secret", response.body)
        self.assertNotIn("admin:stored-secret", response.body)

    def test_panel_inbound_failure_returns_json_without_secrets(self):
        app = XuiManagerApp(Path(self.tmp.name) / "bad-inbounds.db", client_factory=ExplodingPanelClient)
        app.db.seed_admin("admin@example.com", "password123")
        login = app.handle_json(
            "POST",
            "/api/login",
            {},
            json.dumps({"email": "admin@example.com", "password": "password123"}),
        )
        headers = {
            "Cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }
        panel_id = app.db.create_panel("Panel", "https://panel.example.com", "admin", "stored-secret")

        response = app.handle_json("POST", "/api/admin/panels/inbounds", headers, json.dumps({"panel_id": panel_id}))
        payload = json.loads(response.body)

        self.assertEqual(response.status, 502)
        self.assertIn("X-UI inbounds fetch failed", payload["error"])
        self.assertNotIn("stored-secret", response.body)

    def test_sync_usage_route_uses_managed_sync_service(self):
        response = self.post_admin("/api/admin/sync-usage", {})
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertIn("synced", payload)
        self.assertIn("disabled", payload)

    def test_delete_user_route_requires_disabled_non_admin_and_deletes_target(self):
        plan_id = self.app.db.create_plan("Premium", 100, 30, [], True)
        active = self.app.db.approve_user(self.app.db.register_user("active@example.com", "secret123", plan_id)["id"])
        disabled = self.app.db.register_user("disabled@example.com", "secret123")
        disabled = self.app.db.update_user_status(disabled["id"], "disabled")

        active_response = self.post_admin("/api/admin/users/delete", {"user_id": active["id"]})
        admin_response = self.post_admin("/api/admin/users/delete", {"user_id": self.admin["id"]})
        deleted_response = self.post_admin("/api/admin/users/delete", {"user_id": disabled["id"]})

        self.assertEqual(active_response.status, 400)
        self.assertEqual(admin_response.status, 400)
        self.assertEqual(deleted_response.status, 200)
        self.assertEqual(json.loads(deleted_response.body), {"deleted": True, "errors": []})
        self.assertIsNone(self.app.db.get_user(disabled["id"]))

    def test_disabling_user_reconciles_managed_clients_before_deletion(self):
        plan_id = self.app.db.create_plan("Premium", 100, 30, [], False)
        user = self.app.db.register_user("managed@example.com", "secret123", plan_id)
        panel_id = self.app.db.create_panel("Panel", "https://panel.example.com", "admin", "secret")
        self.app.db.create_node(
            "Managed", "vless://template@example.com:443?security=tls#KR", 1, [], True, panel_id, 1, "managed"
        )
        self.app.provisioning.provision_user(user["id"])

        response = self.post_admin("/api/admin/users/status", {"user_id": user["id"], "status": "disabled"})

        managed = self.app.db.list_managed_clients(user_id=user["id"])[0]
        self.assertEqual(response.status, 200)
        self.assertFalse(managed["desired_enabled"])
        self.assertFalse(managed["remote_enabled"])

    def test_delete_user_route_preserves_local_user_and_redacts_panel_failure(self):
        app = XuiManagerApp(Path(self.tmp.name) / "delete-errors.db", client_factory=ExplodingPanelClient)
        admin = app.db.seed_admin("admin@example.com", "password123")
        login = app.handle_json("POST", "/api/login", {}, json.dumps({"email": admin["email"], "password": "password123"}))
        headers = {
            "Cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }
        plan_id = app.db.create_plan("Premium", 100, 30, [], False)
        user = app.db.register_user("user@example.com", "secret123", plan_id)
        panel_id = app.db.create_panel("Korea", "https://panel.example.com", "panel-admin", "stored-secret")
        app.db.create_node(
            "Korea Managed", "vless://template@example.com:443?security=tls#KR", 1, [], True, panel_id, 1, "managed"
        )
        app.provisioning.provision_user(user["id"])
        managed = app.db.list_managed_clients(user_id=user["id"])[0]
        app.db.update_user_status(user["id"], "disabled")

        response = app.handle_json("POST", "/api/admin/users/delete", headers, json.dumps({"user_id": user["id"]}))

        self.assertEqual(response.status, 502)
        payload = json.loads(response.body)
        self.assertIn("Korea", payload["error"])
        self.assertIn("inbound 1", payload["error"])
        self.assertIsNotNone(app.db.get_user(user["id"]))
        self.assertNotIn("stored-secret", response.body)
        self.assertNotIn("panel-admin", response.body)
        self.assertNotIn(managed["client_uuid"], response.body)

    def test_node_delete_route_removes_node(self):
        node_id = self.app.db.create_node(
            "Temporary",
            "vless://template@example.com:443?security=tls#US",
            1,
            [],
            True,
        )

        response = self.post_admin("/api/admin/nodes/delete", {"id": node_id})

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body), {"deleted": True})
        self.assertEqual(self.app.db.list_nodes(), [])

    def test_mutating_admin_route_rejects_cross_origin_request(self):
        response = self.post_admin(
            "/api/admin/plans",
            {"name": "Bad", "quota_gb": 1, "duration_days": 1},
            headers={"Origin": "https://evil.example.com"},
        )

        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
