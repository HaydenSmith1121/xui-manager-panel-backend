import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from xui_manager.db import Database


def build_old_database(path: Path) -> Database:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table plans (
            id integer primary key autoincrement,
            name text not null,
            quota_bytes integer not null,
            duration_days integer not null,
            allowed_tags text not null default '[]',
            require_approval integer not null default 1,
            enabled integer not null default 1,
            created_at integer not null
        );
        create table users (
            id integer primary key autoincrement,
            email text not null unique,
            password_hash text not null,
            role text not null default 'user',
            status text not null default 'pending',
            plan_id integer,
            token text not null unique,
            quota_bytes integer not null default 0,
            expire_at integer not null default 0,
            created_at integer not null,
            approved_at integer not null default 0,
            foreign key(plan_id) references plans(id)
        );
        create table panels (
            id integer primary key autoincrement,
            name text not null,
            base_url text not null,
            username text not null,
            password text not null,
            subscription_url text not null default '',
            verify_tls integer not null default 1,
            enabled integer not null default 1,
            created_at integer not null
        );
        create table nodes (
            id integer primary key autoincrement,
            name text not null,
            panel_id integer,
            source_url text not null,
            rate real not null default 1,
            tags text not null default '[]',
            enabled integer not null default 1,
            created_at integer not null,
            foreign key(panel_id) references panels(id)
        );

        insert into plans(id, name, quota_bytes, duration_days, created_at)
        values (1, 'Legacy', 1000, 30, 100);
        insert into users(id, email, password_hash, plan_id, token, created_at)
        values (1, 'legacy@example.com', 'hash', 1, 'legacy-token', 100);
        insert into panels(id, name, base_url, username, password, created_at)
        values (1, 'Legacy Panel', 'https://panel.example.com/', 'admin', 'secret', 100);
        insert into nodes(id, name, panel_id, source_url, rate, tags, created_at)
        values (1, 'Legacy US', 1, 'vless://legacy@example.com:443', 1, '["legacy"]', 100);
        """
    )
    conn.commit()
    conn.close()
    return Database(path)


class ManagedSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "app.db"

    def create_target(self):
        db = Database(self.db_path)
        db.init_schema()
        plan_id = db.create_plan("Managed", 100, 30, [], True)
        user = db.register_user("user@example.com", "secret123", plan_id)
        panel_id = db.create_panel("Panel", "https://panel.example.com", "admin", "secret")
        return db, user["id"], panel_id

    def test_old_database_migrates_twice_without_losing_static_rows(self):
        db = build_old_database(self.db_path)

        db.init_schema()
        db.init_schema()

        node = db.list_nodes()[0]
        self.assertEqual(node["name"], "Legacy US")
        self.assertEqual(node["mode"], "static")
        with db.session() as conn:
            self.assertEqual(conn.execute("select count(*) from users").fetchone()[0], 1)
            self.assertEqual(conn.execute("select count(*) from panels").fetchone()[0], 1)
            tables = {
                row["name"]
                for row in conn.execute("select name from sqlite_master where type='table'")
            }
            self.assertTrue({"managed_clients", "usage_ledgers", "app_settings"} <= tables)

            index_columns = {
                tuple(column["name"] for column in conn.execute(f"pragma index_info({index['name']})"))
                for index in conn.execute("pragma index_list(managed_clients)")
            }
            self.assertIn(("user_id", "state"), index_columns)
            self.assertIn(("panel_id", "inbound_id"), index_columns)

    def test_managed_tables_have_all_required_columns_defaults_and_constraints(self):
        db = Database(self.db_path)
        db.init_schema()

        with db.session() as conn:
            managed_columns = {
                row["name"]: (row["type"], row["notnull"], row["dflt_value"], row["pk"])
                for row in conn.execute("pragma table_info(managed_clients)")
            }
            self.assertEqual(
                managed_columns,
                {
                    "id": ("INTEGER", 0, None, 1),
                    "user_id": ("INTEGER", 1, None, 0),
                    "panel_id": ("INTEGER", 1, None, 0),
                    "inbound_id": ("INTEGER", 1, None, 0),
                    "protocol": ("TEXT", 1, "'vless'", 0),
                    "client_uuid": ("TEXT", 1, None, 0),
                    "remote_email": ("TEXT", 1, None, 0),
                    "flow": ("TEXT", 1, "''", 0),
                    "rate": ("REAL", 1, "1", 0),
                    "desired_expire_at": ("INTEGER", 1, "0", 0),
                    "desired_enabled": ("INTEGER", 1, "1", 0),
                    "state": ("TEXT", 1, "'pending'", 0),
                    "remote_enabled": ("INTEGER", 1, "0", 0),
                    "last_error": ("TEXT", 1, "''", 0),
                    "attempt_count": ("INTEGER", 1, "0", 0),
                    "last_attempt_at": ("INTEGER", 1, "0", 0),
                    "last_synced_at": ("INTEGER", 1, "0", 0),
                    "created_at": ("INTEGER", 1, None, 0),
                    "updated_at": ("INTEGER", 1, None, 0),
                },
            )

            ledger_columns = {
                row["name"]: (row["type"], row["notnull"], row["dflt_value"], row["pk"])
                for row in conn.execute("pragma table_info(usage_ledgers)")
            }
            self.assertEqual(
                ledger_columns,
                {
                    "managed_client_id": ("INTEGER", 0, None, 1),
                    "last_remote_up": ("INTEGER", 1, "0", 0),
                    "last_remote_down": ("INTEGER", 1, "0", 0),
                    "raw_up": ("INTEGER", 1, "0", 0),
                    "raw_down": ("INTEGER", 1, "0", 0),
                    "weighted_up": ("INTEGER", 1, "0", 0),
                    "weighted_down": ("INTEGER", 1, "0", 0),
                    "rate": ("REAL", 1, "1", 0),
                    "reset_pending": ("INTEGER", 1, "0", 0),
                    "updated_at": ("INTEGER", 1, "0", 0),
                },
            )

            settings_columns = {
                row["name"]: (row["type"], row["notnull"], row["dflt_value"], row["pk"])
                for row in conn.execute("pragma table_info(app_settings)")
            }
            self.assertEqual(
                settings_columns,
                {
                    "key": ("TEXT", 0, None, 1),
                    "value": ("TEXT", 1, None, 0),
                },
            )

            managed_foreign_keys = {
                (row["from"], row["table"], row["to"], row["on_delete"])
                for row in conn.execute("pragma foreign_key_list(managed_clients)")
            }
            self.assertEqual(
                managed_foreign_keys,
                {
                    ("user_id", "users", "id", "NO ACTION"),
                    ("panel_id", "panels", "id", "NO ACTION"),
                },
            )
            self.assertEqual(
                {
                    (row["from"], row["table"], row["to"], row["on_delete"])
                    for row in conn.execute("pragma foreign_key_list(usage_ledgers)")
                },
                {("managed_client_id", "managed_clients", "id", "CASCADE")},
            )

            unique_columns = {
                tuple(
                    column["name"]
                    for column in conn.execute(f"pragma index_info({index['name']})")
                )
                for index in conn.execute("pragma index_list(managed_clients)")
                if index["unique"]
            }
            self.assertIn(("user_id", "panel_id", "inbound_id"), unique_columns)

    def test_ensure_managed_client_is_unique_with_stable_uuid_and_label(self):
        db, user_id, panel_id = self.create_target()

        first = db.ensure_managed_client(user_id, panel_id, 7, "vless", "", 1.0, 123456)
        second = db.ensure_managed_client(user_id, panel_id, 7, "vless", "", 1.0, 123456)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["client_uuid"], second["client_uuid"])
        self.assertEqual(str(uuid.UUID(first["client_uuid"])), first["client_uuid"])
        self.assertEqual(first["remote_email"], f"xum-u{user_id}-p{panel_id}-i7")
        self.assertEqual(first["state"], "pending")
        self.assertIs(first["desired_enabled"], True)
        self.assertIs(first["remote_enabled"], False)
        self.assertEqual(len(db.list_managed_clients()), 1)

        with self.assertRaises(sqlite3.IntegrityError), db.session() as conn:
            conn.execute(
                """
                insert into managed_clients(
                    user_id, panel_id, inbound_id, client_uuid, remote_email, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, panel_id, 7, str(uuid.uuid4()), "duplicate", 1, 1),
            )

    def test_managed_client_state_desired_values_and_filters_are_decoded(self):
        db, user_id, panel_id = self.create_target()
        client = db.ensure_managed_client(user_id, panel_id, 3, "vless", "vision", 2.0, 1000)

        db.update_managed_client_result(
            client["id"], state="provisioned", remote_enabled=True, error=""
        )
        db.set_managed_client_desired(client["id"], enabled=False, expire_at=2000)
        db.set_managed_client_rate(client["id"], 3.0)

        updated = db.get_managed_client(client["id"])
        self.assertEqual(updated["state"], "provisioned")
        self.assertIs(updated["remote_enabled"], True)
        self.assertIs(updated["desired_enabled"], False)
        self.assertEqual(updated["desired_expire_at"], 2000)
        self.assertEqual(updated["rate"], 3.0)
        self.assertEqual(updated["attempt_count"], 1)
        self.assertGreater(updated["last_attempt_at"], 0)
        self.assertEqual(
            db.get_managed_client_for_target(user_id, panel_id, 3)["id"], client["id"]
        )
        self.assertEqual([row["id"] for row in db.list_managed_clients(user_id)], [client["id"]])
        self.assertEqual(
            [row["id"] for row in db.list_managed_clients(states=["provisioned"])],
            [client["id"]],
        )
        self.assertEqual(db.list_managed_clients(states=["failed"]), [])

    def test_usage_ledger_tracks_deltas_rate_changes_and_remote_resets(self):
        db, user_id, panel_id = self.create_target()
        client = db.ensure_managed_client(user_id, panel_id, 1, "vless", "", 2.0, 0)

        first = db.advance_usage_ledger(client["id"], remote_up=100, remote_down=50, rate=2.0)
        second = db.advance_usage_ledger(client["id"], remote_up=130, remote_down=70, rate=3.0)
        reset = db.advance_usage_ledger(client["id"], remote_up=5, remote_down=4, rate=3.0)

        self.assertEqual(first["raw_up"], 100)
        self.assertEqual(first["weighted_up"], 200)
        self.assertEqual(second["raw_up"], 130)
        self.assertEqual(second["raw_down"], 70)
        self.assertEqual(second["weighted_up"], 290)
        self.assertEqual(second["weighted_down"], 160)
        self.assertEqual(reset["last_remote_up"], 5)
        self.assertEqual(reset["last_remote_down"], 4)
        self.assertEqual(reset["raw_up"], 135)
        self.assertEqual(reset["raw_down"], 74)
        self.assertEqual(reset["weighted_up"], 305)
        self.assertEqual(reset["weighted_down"], 172)
        self.assertEqual(db.get_usage_ledger(client["id"]), reset)

    def test_managed_totals_and_reset_cover_all_clients_for_one_user(self):
        db, user_id, panel_id = self.create_target()
        first = db.ensure_managed_client(user_id, panel_id, 1, "vless", "", 1.0, 0)
        second = db.ensure_managed_client(user_id, panel_id, 2, "vless", "", 0.5, 0)
        db.advance_usage_ledger(first["id"], 100, 40, 1.0)
        db.advance_usage_ledger(second["id"], 80, 20, 0.5)

        self.assertEqual(db.managed_usage_totals(user_id), {"upload": 140, "download": 50})

        db.reset_managed_usage(user_id)

        self.assertEqual(db.managed_usage_totals(user_id), {"upload": 0, "download": 0})
        for client, expected_up, expected_down in ((first, 100, 40), (second, 80, 20)):
            ledger = db.get_usage_ledger(client["id"])
            self.assertEqual(ledger["last_remote_up"], expected_up)
            self.assertEqual(ledger["last_remote_down"], expected_down)
            self.assertEqual(ledger["raw_up"], 0)
            self.assertEqual(ledger["raw_down"], 0)
            self.assertEqual(ledger["weighted_up"], 0)
            self.assertEqual(ledger["weighted_down"], 0)

    def test_post_reset_sync_bills_only_new_remote_deltas(self):
        db, user_id, panel_id = self.create_target()
        client = db.ensure_managed_client(user_id, panel_id, 1, "vless", "", 2.0, 0)
        db.advance_usage_ledger(client["id"], 100, 40, 2.0)

        db.reset_managed_usage(user_id)
        ledger = db.advance_usage_ledger(client["id"], 115, 45, 2.0)

        self.assertEqual(ledger["raw_up"], 15)
        self.assertEqual(ledger["raw_down"], 5)
        self.assertEqual(ledger["weighted_up"], 30)
        self.assertEqual(ledger["weighted_down"], 10)
        self.assertEqual(db.managed_usage_totals(user_id), {"upload": 30, "download": 10})

    def test_reset_without_prior_ledger_baselines_first_post_reset_sync(self):
        db, user_id, panel_id = self.create_target()
        client = db.ensure_managed_client(user_id, panel_id, 1, "vless", "", 2.0, 0)

        db.reset_managed_usage(user_id)
        baseline = db.advance_usage_ledger(client["id"], 500, 200, 2.0)
        billed = db.advance_usage_ledger(client["id"], 550, 230, 2.0)

        self.assertEqual(baseline["last_remote_up"], 500)
        self.assertEqual(baseline["last_remote_down"], 200)
        self.assertEqual(baseline["raw_up"], 0)
        self.assertEqual(baseline["raw_down"], 0)
        self.assertEqual(baseline["weighted_up"], 0)
        self.assertEqual(baseline["weighted_down"], 0)
        self.assertEqual(billed["raw_up"], 50)
        self.assertEqual(billed["raw_down"], 30)
        self.assertEqual(billed["weighted_up"], 100)
        self.assertEqual(billed["weighted_down"], 60)
        self.assertEqual(db.managed_usage_totals(user_id), {"upload": 100, "download": 60})

    def test_deleting_managed_client_cascades_to_usage_ledger(self):
        db, user_id, panel_id = self.create_target()
        client = db.ensure_managed_client(user_id, panel_id, 1, "vless", "", 1.0, 0)
        db.advance_usage_ledger(client["id"], 10, 20, 1.0)

        with db.session() as conn:
            conn.execute("delete from managed_clients where id=?", (client["id"],))

        self.assertIsNone(db.get_managed_client(client["id"]))
        self.assertIsNone(db.get_usage_ledger(client["id"]))

    def test_delete_panel_rejects_managed_client_usage_with_value_error(self):
        db, user_id, panel_id = self.create_target()
        db.ensure_managed_client(user_id, panel_id, 1, "vless", "", 1.0, 0)

        with self.assertRaisesRegex(ValueError, "panel is in use"):
            db.delete_panel(panel_id)

    def test_managed_client_mutators_reject_missing_client_id(self):
        db, _, _ = self.create_target()

        with self.assertRaisesRegex(ValueError, "managed client not found"):
            db.update_managed_client_result(
                999, state="failed", remote_enabled=False, error="missing"
            )
        with self.assertRaisesRegex(ValueError, "managed client not found"):
            db.set_managed_client_desired(999, enabled=False, expire_at=123)
        with self.assertRaisesRegex(ValueError, "managed client not found"):
            db.set_managed_client_rate(999, 2.0)

    def test_settings_and_reinitialization_preserve_migrated_data(self):
        db, user_id, panel_id = self.create_target()
        client = db.ensure_managed_client(user_id, panel_id, 9, "vless", "", 1.0, 0)
        db.advance_usage_ledger(client["id"], 11, 12, 1.0)

        self.assertEqual(db.get_setting("sync_interval", "300"), "300")
        db.set_setting("sync_interval", "600")
        db.init_schema()
        db.init_schema()

        same = db.ensure_managed_client(user_id, panel_id, 9, "vless", "", 1.0, 0)
        self.assertEqual(same["id"], client["id"])
        self.assertEqual(same["client_uuid"], client["client_uuid"])
        self.assertEqual(db.get_usage_ledger(client["id"])["raw_up"], 11)
        self.assertEqual(db.get_setting("sync_interval", "300"), "600")


if __name__ == "__main__":
    unittest.main()
