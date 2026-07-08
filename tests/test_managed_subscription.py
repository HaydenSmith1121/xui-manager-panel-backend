import base64
import json
import tempfile
import unittest
from pathlib import Path

from xui_manager.db import Database
from xui_manager.subscription import build_base64_subscription, build_clash_subscription, build_singbox_subscription


VLESS = "vless://template@example.com:443?security=tls&sni=edge.example&type=ws&path=%2Fedge#Managed"
TROJAN = "trojan://pass@static.example.com:443?sni=static.example.com#Static"


class ManagedSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "app.db")
        self.db.init_schema()
        self.plan_id = self.db.create_plan("Premium", 100, 30, ["premium"], True)
        self.panel_id = self.db.create_panel("Panel", "https://panel.example.com", "admin", "secret")
        self.db.create_node("Managed", VLESS, 1, ["premium"], True, self.panel_id, 7, "managed")

    def active_user(self, email):
        user = self.db.register_user(email, "secret123", self.plan_id)
        return self.db.approve_user(user["id"])

    def provisioned_client(self, user):
        client = self.db.ensure_managed_client(user["id"], self.panel_id, 7, "vless", "", 1, user["expire_at"])
        self.db.update_managed_client_result(client["id"], state="provisioned", remote_enabled=True, error="")
        return self.db.get_managed_client(client["id"])

    def response_yaml(self, user):
        response = build_clash_subscription(self.db, user["token"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "text/yaml; charset=utf-8")
        return response, response.body

    def test_users_receive_distinct_uuids_without_changing_transport(self):
        first = self.active_user("one@example.com")
        second = self.active_user("two@example.com")
        first_client = self.provisioned_client(first)
        second_client = self.provisioned_client(second)

        first_response, first_body = self.response_yaml(first)
        second_response, second_body = self.response_yaml(second)

        self.assertIn(first_client["client_uuid"], first_response.body)
        self.assertNotIn(second_client["client_uuid"], first_response.body)
        self.assertIn(second_client["client_uuid"], second_response.body)
        self.assertNotEqual(first_client["client_uuid"], second_client["client_uuid"])
        self.assertIn('servername: "edge.example"', first_body)
        self.assertIn('network: "ws"', first_body)
        self.assertIn('path: "/edge"', first_body)

    def test_pending_or_failed_managed_targets_are_omitted_but_static_nodes_remain(self):
        user = self.active_user("user@example.com")
        self.db.create_node("Static", TROJAN, 1, ["premium"])
        pending = self.db.ensure_managed_client(user["id"], self.panel_id, 7, "vless", "", 1, user["expire_at"])
        self.db.update_managed_client_result(pending["id"], state="failed", remote_enabled=False, error="failed")

        _, body = self.response_yaml(user)

        self.assertIn('proxies:\n  - name: "Static"', body)
        self.assertNotIn('name: "Managed"', body)
        self.assertNotIn("template", body)

    def test_header_uses_separate_weighted_upload_and_download_totals(self):
        user = self.active_user("user@example.com")
        client = self.provisioned_client(user)
        self.db.advance_usage_ledger(client["id"], 10, 20, 3)

        response, _ = self.response_yaml(user)

        self.assertIn("upload=30; download=60;", response.headers["Subscription-Userinfo"])

    def test_custom_subscription_title_is_used_in_payload_and_profile_header(self):
        self.db.set_setting("subscription_title", "良心云")
        user = self.active_user("user@example.com")
        self.provisioned_client(user)

        response, body = self.response_yaml(user)

        profile_title = base64.b64decode(response.headers["Profile-Title"]).decode("utf-8")
        self.assertEqual(profile_title, "良心云")
        self.assertTrue(body.startswith('name: "良心云"'))

    def test_clash_subscription_is_yaml_for_broader_clash_client_compatibility(self):
        user = self.active_user("clash@example.com")
        self.provisioned_client(user)

        response = build_clash_subscription(self.db, user["token"])

        self.assertEqual(response.headers["Content-Type"], "text/yaml; charset=utf-8")
        self.assertTrue(response.body.startswith('name: "clash@example.com"'))
        self.assertIn('proxies:\n  - name: "Managed"', response.body)
        self.assertIn('proxy-groups:\n  - name: "Proxy"', response.body)
        self.assertNotIn("{", response.body.splitlines()[0])

    def test_exhausted_valid_token_returns_empty_200_with_metadata(self):
        tiny_plan = self.db.create_plan("Tiny", 0.000001, 30, ["premium"], True)
        user = self.db.register_user("user@example.com", "secret123", tiny_plan)
        user = self.db.approve_user(user["id"])
        client = self.provisioned_client(user)
        self.db.advance_usage_ledger(client["id"], 10 * 1024 * 1024, 0, 1)

        response, body = self.response_yaml(user)

        self.assertIn("proxies: []", body)
        self.assertIn("total=", response.headers["Subscription-Userinfo"])

    def test_base64_subscription_contains_client_share_links_for_mobile_apps(self):
        user = self.active_user("mobile@example.com")
        client = self.provisioned_client(user)
        self.db.create_node("Static", TROJAN, 1, ["premium"])

        response = build_base64_subscription(self.db, user["token"])
        decoded = base64.b64decode(response.body).decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn(client["client_uuid"], decoded)
        self.assertIn("trojan://pass@static.example.com", decoded)
        self.assertNotIn("template@", decoded)

    def test_singbox_subscription_contains_selectable_outbounds(self):
        user = self.active_user("singbox@example.com")
        client = self.provisioned_client(user)

        response = build_singbox_subscription(self.db, user["token"])
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(payload["outbounds"][0]["type"], "selector")
        vless = next(item for item in payload["outbounds"] if item["type"] == "vless")
        self.assertEqual(vless["uuid"], client["client_uuid"])
        self.assertEqual(vless["tls"]["server_name"], "edge.example")


if __name__ == "__main__":
    unittest.main()
