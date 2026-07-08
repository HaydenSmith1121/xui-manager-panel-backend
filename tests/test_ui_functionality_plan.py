from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xui_manager.app import XuiManagerApp
from xui_manager.billing import bytes_from_gb


class UiFunctionalityPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.secret_patch = patch.dict(os.environ, {"RECHARGE_CARD_SECRET": "test-secret-key"})
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)
        self.app = XuiManagerApp(Path(self.tmp.name) / "app.db")
        self.admin = self.app.db.seed_admin("admin@example.com", "password123")
        login = self.app.handle_json(
            "POST",
            "/api/login",
            {},
            json.dumps({"email": "admin@example.com", "password": "password123"}),
        )
        self.admin_headers = {
            "Cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }

    def post_admin(self, path, payload):
        return self.app.handle_json("POST", path, self.admin_headers, json.dumps(payload))

    def login_user_headers(self, email, password="secret123"):
        login = self.app.handle_json(
            "POST", "/api/login", {}, json.dumps({"email": email, "password": password})
        )
        return {
            "Cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "Host": "manager.example.com",
            "Content-Type": "application/json",
        }

    def test_public_store_seeds_three_purchase_plans_when_empty(self):
        response = self.app.handle_json("GET", "/api/plans", {"Host": "manager.example.com"}, "")
        payload = json.loads(response.body)
        plans = payload["plans"]

        self.assertEqual(response.status, 200)
        self.assertEqual([plan["name"] for plan in plans[:3]], ["入门套餐", "日常套餐", "畅享套餐"])
        self.assertEqual([plan["price_cents"] for plan in plans[:3]], [990, 1990, 3990])
        self.assertEqual([plan["duration_days"] for plan in plans[:3]], [30, 30, 30])
        self.assertEqual([plan["quota_bytes"] for plan in plans[:3]], [bytes_from_gb(30), bytes_from_gb(100), bytes_from_gb(300)])
        self.assertTrue(all(isinstance(plan["id"], int) for plan in plans[:3]))

        second = self.app.handle_json("GET", "/api/plans", {"Host": "manager.example.com"}, "")
        self.assertEqual(len(json.loads(second.body)["plans"]), len(plans))

    def test_logged_in_user_can_buy_seeded_store_plan(self):
        plans = json.loads(self.app.handle_json("GET", "/api/plans", {"Host": "manager.example.com"}, "").body)["plans"]
        user = self.app.db.register_user("buyer@example.com", "secret123")
        self.app.db.adjust_user_balance(user["id"], 5000, "充值测试", self.admin["id"])
        headers = self.login_user_headers("buyer@example.com")

        purchase = self.app.handle_json(
            "POST",
            "/api/purchases",
            headers,
            json.dumps({"plan_id": plans[0]["id"]}),
        )
        payload = json.loads(purchase.body)

        self.assertEqual(purchase.status, 200)
        self.assertEqual(payload["user"]["plan_id"], plans[0]["id"])
        self.assertEqual(payload["user"]["status"], "active")
        self.assertEqual(payload["user"]["balance_cents"], 4010)

    def test_admin_can_reveal_encrypted_recharge_card_but_legacy_card_stays_masked(self):
        generated = self.post_admin("/api/admin/recharge-cards", {"amount_yuan": 20, "count": 1})
        card = json.loads(generated.body)["cards"][0]

        listed = self.app.handle_json("GET", "/api/admin/recharge-cards", self.admin_headers, "")
        listed_card = json.loads(listed.body)["cards"][0]
        revealed = self.app.handle_json(
            "POST", "/api/admin/recharge-cards/reveal", self.admin_headers, json.dumps({"id": card["id"]})
        )

        self.assertEqual(generated.status, 200)
        self.assertEqual(listed.status, 200)
        self.assertTrue(listed_card["can_reveal"])
        self.assertNotIn(card["code"], listed.body)
        self.assertEqual(json.loads(revealed.body)["code"], card["code"])

        legacy = self.app.db.create_recharge_cards(500, 1, self.admin["id"], encryption_secret="")
        legacy_reveal = self.app.handle_json(
            "POST", "/api/admin/recharge-cards/reveal", self.admin_headers, json.dumps({"id": legacy[0]["id"]})
        )

        self.assertEqual(legacy_reveal.status, 400)
        self.assertIn("无法查看完整卡密", legacy_reveal.body)

    def test_admin_can_generate_recharge_card_without_secret_but_cannot_reveal_it(self):
        with patch.dict(os.environ, {"RECHARGE_CARD_SECRET": ""}):
            app = XuiManagerApp(Path(self.tmp.name) / "missing-secret.db")
            admin = app.db.seed_admin("admin2@example.com", "password123")
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

            response = app.handle_json(
                "POST", "/api/admin/recharge-cards", headers, json.dumps({"amount_yuan": 5, "count": 1})
            )
            payload = json.loads(response.body)
            listed = app.handle_json("GET", "/api/admin/recharge-cards", headers, "")
            reveal = app.handle_json(
                "POST",
                "/api/admin/recharge-cards/reveal",
                headers,
                json.dumps({"id": payload["cards"][0]["id"]}),
            )

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["cards"][0]["code"].startswith("HXY-"))
        self.assertFalse(json.loads(listed.body)["cards"][0]["can_reveal"])
        self.assertEqual(reveal.status, 400)
        self.assertIn("无法查看完整卡密", reveal.body)

    def test_admin_plan_api_persists_product_metadata(self):
        created = self.post_admin(
            "/api/admin/plans",
            {
                "name": "入门套餐",
                "price_yuan": 19.9,
                "quota_gb": 100,
                "duration_days": 30,
                "allowed_tags": "hk,jp",
                "enabled": True,
                "product_type": "subscription",
                "category": "月付套餐",
                "description": "适合第一次使用的新用户。",
                "purchase_notice": "购买新套餐会替换旧套餐，剩余流量和时长不保留。",
            },
        )
        plan_id = json.loads(created.body)["id"]
        updated = self.post_admin(
            "/api/admin/plans",
            {
                "id": plan_id,
                "name": "流量重置包",
                "price_yuan": 5,
                "quota_gb": 0,
                "duration_days": 0,
                "allowed_tags": "",
                "enabled": True,
                "product_type": "reset_pack",
                "category": "工具包",
                "description": "清空本周期已用流量。",
                "purchase_notice": "不会延长到期时间，也不会增加总流量。",
            },
        )
        listed = self.app.handle_json("GET", "/api/admin/plans", self.admin_headers, "")
        plan = json.loads(listed.body)["plans"][0]

        self.assertEqual(created.status, 200)
        self.assertEqual(updated.status, 200)
        self.assertEqual(plan["product_type"], "reset_pack")
        self.assertEqual(plan["category"], "工具包")
        self.assertEqual(plan["description"], "清空本周期已用流量。")
        self.assertEqual(plan["purchase_notice"], "不会延长到期时间，也不会增加总流量。")

    def test_admin_can_manage_tutorials_and_public_only_lists_enabled_items(self):
        created = self.post_admin(
            "/api/admin/tutorials",
            {
                "platform": "Windows",
                "title": "Clash Verge 导入订阅",
                "content": "1. 复制订阅链接\n2. 打开客户端导入。",
                "image_url": "data:image/png;base64,AA==",
                "enabled": True,
                "sort_order": 10,
            },
        )
        tutorial_id = json.loads(created.body)["tutorial"]["id"]
        disabled = self.post_admin(
            "/api/admin/tutorials",
            {
                "platform": "Android",
                "title": "待发布教程",
                "content": "草稿内容",
                "enabled": False,
                "sort_order": 20,
            },
        )
        public = self.app.handle_json("GET", "/api/tutorials", {}, "")
        admin_list = self.app.handle_json("GET", "/api/admin/tutorials", self.admin_headers, "")
        updated = self.post_admin(
            "/api/admin/tutorials",
            {
                "id": tutorial_id,
                "platform": "Windows",
                "title": "Windows 一键导入教程",
                "content": "点击一键导入按钮，按客户端提示确认。",
                "image_url": "data:image/png;base64,BB==",
                "enabled": True,
                "sort_order": 1,
            },
        )
        deleted = self.post_admin("/api/admin/tutorials/delete", {"id": tutorial_id})

        self.assertEqual(created.status, 200)
        self.assertEqual(disabled.status, 200)
        self.assertEqual(public.status, 200)
        self.assertEqual([item["title"] for item in json.loads(public.body)["tutorials"]], ["Clash Verge 导入订阅"])
        self.assertEqual(len(json.loads(admin_list.body)["tutorials"]), 2)
        self.assertEqual(json.loads(updated.body)["tutorial"]["title"], "Windows 一键导入教程")
        self.assertEqual(deleted.status, 200)
        self.assertEqual(json.loads(self.app.handle_json("GET", "/api/tutorials", {}, "").body)["tutorials"], [])

    def test_public_node_status_exposes_enabled_nodes_latency_and_rate(self):
        enabled_id = self.app.db.create_node("香港 A", "vless://example", 2.5, ["hk", "premium"], True)
        disabled_id = self.app.db.create_node("维护节点", "vless://disabled", 1, ["us"], False)
        self.app.db.update_node_status(enabled_id, "online", 48)
        self.app.db.update_node_status(disabled_id, "online", 12)

        response = self.app.handle_json("GET", "/api/nodes/status", {}, "")
        payload = json.loads(response.body)

        self.assertEqual(response.status, 200)
        self.assertEqual([node["name"] for node in payload["nodes"]], ["香港 A"])
        self.assertEqual(payload["nodes"][0]["latency_ms"], 48)
        self.assertEqual(payload["nodes"][0]["rate"], 2.5)
        self.assertEqual(payload["nodes"][0]["status"], "online")
        self.assertIn("last_checked_at", payload["nodes"][0])

    def test_admin_can_update_node_status_fields(self):
        node_id = self.app.db.create_node("日本 B", "vless://node", 0.8, ["jp"], True)

        response = self.post_admin(
            "/api/admin/nodes/status",
            {"id": node_id, "status": "degraded", "latency_ms": 188},
        )
        public = self.app.handle_json("GET", "/api/nodes/status", {}, "")

        self.assertEqual(response.status, 200)
        node = json.loads(public.body)["nodes"][0]
        self.assertEqual(node["status"], "degraded")
        self.assertEqual(node["latency_ms"], 188)

    def test_checkin_requires_active_plan_and_awards_fixed_daily_traffic_once(self):
        plan_id = self.app.db.create_plan("Daily", 10, 30, [], False, price_cents=0)
        active = self.app.db.register_user("active@example.com", "secret123")
        self.app.db.purchase_plan(active["id"], plan_id)
        inactive = self.app.db.register_user("inactive@example.com", "secret123")
        self.app.db.save_checkin_settings({"enabled": True, "mode": "fixed", "fixed_gb": 1.5})

        active_headers = self.login_user_headers(active["email"])
        inactive_headers = self.login_user_headers(inactive["email"])
        first = self.app.handle_json("POST", "/api/checkin", active_headers, "{}")
        second = self.app.handle_json("POST", "/api/checkin", active_headers, "{}")
        blocked = self.app.handle_json("POST", "/api/checkin", inactive_headers, "{}")

        self.assertEqual(first.status, 200)
        payload = json.loads(first.body)
        self.assertEqual(payload["reward_bytes"], bytes_from_gb(1.5))
        self.assertEqual(payload["user"]["quota_bytes"], bytes_from_gb(11.5))
        self.assertTrue(payload["checkin"]["checked_in_today"])
        self.assertEqual(second.status, 400)
        self.assertIn("今日已签到", second.body)
        self.assertEqual(blocked.status, 400)
        self.assertIn("仅限已开通套餐", blocked.body)

    def test_checkin_is_enabled_by_default_for_active_users(self):
        plan_id = self.app.db.create_plan("Default Daily", 10, 30, [], False, price_cents=0)
        user = self.app.db.register_user("default-checkin@example.com", "secret123")
        self.app.db.purchase_plan(user["id"], plan_id)
        headers = self.login_user_headers(user["email"])

        status_before = self.app.handle_json("GET", "/api/checkin", headers, "")
        checked = self.app.handle_json("POST", "/api/checkin", headers, "{}")

        self.assertTrue(json.loads(status_before.body)["checkin"]["enabled"])
        self.assertEqual(checked.status, 200)
        payload = json.loads(checked.body)
        self.assertEqual(payload["reward_bytes"], bytes_from_gb(1))
        self.assertTrue(payload["checkin"]["checked_in_today"])

    def test_checkin_random_range_and_status_endpoint_are_configurable_by_admin(self):
        plan_id = self.app.db.create_plan("Random", 10, 30, [], False, price_cents=0)
        user = self.app.db.register_user("random@example.com", "secret123")
        self.app.db.purchase_plan(user["id"], plan_id)
        headers = self.login_user_headers(user["email"])
        saved = self.post_admin(
            "/api/admin/checkin/settings",
            {"enabled": True, "mode": "random", "min_gb": 0.2, "max_gb": 0.3},
        )

        status_before = self.app.handle_json("GET", "/api/checkin", headers, "")
        checked = self.app.handle_json("POST", "/api/checkin", headers, "{}")
        status_after = self.app.handle_json("GET", "/api/checkin", headers, "")

        self.assertEqual(saved.status, 200)
        reward = json.loads(checked.body)["reward_bytes"]
        self.assertGreaterEqual(reward, bytes_from_gb(0.2))
        self.assertLessEqual(reward, bytes_from_gb(0.3))
        self.assertFalse(json.loads(status_before.body)["checkin"]["checked_in_today"])
        self.assertTrue(json.loads(status_after.body)["checkin"]["checked_in_today"])
        self.assertEqual(len(json.loads(status_after.body)["checkin"]["recent"]), 1)

    def test_ticket_lifecycle_for_user_and_admin(self):
        user = self.app.db.register_user("ticket@example.com", "secret123")
        headers = self.login_user_headers(user["email"])

        created = self.app.handle_json(
            "POST",
            "/api/tickets",
            headers,
            json.dumps({"subject": "节点不可用", "message": "香港 A 连接失败"}),
        )
        ticket = json.loads(created.body)["ticket"]
        admin_reply = self.post_admin(
            "/api/admin/tickets/reply",
            {"ticket_id": ticket["id"], "message": "已安排检查", "status": "closed"},
        )
        user_reply = self.app.handle_json(
            "POST",
            "/api/tickets/reply",
            headers,
            json.dumps({"ticket_id": ticket["id"], "message": "我这边还有问题"}),
        )
        user_list = self.app.handle_json("GET", "/api/tickets", headers, "")
        admin_list = self.app.handle_json("GET", "/api/admin/tickets", self.admin_headers, "")

        self.assertEqual(created.status, 200)
        self.assertEqual(admin_reply.status, 200)
        self.assertEqual(user_reply.status, 200)
        ticket_after_reply = json.loads(user_list.body)["tickets"][0]
        self.assertEqual(ticket_after_reply["status"], "open")
        self.assertEqual(ticket_after_reply["reply_count"], 2)
        self.assertEqual(ticket_after_reply["replies"][-1]["role"], "user")
        self.assertEqual(ticket_after_reply["replies"][-1]["message"], "我这边还有问题")
        self.assertEqual(json.loads(admin_list.body)["tickets"][0]["user_email"], "ticket@example.com")

    def test_user_cannot_reply_to_another_users_ticket(self):
        owner = self.app.db.register_user("ticket-owner@example.com", "secret123")
        other = self.app.db.register_user("ticket-other@example.com", "secret123")
        owner_headers = self.login_user_headers(owner["email"])
        other_headers = self.login_user_headers(other["email"])
        created = self.app.handle_json(
            "POST",
            "/api/tickets",
            owner_headers,
            json.dumps({"subject": "账号问题", "message": "请帮忙看下"}),
        )
        ticket_id = json.loads(created.body)["ticket"]["id"]

        response = self.app.handle_json(
            "POST",
            "/api/tickets/reply",
            other_headers,
            json.dumps({"ticket_id": ticket_id, "message": "冒充回复"}),
        )

        self.assertEqual(response.status, 403)

