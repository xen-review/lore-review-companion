import configparser
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


class DevelopmentDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.script = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "contrib/deploy-development.py")
        )

    def test_config_preserves_values_and_sets_matching_static_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.ini"
            source.write_text(
                "APP_NAME = Development\n[server]\nSTATIC_ROOT_PATH = /old\nROOT_URL = https://forge.test/\n[security]\nSECRET_KEY = synthetic-secret%value\n"
            )
            target = root / "release/app.ini"
            self.script["write_config"](source, target, root / "release")
            parsed = configparser.ConfigParser(interpolation=None)
            parsed.read(target)
            self.assertEqual(
                parsed["server"]["STATIC_ROOT_PATH"], str(root / "release")
            )
            self.assertEqual(parsed["security"]["SECRET_KEY"], "synthetic-secret%value")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_deploy_requires_clean_pushed_source(self):
        with patch("subprocess.check_output", return_value=" M source.py\n"):
            with self.assertRaisesRegex(ValueError, "Commit and push"):
                self.script["revision"](Path("/source"))
        with patch("subprocess.check_output", side_effect=["", "new\n", "old\n"]):
            with self.assertRaisesRegex(ValueError, "Push the source"):
                self.script["revision"](Path("/source"))
        with patch("subprocess.check_output", side_effect=["", "same\n", "same\n"]):
            self.assertEqual(self.script["revision"](Path("/source")), "same")

    def test_deploy_preview_never_builds_or_changes_services(self):
        with (
            patch(
                "subprocess.check_output",
                side_effect=["", "same\n", "same\n", "", "same\n", "same\n"],
            ),
            patch("subprocess.run") as run,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                self.script["main"](["--forgejo-config", "/missing.ini"]), 0
            )
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["companion_revision"], "same")
