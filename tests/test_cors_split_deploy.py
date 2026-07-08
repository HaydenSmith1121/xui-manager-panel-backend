import os
import unittest
from unittest.mock import patch

from xui_manager.app import XuiManagerApp, cookie_header, expired_cookie_header
from xui_manager.subscription import Response


class SplitDeployCorsTests(unittest.TestCase):
    def make_app(self):
        return XuiManagerApp(":memory:")

    def test_allowed_frontend_origin_gets_cors_headers(self):
        app = self.make_app()
        with patch.dict(os.environ, {"FRONTEND_ORIGIN": "https://front.example.com"}, clear=False):
            headers = app.cors_headers({"Origin": "https://front.example.com"})
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://front.example.com")
        self.assertEqual(headers["Access-Control-Allow-Credentials"], "true")
        self.assertIn("POST", headers["Access-Control-Allow-Methods"])

    def test_allowed_frontend_origin_can_post_json(self):
        app = self.make_app()
        request_headers = {
            "Content-Type": "application/json",
            "Origin": "https://front.example.com",
            "Host": "api.example.com",
        }
        with patch.dict(os.environ, {"FRONTEND_ORIGIN": "https://front.example.com"}, clear=False):
            self.assertIsNone(app.require_mutation_headers(request_headers))

    def test_unconfigured_cross_origin_post_is_rejected(self):
        app = self.make_app()
        request_headers = {
            "Content-Type": "application/json",
            "Origin": "https://front.example.com",
            "Host": "api.example.com",
        }
        with patch.dict(os.environ, {"FRONTEND_ORIGIN": ""}, clear=False):
            response = app.require_mutation_headers(request_headers)
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, 403)

    def test_same_public_origin_with_proxy_host_missing_port_can_post_json(self):
        app = self.make_app()
        request_headers = {
            "Content-Type": "application/json",
            "Origin": "http://66.212.16.58:25888",
            "Host": "66.212.16.58",
            "X-Forwarded-Proto": "http",
        }
        with patch.dict(os.environ, {"FRONTEND_ORIGIN": "", "CORS_ALLOWED_ORIGINS": ""}, clear=False):
            self.assertIsNone(app.require_mutation_headers(request_headers))

    def test_cookie_attributes_can_support_cross_site_deploy(self):
        with patch.dict(
            os.environ,
            {"SESSION_COOKIE_SAMESITE": "None", "SESSION_COOKIE_SECURE": "true"},
            clear=False,
        ):
            self.assertIn("SameSite=None", cookie_header("abc"))
            self.assertIn("Secure", cookie_header("abc"))
            self.assertIn("SameSite=None", expired_cookie_header())


if __name__ == "__main__":
    unittest.main()
