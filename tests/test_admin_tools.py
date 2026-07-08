from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tools.reset_admin import reset_admin
from xui_manager.db import Database


class AdminToolTests(unittest.TestCase):
    def test_reset_admin_creates_and_updates_admin_login(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "app.db"
            db = Database(db_path)
            db.init_schema()

            reset_admin(db_path, "admin@admin.com", "first-pass")
            self.assertIsNotNone(db.authenticate("admin@admin.com", "first-pass"))

            reset_admin(db_path, "admin@admin.com", "second-pass")
            self.assertIsNone(db.authenticate("admin@admin.com", "first-pass"))
            user = db.authenticate("admin@admin.com", "second-pass")
            self.assertEqual(user["role"], "admin")
            self.assertEqual(user["status"], "active")

    def test_reset_admin_command_runs_from_project_root(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "app.db"

            result = subprocess.run(
                [
                    sys.executable,
                    "tools/reset_admin.py",
                    "--db",
                    str(db_path),
                    "--email",
                    "admin@admin.com",
                    "--password",
                    "new-pass",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("admin reset: admin@admin.com", result.stdout)
            db = Database(db_path)
            self.assertIsNotNone(db.authenticate("admin@admin.com", "new-pass"))


if __name__ == "__main__":
    unittest.main()
