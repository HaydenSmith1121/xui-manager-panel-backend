import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployPortDefaultsTests(unittest.TestCase):
    def test_backend_defaults_to_frontend_proxy_upstream_port(self):
        install_sh = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
        app_py = (ROOT / "xui_manager" / "app.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn('LISTEN_PORT="${LISTEN_PORT:-25889}"', install_sh)
        self.assertIn('os.environ.get("LISTEN_PORT", "25889")', app_py)
        self.assertIn("`127.0.0.1:25889`", readme)
        self.assertIn("export LISTEN_PORT=25889", readme)
        self.assertIn("BACKEND_UPSTREAM=http://127.0.0.1:25889", readme)
        self.assertIn("curl http://127.0.0.1:25889/api/health", readme)

    def test_upgrade_migrates_legacy_default_port(self):
        upgrade_sh = (ROOT / "deploy" / "upgrade.sh").read_text(encoding="utf-8")

        self.assertIn('MIGRATE_DEFAULT_LISTEN_PORT="${MIGRATE_DEFAULT_LISTEN_PORT:-1}"', upgrade_sh)
        self.assertIn("LISTEN_PORT=25888", upgrade_sh)
        self.assertIn("LISTEN_PORT=25889", upgrade_sh)


if __name__ == "__main__":
    unittest.main()
