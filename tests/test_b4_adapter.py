# SPDX-License-Identifier: GPL-2.0-or-later

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from lore_review_companion.b4_adapter import (
    B4AssemblyError, B4ReviewError, PINNED_B4_SOURCE, PINNED_B4_VERSIONS,
    assemble_local_series, extract_quoted_comments, inspect_b4_runtime, read_local_review_drafts,
)
from lore_review_companion.cli import main


class AssembleLocalSeriesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.mailbox = Path(self.tempdir.name) / "series.mbox"
        self.mailbox.write_bytes(
            b"From: Author <author@example.test>\nMessage-ID: <patch@example.test>\n"
            b"Subject: [PATCH 1/1] change\n\npatch\n"
        )

    @patch("lore_review_companion.b4_adapter.inspect_b4_runtime")
    @patch("lore_review_companion.b4_adapter.subprocess.run")
    def test_runs_b4_offline_and_returns_generated_mailbox(self, run, inspect):
        def fake_run(command, **kwargs):
            output_dir = Path(command[command.index("--outdir") + 1])
            (output_dir / "series.mbx").write_bytes(self.mailbox.read_bytes())
            return subprocess.CompletedProcess(command, 0, "summary", "")

        run.side_effect = fake_run
        result = assemble_local_series(self.mailbox)

        command = run.call_args.args[0]
        self.assertEqual(command[:3], [sys.executable, "-I", "-c"])
        inspect.assert_called_once_with(sys.executable, timeout=120)
        self.assertIn("--offline-mode", command)
        self.assertIn("--no-stdin", command)
        self.assertIn("--no-add-trailers", command)
        self.assertIn("--no-partial-reroll", command)
        self.assertEqual(command[command.index("--use-version") + 1], "1")
        self.assertIn("--use-local-mbox", command)
        self.assertIn(str(self.mailbox), command)
        self.assertIn("--no-cover", command)
        self.assertEqual(result.patch_mailbox, self.mailbox.read_bytes())
        self.assertEqual(result.stdout, "summary")
        self.assertEqual(run.call_args.kwargs["cwd"], command[command.index("--outdir") + 1])
        self.assertNotIn("LORE_FORGE_TOKEN", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    @patch("lore_review_companion.b4_adapter.inspect_b4_runtime")
    @patch("lore_review_companion.b4_adapter.subprocess.run")
    def test_assembly_normalizes_mail_line_endings_and_preserves_in_body_patch_author(self, run, _inspect):
        self.mailbox.write_bytes(
            b"From: Sender <sender@example.test>\r\nMessage-ID: <patch@example.test>\r\n"
            b"Subject: [PATCH 1/1] change\r\n\r\nFrom: Author <author@example.test>\r\n\r\n"
            b"Original patch.\r\n\r\ndiff --git a/x b/x\r\n--- a/x\r\n+++ b/x\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n"
        )
        output = self.mailbox.read_bytes().replace(b"\r\n", b"\n").replace(
            b"From: Sender <sender@example.test>", b"From: Author <author@example.test>", 1,
        ).replace(b"\n\nFrom: Author <author@example.test>\n", b"\n", 1)

        def fake_run(command, **kwargs):
            output_dir = Path(command[command.index("--outdir") + 1])
            (output_dir / "series.mbx").write_bytes(output)
            return subprocess.CompletedProcess(command, 0, "summary", "")

        run.side_effect = fake_run
        self.assertEqual(assemble_local_series(self.mailbox).patch_mailbox, output)
        output = output.replace(b"+new\n", b"+different\n")
        with self.assertRaisesRegex(B4AssemblyError, "membership"):
            assemble_local_series(self.mailbox)

    @patch("lore_review_companion.b4_adapter.inspect_b4_runtime")
    @patch("lore_review_companion.b4_adapter.subprocess.run")
    def test_reports_b4_failure(self, run, _inspect):
        run.return_value = subprocess.CompletedProcess(
            [], 1, "", "could not parse thread"
        )
        with self.assertRaisesRegex(B4AssemblyError, "could not parse thread"):
            assemble_local_series(self.mailbox)

    @patch("lore_review_companion.b4_adapter.inspect_b4_runtime")
    @patch("lore_review_companion.b4_adapter.subprocess.run")
    def test_rejects_missing_assembly_output(self, run, _inspect):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        with self.assertRaisesRegex(B4AssemblyError, "found 0"):
            assemble_local_series(self.mailbox)

    @patch("lore_review_companion.b4_adapter.subprocess.run")
    def test_runtime_pin_rejects_source_drift_and_keeps_module_evidence(self, run):
        runtime = {"versions": PINNED_B4_VERSIONS, "source_sha256": PINNED_B4_SOURCE, "module_paths": {}}
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(runtime), "")
        self.assertEqual(inspect_b4_runtime(sys.executable), runtime)
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps({**runtime, "source_sha256": "0" * 64}), "")
        with self.assertRaisesRegex(B4AssemblyError, "fingerprint"):
            inspect_b4_runtime(sys.executable)

    def test_cli_reports_runtime_preflight_as_json(self):
        runtime = {"versions": PINNED_B4_VERSIONS, "source_sha256": PINNED_B4_SOURCE}
        with (
            patch("lore_review_companion.cli.inspect_b4_runtime", return_value=runtime) as inspect,
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(main(["check-b4-runtime", "--python", sys.executable]), 0)
        self.assertEqual(json.loads(output.getvalue()), runtime)
        inspect.assert_called_once_with(sys.executable)

    @patch("lore_review_companion.b4_adapter.inspect_b4_runtime")
    def test_incomplete_or_wrong_revision_never_invokes_runtime(self, inspect):
        with self.assertRaisesRegex(B4AssemblyError, "Requested revision"):
            assemble_local_series(self.mailbox, revision=2)
        self.mailbox.write_bytes(self.mailbox.read_bytes().replace(b"1/1", b"1/2"))
        with self.assertRaisesRegex(B4AssemblyError, "complete"):
            assemble_local_series(self.mailbox)
        inspect.assert_not_called()

    @patch("lore_review_companion.b4_adapter.inspect_b4_runtime")
    @patch("lore_review_companion.b4_adapter.subprocess.run")
    def test_extracts_validated_comments_in_isolated_interpreter(self, run, inspect):
        run.return_value = subprocess.CompletedProcess(
            [], 0, '[{"path":"x.c","line":3,"text":"Review","content":"+new"}]', "",
        )
        comments = extract_quoted_comments("quoted reply", python_executable="/venv/bin/python")
        self.assertEqual((comments[0].path, comments[0].line, comments[0].text), ("x.c", 3, "Review"))
        self.assertEqual(run.call_args.args[0][:2], ["/venv/bin/python", "-c"])
        self.assertEqual(run.call_args.kwargs["input"], '"quoted reply"')
        self.assertNotIn("LORE_FORGE_TOKEN", run.call_args.kwargs["env"])
        inspect.assert_called_once_with("/venv/bin/python", timeout=30)

    @patch("lore_review_companion.b4_adapter.inspect_b4_runtime")
    @patch("lore_review_companion.b4_adapter.subprocess.run")
    def test_rejects_incompatible_parser_and_invalid_output(self, run, _inspect):
        run.return_value = subprocess.CompletedProcess([], 1, "", "Unsupported b4 review version")
        with self.assertRaisesRegex(B4ReviewError, "Unsupported"):
            extract_quoted_comments("reply", python_executable="python")
        run.return_value = subprocess.CompletedProcess([], 0, '[{"path":"x.c","line":true}]', "")
        with self.assertRaisesRegex(B4ReviewError, "invalid comment"):
            extract_quoted_comments("reply", python_executable="python")

    @patch("lore_review_companion.b4_adapter.inspect_b4_runtime")
    @patch("lore_review_companion.b4_adapter.subprocess.run")
    def test_reads_only_pinned_local_review_drafts(self, run, _inspect):
        commit = "a" * 40

        def fake_run(command, **kwargs):
            if "check-ref-format" in command:
                return subprocess.CompletedProcess(command, 0, "b4/review/example\n", "")
            if "rev-parse" in command:
                return subprocess.CompletedProcess(command, 0, commit + "\n", "")
            if "cat-file" in command:
                return subprocess.CompletedProcess(command, 0, "1234\n", "")
            return subprocess.CompletedProcess(command, 0, (
                '{"revision":2,"change_id":"change","drafts":[{"patch_index":1,'
                '"message_id":"patch@example.test","reply":"General",'
                '"trailers":["Reviewed-by: Reviewer"],"comments":'
                '[{"path":"x.c","line":3,"text":"Review","content":"+new"}]}]}'
            ), "")

        run.side_effect = fake_run
        snapshot = read_local_review_drafts(
            self.mailbox.parent, "b4/review/example", "reviewer@example.test", python_executable="/venv/python",
        )
        self.assertEqual((snapshot.tracking_commit, snapshot.revision), (commit, 2))
        self.assertEqual(snapshot.drafts[0].comments[0].text, "Review")
        reader = run.call_args.args[0]
        self.assertEqual(reader[-2:], [commit, "reviewer@example.test"])
        self.assertNotIn("LORE_FORGE_TOKEN", run.call_args.kwargs["env"])

    @patch("lore_review_companion.b4_adapter.inspect_b4_runtime")
    @patch("lore_review_companion.b4_adapter.subprocess.run")
    def test_rejects_invalid_or_oversized_review_drafts(self, run, _inspect):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "branch", ""),
            subprocess.CompletedProcess([], 0, "a" * 40, ""),
            subprocess.CompletedProcess([], 0, str(8 * 1024 * 1024 + 1), ""),
        ]
        with self.assertRaisesRegex(B4ReviewError, "8 MiB"):
            read_local_review_drafts(
                self.mailbox.parent, "b4/review/example", "reviewer@example.test", python_executable="python",
            )
        with self.assertRaisesRegex(ValueError, "b4/review"):
            read_local_review_drafts(self.mailbox.parent, "main", "reviewer@example.test", python_executable="python")

    @unittest.skipUnless(os.environ.get("LORE_FORGE_ACCEPTANCE_B4_PYTHON"), "requires an installed b4 interpreter")
    def test_real_b4_assembly_does_not_read_open_caller_stdin(self):
        repository = self.mailbox.parent / "repository"
        environment = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": self.tempdir.name,
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONPATH": str(Path(__file__).parent.parent),
        }

        def git(*args):
            return subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                 "-c", "commit.gpgsign=false", *args],
                check=True, capture_output=True, env=environment,
            ).stdout

        git("init", "-q", "--initial-branch=main", str(repository))
        git("-C", str(repository), "commit", "--allow-empty", "-qm", "base")
        (repository / "fixture.txt").write_text("synthetic fixture\n")
        git("-C", str(repository), "add", "fixture.txt")
        git("-C", str(repository), "commit", "-qm", "synthetic patch")
        self.mailbox.write_bytes(git("-C", str(repository), "format-patch", "--stdout", "--numbered", "--thread", "HEAD~1..HEAD"))
        executable = str(Path(os.environ["LORE_FORGE_ACCEPTANCE_B4_PYTHON"]).with_name("b4"))
        script = (
            "import sys; from pathlib import Path; from lore_review_companion.b4_adapter import assemble_local_series; "
            "assert assemble_local_series(Path(sys.argv[1]), executable=sys.argv[2], timeout=10).patch_mailbox"
        )
        with subprocess.Popen(
            [sys.executable, "-c", script, str(self.mailbox), executable],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=Path(__file__).parent, env=environment, text=True,
        ) as process:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                self.fail("b4 assembly waited for open caller stdin")
            _, error = process.communicate()
            self.assertEqual(process.returncode, 0, error)

    @unittest.skipUnless(os.environ.get("LORE_FORGE_ACCEPTANCE_B4_PYTHON"), "requires an installed b4 interpreter")
    def test_reads_real_b4_tracking_commit_without_mutation(self):
        repository = self.mailbox.parent / "repository"
        environment = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": self.tempdir.name,
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        }

        def git(*args):
            return subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", *args],
                check=True, capture_output=True, text=True, env=environment,
            ).stdout.strip()

        git("init", "-q", "--initial-branch=main", str(repository))
        git("-C", str(repository), "commit", "--allow-empty", "-qm", "base")
        tracking = {
            "series": {"revision": 2, "change-id": "example", "header-info": {"msgid": "cover@example.test"}},
            "patches": [{
                "header-info": {"msgid": "patch@example.test"},
                "reviews": {
                    "reviewer@example.test": {"reply": "General draft", "comments": [
                        {"path": "test.c", "line": 4, "text": "Check this", "content": "+new"},
                    ]},
                    "other@example.test": {"reply": "Do not include"},
                },
            }],
        }
        git("-C", str(repository), "commit", "--allow-empty", "-qm", f"Cover\n\n--- b4-review-tracking ---\n{json.dumps(tracking)}")
        git("-C", str(repository), "branch", "b4/review/example")
        head = git("-C", str(repository), "rev-parse", "HEAD")
        before = git("-C", str(repository), "status", "--porcelain")
        snapshot = read_local_review_drafts(
            repository, "b4/review/example", "reviewer@example.test",
            python_executable=os.environ["LORE_FORGE_ACCEPTANCE_B4_PYTHON"],
        )
        self.assertEqual((snapshot.tracking_commit, snapshot.revision), (head, 2))
        self.assertEqual(len(snapshot.drafts), 1)
        self.assertEqual((snapshot.drafts[0].message_id, snapshot.drafts[0].reply), ("patch@example.test", "General draft"))
        self.assertEqual(snapshot.drafts[0].comments[0].content, "+new")
        self.assertEqual(git("-C", str(repository), "rev-parse", "HEAD"), head)
        self.assertEqual(git("-C", str(repository), "rev-parse", "b4/review/example"), head)
        self.assertEqual(git("-C", str(repository), "status", "--porcelain"), before)


if __name__ == "__main__":
    unittest.main()