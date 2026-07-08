import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from xui_manager.db import Database
from xui_manager.vless import (
    eligible_managed_nodes,
    group_managed_targets,
    parse_vless_template,
    replace_vless_uuid,
    validate_target_nodes,
)


def node(**overrides):
    data = {
        "id": 1,
        "name": "US",
        "mode": "managed",
        "panel_id": 1,
        "inbound_id": 2,
        "source_url": "vless://old@example.com:443?security=reality&flow=xtls-rprx-vision&sni=edge.example#US",
        "rate": 1,
        "tags": ["premium"],
        "enabled": True,
    }
    data.update(overrides)
    return data


class VlessTemplateTests(unittest.TestCase):
    def test_replace_uuid_preserves_every_other_uri_component(self):
        template = "vless://old@example.com:443?security=reality&flow=xtls-rprx-vision&sni=edge.example#US"

        rewritten = replace_vless_uuid(template, "22222222-2222-2222-2222-222222222222")

        self.assertIn("vless://22222222-2222-2222-2222-222222222222@example.com:443", rewritten)
        self.assertIn("security=reality", rewritten)
        self.assertIn("flow=xtls-rprx-vision", rewritten)
        self.assertTrue(rewritten.endswith("#US"))

    def test_tls_link_parses_without_flow(self):
        parsed = parse_vless_template(
            "vless://old@example.com:443?security=tls&type=tcp&sni=example.com#TLS"
        )

        self.assertEqual(parsed.flow, "")
        self.assertEqual(parsed.host, "example.com")
        self.assertEqual(parsed.port, 443)

    def test_reality_vision_link_parses_flow(self):
        parsed = parse_vless_template(
            "vless://old@example.com:443?security=reality&flow=xtls-rprx-vision&sni=edge.example#US"
        )

        self.assertEqual(parsed.flow, "xtls-rprx-vision")
        self.assertEqual(parsed.host, "example.com")
        self.assertEqual(parsed.port, 443)

    def test_websocket_query_order_and_encoding_are_preserved(self):
        template = (
            "vless://old@ws.example.com:8443?"
            "type=ws&path=%2Fedge%3Fed%3D2048&host=cdn.example.com&security=tls#WS"
        )

        rewritten = replace_vless_uuid(template, "22222222-2222-2222-2222-222222222222")

        self.assertEqual(
            rewritten,
            "vless://22222222-2222-2222-2222-222222222222@ws.example.com:8443?"
            "type=ws&path=%2Fedge%3Fed%3D2048&host=cdn.example.com&security=tls#WS",
        )
        self.assertEqual(parse_qsl(urlsplit(rewritten).query), parse_qsl(urlsplit(template).query))

    def test_ipv6_host_syntax_is_supported(self):
        template = "vless://old@[2001:db8::1]:443?security=tls#IPv6"

        parsed = parse_vless_template(template)
        rewritten = replace_vless_uuid(template, "22222222-2222-2222-2222-222222222222")

        self.assertEqual(parsed.host, "2001:db8::1")
        self.assertEqual(parsed.port, 443)
        self.assertIn("@[2001:db8::1]:443", rewritten)

    def test_malformed_links_are_rejected(self):
        cases = [
            "trojan://old@example.com:443",
            "vless://example.com:443",
            "vless://old@:443",
            "vless://old@example.com",
            "vless://old@example.com:notaport",
            "not a url",
        ]

        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    parse_vless_template(case)

    def test_replace_uuid_rejects_invalid_uuid(self):
        with self.assertRaisesRegex(ValueError, "invalid uuid"):
            replace_vless_uuid("vless://old@example.com:443", "not-a-uuid")


class ManagedNodeHelperTests(unittest.TestCase):
    def test_target_accepts_identical_positive_rate_and_flow(self):
        rate, flow = validate_target_nodes([node(rate=2), node(id=2, rate=2)])

        self.assertEqual(rate, 2)
        self.assertEqual(flow, "xtls-rprx-vision")

    def test_target_rejects_conflicting_rates(self):
        with self.assertRaisesRegex(ValueError, "same multiplier"):
            validate_target_nodes([node(rate=1), node(id=2, rate=3)])

    def test_target_rejects_non_finite_rates(self):
        for rate in (float("nan"), float("inf"), "nan", "inf"):
            with self.subTest(rate=rate):
                with self.assertRaisesRegex(ValueError, "positive"):
                    validate_target_nodes([node(rate=rate)])

    def test_target_rejects_non_finite_inbound_ids(self):
        for inbound_id in (float("inf"), "inf"):
            with self.subTest(inbound_id=inbound_id):
                with self.assertRaisesRegex(ValueError, "inbound_id"):
                    validate_target_nodes([node(inbound_id=inbound_id)])

    def test_target_rejects_conflicting_flows(self):
        with self.assertRaisesRegex(ValueError, "same flow"):
            validate_target_nodes(
                [
                    node(source_url="vless://old@example.com:443?flow=xtls-rprx-vision"),
                    node(id=2, source_url="vless://old@example.com:443?security=tls"),
                ]
            )

    def test_eligible_managed_nodes_filters_enabled_mode_and_tags(self):
        nodes = [
            node(id=1, tags=["premium"]),
            node(id=2, tags=["standard"]),
            node(id=3, enabled=False, tags=["premium"]),
            node(id=4, mode="static", tags=["premium"]),
        ]

        eligible = eligible_managed_nodes(nodes, {"premium"})

        self.assertEqual([item["id"] for item in eligible], [1])
        self.assertIsNot(eligible[0], nodes[0])

    def test_eligible_managed_nodes_allows_all_tags_when_requested(self):
        nodes = [node(id=1, tags=["premium"]), node(id=2, tags=["standard"])]

        self.assertEqual([item["id"] for item in eligible_managed_nodes(nodes, None)], [1, 2])
        self.assertEqual([item["id"] for item in eligible_managed_nodes(nodes, {"all"})], [1, 2])

    def test_group_managed_targets_groups_by_panel_and_inbound(self):
        grouped = group_managed_targets(
            [
                node(id=1, panel_id=7, inbound_id=9),
                node(id=2, panel_id=7, inbound_id=9),
                node(id=3, panel_id=7, inbound_id=10),
            ]
        )

        self.assertEqual(sorted(grouped), [(7, 9), (7, 10)])
        self.assertEqual([item["id"] for item in grouped[(7, 9)]], [1, 2])


class ManagedNodeDatabaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "app.db")
        self.db.init_schema()
        self.panel_id = self.db.create_panel("Panel", "https://panel.example.com", "admin", "secret")

    def create_managed_node(self, **overrides):
        args = {
            "name": "Managed",
            "source_url": "vless://old@example.com:443?security=reality&flow=xtls-rprx-vision#US",
            "rate": 1,
            "tags": ["premium"],
            "enabled": True,
            "panel_id": self.panel_id,
            "inbound_id": 3,
            "mode": "managed",
        }
        args.update(overrides)
        return self.db.create_node(**args)

    def test_static_node_remains_backward_compatible_without_managed_fields(self):
        node_id = self.db.create_node("Static", "vmess://example", 1, ["standard"])

        saved = self.db.list_nodes()[0]
        self.assertEqual(saved["id"], node_id)
        self.assertEqual(saved["mode"], "static")
        self.assertIsNone(saved["panel_id"])
        self.assertEqual(saved["inbound_id"], 0)

    def test_create_managed_node_requires_panel_inbound_vless_template_and_positive_rate(self):
        cases = [
            ({"panel_id": None}, "panel_id"),
            ({"inbound_id": 0}, "inbound_id"),
            ({"inbound_id": -1}, "inbound_id"),
            ({"inbound_id": "not-int"}, "inbound_id"),
            ({"source_url": "trojan://old@example.com:443"}, "VLESS"),
            ({"source_url": "vless://old@example.com"}, "port"),
            ({"rate": 0}, "positive"),
        ]

        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, message):
                    self.create_managed_node(**overrides)

    def test_create_managed_node_rejects_non_finite_rate_with_friendly_error(self):
        for rate in ("inf", "nan"):
            with self.subTest(rate=rate):
                with self.assertRaisesRegex(ValueError, "positive"):
                    self.create_managed_node(rate=rate)

    def test_create_managed_node_rejects_non_finite_inbound_id_with_friendly_error(self):
        for inbound_id in ("inf", float("inf")):
            with self.subTest(inbound_id=inbound_id):
                with self.assertRaisesRegex(ValueError, "inbound_id"):
                    self.create_managed_node(inbound_id=inbound_id)

    def test_create_enabled_managed_node_rejects_enabled_sibling_rate_conflict(self):
        self.create_managed_node(rate=1)

        with self.assertRaisesRegex(ValueError, "same multiplier"):
            self.create_managed_node(rate=2)

    def test_create_enabled_managed_node_ignores_disabled_sibling_conflict(self):
        self.create_managed_node(rate=1, enabled=False)

        node_id = self.create_managed_node(rate=2, enabled=True)

        self.assertEqual(self.db.list_nodes()[-1]["id"], node_id)

    def test_update_enabled_managed_node_rejects_enabled_sibling_flow_conflict(self):
        first = self.create_managed_node(
            source_url="vless://old@example.com:443?flow=xtls-rprx-vision"
        )
        second = self.create_managed_node(
            name="Second",
            source_url="vless://old@example.com:443?flow=xtls-rprx-vision",
        )

        with self.assertRaisesRegex(ValueError, "same flow"):
            self.db.update_node(
                second,
                "Second",
                "vless://old@example.com:443?security=tls",
                1,
                ["premium"],
                True,
                self.panel_id,
                3,
                mode="managed",
            )

        self.assertEqual(len(self.db.list_nodes()), 2)
        self.assertEqual(self.db.list_nodes()[0]["id"], first)


if __name__ == "__main__":
    unittest.main()
