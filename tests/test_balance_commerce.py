import tempfile
import time
import unittest
from pathlib import Path

from xui_manager.billing import bytes_from_gb
from xui_manager.db import Database


class BalanceCommerceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "commerce.db")
        self.db.init_schema()
        self.admin = self.db.seed_admin("admin@example.com", "password123")
        self.user = self.db.register_user("buyer@example.com", "password123")

    def test_schema_migration_adds_balance_price_notes_and_recharge_tables(self):
        conn = self.db.connect()
        try:
            plan_columns = {row["name"] for row in conn.execute("pragma table_info(plans)")}
            user_columns = {row["name"] for row in conn.execute("pragma table_info(users)")}
            tables = {row["name"] for row in conn.execute("select name from sqlite_master where type='table'")}
        finally:
            conn.close()

        self.assertIn("price_cents", plan_columns)
        self.assertTrue({"product_type", "category", "description", "purchase_notice"}.issubset(plan_columns))
        self.assertTrue({"balance_cents", "admin_note", "is_priority"}.issubset(user_columns))
        self.assertTrue({"recharge_cards", "balance_transactions", "tutorials"}.issubset(tables))

    def test_product_metadata_is_persisted_on_create_and_update(self):
        plan_id = self.db.create_plan(
            "Starter",
            100,
            30,
            ["hk"],
            False,
            price_cents=1200,
            product_type="subscription",
            category="月付套餐",
            description="适合日常浏览、学习和远程办公。",
            purchase_notice="购买新套餐会立即替换旧套餐，旧套餐剩余流量和时长将作废。",
        )

        plan = self.db.get_plan(plan_id)

        self.assertEqual(plan["product_type"], "subscription")
        self.assertEqual(plan["category"], "月付套餐")
        self.assertEqual(plan["description"], "适合日常浏览、学习和远程办公。")
        self.assertIn("剩余流量和时长将作废", plan["purchase_notice"])

        updated = self.db.update_plan(
            plan_id,
            "Starter Traffic Pack",
            50,
            0,
            [],
            False,
            price_cents=600,
            product_type="traffic_pack",
            category="流量包",
            description="给当前套餐额外增加 50GB。",
            purchase_notice="流量包不会延长到期时间。",
        )

        self.assertEqual(updated["product_type"], "traffic_pack")
        self.assertEqual(updated["category"], "流量包")
        self.assertEqual(updated["description"], "给当前套餐额外增加 50GB。")
        self.assertEqual(updated["purchase_notice"], "流量包不会延长到期时间。")

    def test_purchase_plan_deducts_balance_activates_plan_and_resets_usage(self):
        plan_id = self.db.create_plan("Pro", 100, 30, [], False, price_cents=1299)
        node_id = self.db.create_node("Static", "vless://id@example.com:443#node", 1, [], True)
        self.db.record_usage(self.user["id"], node_id, 100, 200)
        self.db.adjust_user_balance(self.user["id"], 2000, "initial credit", self.admin["id"])

        purchased = self.db.purchase_plan(self.user["id"], plan_id)

        self.assertEqual(purchased["balance_cents"], 701)
        self.assertEqual(purchased["status"], "active")
        self.assertEqual(purchased["plan_id"], plan_id)
        self.assertEqual(self.db.usage_for_user(self.user["id"]), [])
        transactions = self.db.list_balance_transactions(self.user["id"])
        self.assertEqual(transactions[0]["kind"], "purchase")
        self.assertEqual(transactions[0]["amount_cents"], -1299)
        self.assertEqual(transactions[0]["balance_after_cents"], 701)

    def test_buying_new_subscription_replaces_old_cycle_and_abandons_remaining_value(self):
        old_plan_id = self.db.create_plan("Old Cycle", 100, 30, [], False, price_cents=1000)
        new_plan_id = self.db.create_plan("New Cycle", 200, 10, [], False, price_cents=1500)
        node_id = self.db.create_node("Static", "vless://id@example.com:443#node", 1, [], True)
        self.db.adjust_user_balance(self.user["id"], 5000, "initial credit", self.admin["id"])
        first = self.db.purchase_plan(self.user["id"], old_plan_id)
        self.db.record_usage(self.user["id"], node_id, bytes_from_gb(4), bytes_from_gb(6))

        before = int(time.time())
        replaced = self.db.purchase_plan(self.user["id"], new_plan_id)
        after = int(time.time())

        self.assertEqual(replaced["plan_id"], new_plan_id)
        self.assertEqual(replaced["quota_bytes"], bytes_from_gb(200))
        self.assertGreaterEqual(replaced["expire_at"], before + 10 * 86400)
        self.assertLessEqual(replaced["expire_at"], after + 10 * 86400 + 2)
        self.assertLess(replaced["expire_at"], first["expire_at"])
        self.assertEqual(self.db.usage_for_user(self.user["id"]), [])
        self.assertEqual(replaced["balance_cents"], 2500)

    def test_addon_packs_extend_current_subscription_without_replacing_it(self):
        base_plan_id = self.db.create_plan("Base", 50, 30, [], False, price_cents=1000)
        traffic_pack_id = self.db.create_plan(
            "20GB Traffic Pack",
            20,
            0,
            [],
            False,
            price_cents=500,
            product_type="traffic_pack",
            category="流量包",
        )
        time_pack_id = self.db.create_plan(
            "15 Days Time Pack",
            0,
            15,
            [],
            False,
            price_cents=400,
            product_type="time_pack",
            category="时长包",
        )
        reset_pack_id = self.db.create_plan(
            "Usage Reset Pack",
            0,
            0,
            [],
            False,
            price_cents=300,
            product_type="reset_pack",
            category="流量重置包",
        )
        node_id = self.db.create_node("Static", "vless://id@example.com:443#node", 1, [], True)
        self.db.adjust_user_balance(self.user["id"], 5000, "initial credit", self.admin["id"])
        base = self.db.purchase_plan(self.user["id"], base_plan_id)
        self.db.record_usage(self.user["id"], node_id, bytes_from_gb(2), bytes_from_gb(3))

        traffic_added = self.db.purchase_plan(self.user["id"], traffic_pack_id)
        usage_after_traffic = self.db.usage_for_user(self.user["id"])
        time_added = self.db.purchase_plan(self.user["id"], time_pack_id)
        self.db.record_usage(self.user["id"], node_id, bytes_from_gb(4), bytes_from_gb(6))
        reset = self.db.purchase_plan(self.user["id"], reset_pack_id)

        self.assertEqual(traffic_added["plan_id"], base_plan_id)
        self.assertEqual(traffic_added["quota_bytes"], bytes_from_gb(70))
        self.assertEqual(traffic_added["expire_at"], base["expire_at"])
        self.assertEqual(len(usage_after_traffic), 1)
        self.assertEqual(time_added["plan_id"], base_plan_id)
        self.assertEqual(time_added["quota_bytes"], bytes_from_gb(70))
        self.assertEqual(time_added["expire_at"], base["expire_at"] + 15 * 86400)
        self.assertEqual(reset["plan_id"], base_plan_id)
        self.assertEqual(reset["quota_bytes"], bytes_from_gb(70))
        self.assertEqual(reset["expire_at"], time_added["expire_at"])
        self.assertEqual(self.db.usage_for_user(self.user["id"]), [])

    def test_addon_packs_require_an_active_subscription(self):
        traffic_pack_id = self.db.create_plan(
            "20GB Traffic Pack",
            20,
            0,
            [],
            False,
            price_cents=0,
            product_type="traffic_pack",
            category="流量包",
        )

        with self.assertRaisesRegex(ValueError, "需要先开通有效套餐"):
            self.db.purchase_plan(self.user["id"], traffic_pack_id)

    def test_purchase_with_insufficient_balance_is_atomic(self):
        plan_id = self.db.create_plan("Premium", 300, 30, [], False, price_cents=5000)

        with self.assertRaisesRegex(ValueError, "余额不足"):
            self.db.purchase_plan(self.user["id"], plan_id)

        unchanged = self.db.get_user(self.user["id"])
        self.assertEqual(unchanged["balance_cents"], 0)
        self.assertEqual(unchanged["status"], "unsubscribed")
        self.assertEqual(self.db.list_balance_transactions(self.user["id"]), [])

    def test_recharge_card_can_only_be_redeemed_once(self):
        cards = self.db.create_recharge_cards(2500, 1, self.admin["id"])

        redeemed = self.db.redeem_recharge_card(self.user["id"], cards[0]["code"])

        self.assertEqual(redeemed["balance_cents"], 2500)
        with self.assertRaisesRegex(ValueError, "无效或已使用"):
            self.db.redeem_recharge_card(self.user["id"], cards[0]["code"])
        listed = self.db.list_recharge_cards()
        self.assertEqual(listed[0]["status"], "used")
        self.assertNotIn("code_hash", listed[0])
        self.assertNotIn(cards[0]["code"], str(listed[0]))

    def test_admin_adjustment_rejects_negative_result_and_note_marks_priority_user(self):
        credited = self.db.adjust_user_balance(self.user["id"], 800, "service credit", self.admin["id"])
        noted = self.db.update_user_note(self.user["id"], "长期客户", True)

        self.assertEqual(credited["balance_cents"], 800)
        self.assertEqual(noted["admin_note"], "长期客户")
        self.assertTrue(noted["is_priority"])
        with self.assertRaisesRegex(ValueError, "余额不能为负数"):
            self.db.adjust_user_balance(self.user["id"], -801, "invalid", self.admin["id"])
        self.assertEqual(self.db.get_user(self.user["id"])["balance_cents"], 800)


if __name__ == "__main__":
    unittest.main()
