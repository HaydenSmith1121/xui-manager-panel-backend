import json
import tempfile
import unittest
from pathlib import Path

from xui_manager.billing import usage_totals
from xui_manager.db import Database
from xui_manager.provisioning import ProvisioningService
from xui_manager.usage_sync import UsageSyncService


GB = 1024 * 1024 * 1024
VLESS = "vless://template@example.com:443?security=reality&flow=xtls-rprx-vision#US"


def inbound(inbound_id=1, *, stats=None, clients=None):
    return {
        "id": inbound_id,
        "protocol": "vless",
        "settings": json.dumps({"clients": list(clients or [])}),
        "clientStats": list(stats or []),
    }


class FakePanelClient:
    def __init__(self, inbounds=None, *, fail=False, fail_message="panel down"):
        self.inbounds = {int(item["id"]): item for item in (inbounds or [inbound(1)])}
        self.fail = fail
        self.fail_message = fail_message
        self.login_calls = 0
        self.list_calls = 0
        self.update_calls = 0

    def login(self):
        self.login_calls += 1
        if self.fail:
            raise RuntimeError(self.fail_message)

    def list_inbounds(self):
        self.list_calls += 1
        if self.fail:
            raise RuntimeError("list failed")
        return list(self.inbounds.values())

    def get_inbound(self, inbound_id):
        if self.fail:
            raise RuntimeError("panel down")
        return self.inbounds.get(int(inbound_id), {})

    def find_client(self, inbound_data, email):
        settings = inbound_data.get("settings") or "{}"
        if isinstance(settings, str):
            settings = json.loads(settings)
        for client in settings.get("clients") or []:
            if client.get("email") == email:
                return dict(client)
        return None

    def add_vless_client(self, *, inbound_id, client_uuid, email, flow, expire_at):
        client = {"id": client_uuid, "email": email, "flow": flow, "enable": True}
        self._store_client(inbound_id, client)
        return dict(client)

    def update_vless_client(self, *, inbound_id, client_uuid, email, flow, expire_at, enabled):
        self.update_calls += 1
        client = {"id": client_uuid, "email": email, "flow": flow, "enable": bool(enabled)}
        self._store_client(inbound_id, client)
        return dict(client)

    def set_traffic(self, email, *, up=0, down=0, inbound_id=1):
        self.inbounds[int(inbound_id)]["clientStats"] = [{"email": email, "up": up, "down": down}]

    def _store_client(self, inbound_id, client):
        item = self.inbounds[int(inbound_id)]
        settings = json.loads(item.get("settings") or "{}")
        clients = [existing for existing in settings.get("clients", []) if existing.get("email") != client["email"]]
        clients.append(client)
        item["settings"] = json.dumps({"clients": clients})


class UsageSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "app.db")
        self.db.init_schema()
        self.plan_id = self.db.create_plan("Premium", 100, 30, ["premium"], True)
        self.user = self.db.register_user("user@example.com", "secret123", self.plan_id)
        self.user = self.db.approve_user(self.user["id"])
        self.panel_id = self.db.create_panel("Panel", "https://panel.example.com", "admin", "secret")
        self.db.create_node("Managed", VLESS, 3, ["premium"], True, self.panel_id, 1, "managed")
        self.remote = FakePanelClient([inbound(1)])
        self.clients = {"https://panel.example.com/": self.remote}

    def factory(self, base_url, username, password, verify_tls=True):
        return self.clients[base_url]

    def provision(self):
        ProvisioningService(self.db, self.factory).provision_user(self.user["id"])
        return self.db.list_managed_clients(user_id=self.user["id"])[0]

    def service(self, now=None):
        provisioning = ProvisioningService(self.db, self.factory, now=now or (lambda: 1_700_000_000))
        return UsageSyncService(self.db, provisioning, self.factory, now=now or (lambda: 1_700_000_000))

    def test_multiplier_applies_only_to_new_remote_delta(self):
        managed = self.provision()
        self.remote.set_traffic(managed["remote_email"], up=1 * GB)

        self.service().sync_all()
        self.db.set_managed_client_rate(managed["id"], 1)
        self.remote.set_traffic(managed["remote_email"], up=2 * GB)
        self.service().sync_all()

        self.assertEqual(self.db.managed_usage_totals(self.user["id"])["upload"], 4 * GB)

    def test_counter_reset_preserves_history_and_counts_new_bytes(self):
        managed = self.provision()
        self.remote.set_traffic(managed["remote_email"], down=10 * GB)
        self.service().sync_all()
        self.remote.set_traffic(managed["remote_email"], down=2 * GB)

        self.service().sync_all()

        self.assertEqual(self.db.managed_usage_totals(self.user["id"])["download"], 12 * 3 * GB)

    def test_sync_groups_by_panel_and_reports_stale_panel_error(self):
        managed = self.provision()
        self.remote.set_traffic(managed["remote_email"], up=1, down=2)
        self.remote.fail = True

        result = self.service().sync_all()

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["errors"][0]["panel_id"], self.panel_id)
        self.assertEqual(self.remote.login_calls, 2)  # provisioning once, sync once

    def test_sync_panel_error_redacts_panel_password(self):
        self.provision()
        self.remote.fail = True
        self.remote.fail_message = "secret login failed"
        result = self.service().sync_all()

        self.assertEqual(result["synced"], 0)
        self.assertNotIn("secret", result["errors"][0]["error"])

    def test_enforcement_disables_remote_when_quota_is_exhausted(self):
        managed = self.provision()
        self.remote.set_traffic(managed["remote_email"], up=200 * GB)

        result = self.service().sync_all()

        refreshed = self.db.list_managed_clients(user_id=self.user["id"])[0]
        self.assertEqual(result["disabled"], 1)
        self.assertFalse(refreshed["desired_enabled"])
        self.assertFalse(refreshed["remote_enabled"])
        self.assertEqual(self.remote.update_calls, 1)

    def test_enforcement_disables_remote_when_account_expired(self):
        managed = self.provision()
        now = lambda: self.user["expire_at"] + 1
        self.remote.set_traffic(managed["remote_email"], up=1)

        self.service(now=now).sync_all()

        refreshed = self.db.list_managed_clients(user_id=self.user["id"])[0]
        self.assertFalse(refreshed["desired_enabled"])
        self.assertFalse(refreshed["remote_enabled"])

    def test_expired_user_is_disabled_even_when_remote_has_no_traffic_stat(self):
        self.provision()
        now = lambda: self.user["expire_at"] + 1

        result = self.service(now=now).sync_all()

        refreshed = self.db.list_managed_clients(user_id=self.user["id"])[0]
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["disabled"], 1)
        self.assertFalse(refreshed["desired_enabled"])
        self.assertFalse(refreshed["remote_enabled"])

    def test_usage_totals_combines_legacy_static_and_managed_but_excludes_managed_legacy_records(self):
        managed = self.provision()
        static_id = self.db.create_node("Static", "trojan://pass@example.com:443", 2, ["premium"])
        managed_node = [node for node in self.db.list_nodes() if node["mode"] == "managed"][0]
        self.db.record_usage(self.user["id"], static_id, 10, 20)
        self.db.record_usage(self.user["id"], managed_node["id"], 1000, 1000)
        self.remote.set_traffic(managed["remote_email"], up=5, down=7)
        self.service().sync_all()

        totals = usage_totals(self.db, self.user["id"])

        self.assertEqual(totals["upload"], 20 + 15)
        self.assertEqual(totals["download"], 40 + 21)


if __name__ == "__main__":
    unittest.main()
