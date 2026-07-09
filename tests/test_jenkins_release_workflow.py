import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class BackendJenkinsReleaseWorkflowTest(unittest.TestCase):
    def read(self, relative_path):
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_release_script_uses_versioned_current_symlink_and_service_rollback(self):
        script = self.read("deploy/jenkins-deploy.sh")
        self.assertIn("RELEASES_DIR=", script)
        self.assertIn("CURRENT_LINK=", script)
        self.assertIn("PREVIOUS_LINK=", script)
        self.assertIn('ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"', script)
        self.assertIn("rollback_to_previous", script)
        self.assertIn('WorkingDirectory=${CURRENT_LINK}', script)
        self.assertIn("ExecStart=/usr/bin/python3 -m xui_manager.app", script)

    def test_rollback_script_can_target_previous_or_named_release(self):
        script = self.read("deploy/rollback.sh")
        self.assertIn("TARGET_RELEASE=", script)
        self.assertIn("PREVIOUS_LINK=", script)
        self.assertIn("find_latest_previous_release", script)
        self.assertIn('ln -sfn "$TARGET_DIR" "$CURRENT_LINK"', script)
        self.assertIn('systemctl restart "$APP_NAME"', script)

    def test_jenkinsfile_exposes_deploy_and_rollback_actions(self):
        jenkinsfile = self.read("Jenkinsfile")
        self.assertIn("choice(name: 'ACTION'", jenkinsfile)
        self.assertIn("deploy", jenkinsfile)
        self.assertIn("rollback", jenkinsfile)
        self.assertIn("deploy/jenkins-deploy.sh", jenkinsfile)
        self.assertIn("deploy/rollback.sh", jenkinsfile)


if __name__ == "__main__":
    unittest.main()

