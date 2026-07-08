import json
import tempfile
import unittest
from pathlib import Path

from xui_manager.db import Database
from xui_manager.provisioning import ProvisioningService


UUID_OTHER = "99999999-8888-4777-9666-555555555555"
VLESS = "vless://template@example.com:443?security=reality&flow=xtls-rprx-vision#US"


def inbound(inbound_id=1, *, protocol="vless", clients=None, remark="primary"):
    return {
        "id": inbound_id,
        "remark": remark,
        "protocol": protocol,
        "settings": json.dumps({"clients": list(clients or [])}),
        "clientStats": [],
    }


class FakePanelClient:
    def __init__(self, inbounds, *, fail=False, fail_message="panel password secret-token failed"):
        self.inbounds = {int(item["id"]): item for item in inbounds}
        self.fail = fail
        self.fail_message = fail_message
        self.login_calls = 0
        self.add_calls = 0
        self.update_calls = 0
        self.delete_calls = 0

    def recover(self):
        self.fail = False

    def login(self):
        self.login_calls += 1
        if self.fail:
            raise RuntimeError(self.fail_message)

    def get_inbound(self, inbound_id):
        if self.fail:
            raise RuntimeError("panel unavailable")
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
        self.add_calls += 1
        if self.fail:
            raise RuntimeError("add failed")
        client = {
            "id": client_uuid,
            "email": email,
            "flow": flow,
            "expiryTime": int(expire_at) * 1000 if expire_at else 0,
            "enable": True,
        }
        self._store_client(inbound_id, client)
        return dict(client)

    def update_vless_client(self, *, inbound_id, client_uuid, email, flow, expire_at, enabled):
        self.update_calls += 1
        if self.fail:
            raise RuntimeError("update failed")
        client = {
            "id": client_uuid,
            "email": email,
            "flow": flow,
            "expiryTime": int(expire_at) * 1000 if expire_at else 0,
            "enable": bool(enabled),
        }
        self._store_client(inbound_id, client)
        return dict(client)

    def delete_vless_client(self, *, inbound_id, client_uuid, email):
        self.delete_calls += 1
        if self.fail:
            raise RuntimeError("delete failed secret-token")
        item = self.inbounds[int(inbound_id)]
        settings = json.loads(item.get("settings") or "{}")
        settings["clients"] = [client for client in settings.get("clients", []) if client.get("id") != client_uuid]
        item["settings"] = json.dumps(settings)
        return True

    def _store_client(self, inbound_id, client):
        item = self.inbounds[int(inbound_id)]
        settings = json.loads(item.get("settings") or "{}")
        clients = [existing for existing in settings.get("clients", []) if existing.get("email") != client["email"]]
        clients.append(client)
        item["settings"] = json.dumps({"clients": clients})


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "app.db")
        self.db.init_schema()
        self.plan_id = self.db.create_plan("Premium", 100, 30, ["premium"], True)
        self.user = self.db.register_user("user@example.com", "secret123", self.plan_id)
        self.user = self.db.approve_user(self.user["id"])
        self.clients = {}

    def panel(self, name, base, *, enabled=True, fake=None):
        panel_id = self.db.create_panel(name, base, "admin", "secret", enabled=enabled)
        self.clients[self.db.list_panels()[-1]["base_url"]] = fake or FakePanelClient([inbound(1)])
        return panel_id

    def factory(self, base_url, username, password, verify_tls=True):
        return self.clients[base_url]

    def managed_node(self, panel_id, inbound_id=1, *, tags=None, rate=1, enabled=True, source_url=VLESS):
        return self.db.create_node(
            "Managed",
            source_url,
            rate,
            tags or ["premium"],
            enabled,
            panel_id,
            inbound_id,
            "managed",
        )

    def test_provision_user_filters_plan_tags_deduplicates_target_and_is_idempotent(self):
        panel_id = self.panel("Panel", "https://panel.example.com", fake=FakePanelClient([inbound(1), inbound(2)]))
        self.managed_node(panel_id, 1, rate=2)
        self.managed_node(panel_id, 1, rate=2)
        self.managed_node(panel_id, 2, tags=["standard"])

        service = ProvisioningService(self.db, self.factory)
        first = service.provision_user(self.user["id"])
        second = service.provision_user(self.user["id"])

        client = self.db.list_managed_clients(user_id=self.user["id"])[0]
        fake = next(iter(self.clients.values()))
        self.assertEqual(first, {"provisioned": 1, "failed": 0, "pending": 0})
        self.assertEqual(second, {"provisioned": 1, "failed": 0, "pending": 0})
        self.assertEqual(fake.add_calls, 1)
        self.assertEqual(fake.update_calls, 1)
        self.assertEqual(client["remote_email"], f"xum-u{self.user['id']}-p{panel_id}-i1")
        self.assertEqual(client["rate"], 2)
        self.assertEqual(client["flow"], "xtls-rprx-vision")

    def test_partial_failure_keeps_success_and_retry_is_idempotent(self):
        good = FakePanelClient([inbound(1)])
        bad = FakePanelClient([inbound(1)], fail=True)
        good_panel = self.panel("Good", "https://good.example.com", fake=good)
        bad_panel = self.panel("Bad", "https://bad.example.com", fake=bad)
        self.managed_node(good_panel, 1)
        self.managed_node(bad_panel, 1)

        service = ProvisioningService(self.db, self.factory)
        first = service.provision_user(self.user["id"])
        bad.recover()
        second = service.retry_user(self.user["id"])

        self.assertEqual(first, {"provisioned": 1, "failed": 1, "pending": 0})
        self.assertEqual(second, {"provisioned": 2, "failed": 0, "pending": 0})
        self.assertEqual(good.add_calls + bad.add_calls, 2)

    def test_remote_uuid_conflict_is_stored_safely_without_mutation(self):
        panel_id = self.panel("Panel", "https://panel.example.com")
        self.managed_node(panel_id, 1)
        existing = {
            "id": UUID_OTHER,
            "email": f"xum-u{self.user['id']}-p{panel_id}-i1",
            "flow": "xtls-rprx-vision",
            "enable": True,
        }
        fake = FakePanelClient([inbound(1, clients=[existing])])
        self.clients[self.db.list_panels()[-1]["base_url"]] = fake

        result = ProvisioningService(self.db, self.factory).provision_user(self.user["id"])

        client = self.db.list_managed_clients(user_id=self.user["id"])[0]
        self.assertEqual(result, {"provisioned": 0, "failed": 1, "pending": 0})
        self.assertEqual(fake.add_calls, 0)
        self.assertEqual(fake.update_calls, 0)
        self.assertIn("conflict", client["last_error"])
        self.assertNotIn(UUID_OTHER, client["last_error"])
        self.assertNotIn(client["client_uuid"], client["last_error"])

    def test_panel_password_is_not_stored_in_failure_error(self):
        panel_id = self.db.create_panel("Panel", "https://panel.example.com", "admin", "plainpass123")
        self.clients[self.db.list_panels()[-1]["base_url"]] = FakePanelClient(
            [inbound(1)],
            fail=True,
            fail_message="plainpass123 login failed",
        )
        self.managed_node(panel_id, 1)

        ProvisioningService(self.db, self.factory).provision_user(self.user["id"])

        client = self.db.list_managed_clients(user_id=self.user["id"])[0]
        self.assertNotIn("plainpass123", client["last_error"])

    def test_disabled_panel_is_skipped(self):
        panel_id = self.panel("Disabled", "https://disabled.example.com", enabled=False)
        self.managed_node(panel_id, 1)

        result = ProvisioningService(self.db, self.factory).provision_user(self.user["id"])

        self.assertEqual(result, {"provisioned": 0, "failed": 0, "pending": 0})
        self.assertEqual(self.db.list_managed_clients(user_id=self.user["id"]), [])

    def test_status_ignores_obsolete_failed_targets_after_node_inbound_changes(self):
        panel_id = self.panel(
            "Panel",
            "https://panel.example.com",
            fake=FakePanelClient([inbound(10)]),
        )
        obsolete = self.db.ensure_managed_client(
            self.user["id"],
            panel_id,
            9,
            "vless",
            "",
            1,
            self.user["expire_at"],
        )
        self.db.update_managed_client_result(
            obsolete["id"],
            state="failed",
            remote_enabled=False,
            error="Obtain (record not found)",
        )
        self.managed_node(panel_id, 10)

        service = ProvisioningService(self.db, self.factory)
        result = service.provision_user(self.user["id"])

        self.assertEqual(result, {"provisioned": 1, "failed": 0, "pending": 0})
        self.assertEqual(service.failure_details_for_user(self.user["id"]), [])

    def test_set_user_enabled_reconciles_remote_enabled_state(self):
        panel_id = self.panel("Panel", "https://panel.example.com")
        self.managed_node(panel_id, 1)
        service = ProvisioningService(self.db, self.factory)
        service.provision_user(self.user["id"])

        result = service.set_user_enabled(self.user["id"], False)

        fake = next(iter(self.clients.values()))
        client = self.db.list_managed_clients(user_id=self.user["id"])[0]
        self.assertEqual(result, {"provisioned": 1, "failed": 0, "pending": 0})
        self.assertEqual(fake.update_calls, 1)
        self.assertFalse(client["desired_enabled"])
        self.assertFalse(client["remote_enabled"])

    def test_activate_purchased_plan_disables_old_targets_before_enabling_new_targets(self):
        fake = FakePanelClient([inbound(1), inbound(2)])
        panel_id = self.panel("Panel", "https://panel.example.com", fake=fake)
        self.managed_node(panel_id, 1)
        service = ProvisioningService(self.db, self.factory, now=lambda: 1_700_000_000)
        service.provision_user(self.user["id"])
        basic_plan = self.db.create_plan("Basic", 30, 30, ["basic"], False)
        self.db.create_node("Basic", VLESS, 1, ["basic"], True, panel_id, 2, "managed")
        self.db.purchase_plan(self.user["id"], basic_plan)

        summary = service.activate_purchased_plan(self.user["id"])

        managed = {item["inbound_id"]: item for item in self.db.list_managed_clients(user_id=self.user["id"])}
        old_client = fake.find_client(fake.get_inbound(1), managed[1]["remote_email"])
        new_client = fake.find_client(fake.get_inbound(2), managed[2]["remote_email"])
        self.assertFalse(old_client["enable"])
        self.assertTrue(new_client["enable"])
        self.assertEqual(summary["provisioned"], 1)

    def test_delete_user_removes_remote_client_then_local_records(self):
        panel_id = self.panel("Panel", "https://panel.example.com")
        node_id = self.managed_node(panel_id, 1)
        service = ProvisioningService(self.db, self.factory)
        service.provision_user(self.user["id"])
        self.db.record_usage(self.user["id"], node_id, 10, 20)
        session = self.db.create_session(self.user["id"])
        self.db.update_user_status(self.user["id"], "disabled")

        result = service.delete_user(self.user["id"])

        fake = next(iter(self.clients.values()))
        self.assertEqual(result, {"deleted": True, "errors": []})
        self.assertEqual(fake.delete_calls, 1)
        self.assertIsNone(self.db.get_user(self.user["id"]))
        self.assertIsNone(self.db.get_session_user(session))
        self.assertEqual(self.db.list_managed_clients(user_id=self.user["id"]), [])

    def test_delete_user_preserves_local_records_when_any_panel_fails(self):
        good = FakePanelClient([inbound(1)])
        bad = FakePanelClient([inbound(1)])
        good_panel = self.panel("Good", "https://good.example.com", fake=good)
        bad_panel = self.panel("Bad", "https://bad.example.com", fake=bad)
        self.managed_node(good_panel, 1)
        self.managed_node(bad_panel, 1)
        service = ProvisioningService(self.db, self.factory)
        service.provision_user(self.user["id"])
        self.db.update_user_status(self.user["id"], "disabled")
        bad.fail = True

        result = service.delete_user(self.user["id"])

        self.assertFalse(result["deleted"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["panel_name"], "Bad")
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertIsNotNone(self.db.get_user(self.user["id"]))
        self.assertEqual(len(self.db.list_managed_clients(user_id=self.user["id"])), 2)

    def test_delete_user_treats_already_missing_remote_client_as_success(self):
        panel_id = self.panel("Panel", "https://panel.example.com")
        self.managed_node(panel_id, 1)
        service = ProvisioningService(self.db, self.factory)
        service.provision_user(self.user["id"])
        fake = next(iter(self.clients.values()))
        fake.inbounds[1]["settings"] = json.dumps({"clients": []})
        self.db.update_user_status(self.user["id"], "disabled")

        result = service.delete_user(self.user["id"])

        self.assertTrue(result["deleted"])
        self.assertIsNone(self.db.get_user(self.user["id"]))

    def test_delete_user_requires_disabled_non_admin_account(self):
        service = ProvisioningService(self.db, self.factory)

        with self.assertRaisesRegex(ValueError, "disabled"):
            service.delete_user(self.user["id"])


if __name__ == "__main__":
    unittest.main()
