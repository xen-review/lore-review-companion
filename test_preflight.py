import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cli import main
from git_branches import GitHttpCredentials, RevisionBranchConflict, RevisionBranchError, RevisionBranchUncertain, ensure_revision_branch, inspect_upstream_history, plan_staging_sync, revision_branch_name, sync_staging
from ledger import Ledger
from preflight import GitPreflightError, preflight_patch_mailbox, prepare_patch_mailbox
from series import discover_series
from test_series import message


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.repository = Path(self.tempdir.name) / "source"
        subprocess.run(["git", "init", "-q", str(self.repository)], check=True)
        self.git("config", "user.name", "Test Author")
        self.git("config", "user.email", "author@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.file = self.repository / "content.txt"
        self.file.write_text("base\n")
        self.git("add", "content.txt")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        self.file.write_text("first\n")
        self.git("commit", "-qam", "first")
        self.file.write_text("second\n")
        self.git("commit", "-qam", "second")
        self.mailbox = self.git("format-patch", "--stdout", f"{self.base}..HEAD")

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=True,
            capture_output=True,
        ).stdout

    def create_remote(self):
        remote = Path(self.tempdir.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        self.git("push", str(remote), f"{self.base}:refs/heads/main")
        return remote

    def run_with_cleanup_check(self, base, mailbox):
        directories = []
        temporary_directory = tempfile.TemporaryDirectory

        def make_directory(**kwargs):
            directory = temporary_directory(dir=self.tempdir.name, **kwargs)
            directories.append(Path(directory.name))
            return directory

        try:
            with patch("preflight.tempfile.TemporaryDirectory", side_effect=make_directory):
                return preflight_patch_mailbox(self.repository, base, mailbox)
        finally:
            self.assertTrue(directories)
            self.assertTrue(all(not directory.exists() for directory in directories))

    def test_applies_dependent_patches_without_changing_source(self):
        head = self.git("rev-parse", "HEAD")
        refs = self.git("show-ref")
        status = self.git("status", "--porcelain=v1")

        result = self.run_with_cleanup_check(self.base, self.mailbox)
        replay = self.run_with_cleanup_check(self.base, self.mailbox)

        self.assertEqual(result.base_commit, self.base)
        self.assertEqual(result.patch_count, 2)
        self.assertNotEqual(result.head_commit, self.base)
        self.assertEqual(result.head_commit, replay.head_commit)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("show-ref"), refs)
        self.assertEqual(self.git("status", "--porcelain=v1"), status)
        self.assertFalse((self.repository / ".git" / "rebase-apply").exists())

    def test_reports_conflicting_base_and_cleans_up(self):
        self.git("checkout", "--detach", self.base)
        self.file.write_text("conflict\n")
        self.git("commit", "-qam", "conflict")
        head = self.git("rev-parse", "HEAD")

        with self.assertRaisesRegex(GitPreflightError, "Apply mailbox failed"):
            self.run_with_cleanup_check("HEAD", self.mailbox)

        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.file.read_text(), "conflict\n")
        self.assertFalse((self.repository / ".git" / "rebase-apply").exists())

    def test_prepared_repository_exists_only_inside_context(self):
        with prepare_patch_mailbox(self.repository, self.base, self.mailbox) as prepared:
            repository = prepared.repository
            self.assertTrue(repository.exists())
            self.assertEqual(prepared.run_git("Inspect", "rev-parse", "HEAD"), prepared.result.head_commit)
        self.assertFalse(repository.exists())

    def test_revision_branch_dry_run_create_and_replay(self):
        remote = self.create_remote()
        arguments = {
            "target_branch": "main", "series_key": "lore:thread@example.test",
            "revision": 1, "expected_patch_count": 2,
        }
        with prepare_patch_mailbox(self.repository, self.base, self.mailbox) as prepared:
            preview = ensure_revision_branch(prepared, remote, **arguments)
            self.assertTrue(preview.planned)
            self.assertEqual(self.git("ls-remote", str(remote), f"refs/heads/{preview.branch}"), b"")

            created = ensure_revision_branch(prepared, remote, write_enabled=True, **arguments)
            replay = ensure_revision_branch(prepared, remote, write_enabled=True, **arguments)

        self.assertTrue(created.created)
        self.assertFalse(replay.created)
        self.assertFalse(replay.planned)
        self.assertEqual(created.head_commit, replay.head_commit)
        self.assertIn(created.head_commit.encode(), self.git("ls-remote", str(remote), f"refs/heads/{created.branch}"))

    def test_staging_sync_plan_requires_a_known_fast_forward_and_does_not_write(self):
        remote = self.create_remote()
        self.git("update-ref", "refs/heads/staging", "HEAD")
        self.git("push", str(remote), f"{self.base}:refs/heads/staging")
        before = self.git("ls-remote", str(remote), "refs/heads/staging")
        plan = plan_staging_sync(self.repository, remote)
        self.assertEqual(plan.target_commit, self.base)
        self.assertEqual(plan.source_commit, self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(len(plan.approval_digest()), 64)
        self.assertEqual(self.git("ls-remote", str(remote), "refs/heads/staging"), before)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main([
                "plan-staging-sync", "--source-repository", str(self.repository), "--remote", str(remote),
            ]), 0)
        preview = json.loads(output.getvalue())
        self.assertEqual(preview["plan"]["target_commit"], self.base)
        self.assertEqual(preview["approval_digest"], plan.approval_digest())
        self.assertEqual(self.git("ls-remote", str(remote), "refs/heads/staging"), before)
        self.git("checkout", "-qb", "divergent", self.base)
        self.file.write_text("different\n")
        self.git("commit", "-qam", "different")
        self.git("push", str(remote), "HEAD:refs/heads/staging")
        with self.assertRaisesRegex(RevisionBranchConflict, "not an ancestor"):
            plan_staging_sync(self.repository, remote)
        with self.assertRaisesRegex(ValueError, "distinct"):
            plan_staging_sync(self.repository, self.repository)

    def test_staging_sync_plan_rejects_missing_refs_objects_and_grafts(self):
        remote = self.create_remote()
        self.git("update-ref", "refs/heads/staging", "HEAD")
        with self.assertRaisesRegex(RevisionBranchConflict, "missing"):
            plan_staging_sync(self.repository, remote)
        self.git("push", str(remote), f"{self.base}:refs/heads/staging")
        other = Path(self.tempdir.name) / "other"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        subprocess.run([
            "git", "-C", str(other), "-c", "user.name=Test", "-c", "user.email=test@example.test",
            "commit", "--allow-empty", "-qm", "unrelated",
        ], check=True)
        subprocess.run(["git", "-C", str(other), "push", str(remote), "HEAD:refs/heads/other"], check=True, capture_output=True)
        unknown = subprocess.run(
            ["git", "-C", str(other), "rev-parse", "HEAD"], check=True, capture_output=True,
        ).stdout.decode().strip()
        subprocess.run(["git", "-C", str(remote), "update-ref", "refs/heads/staging", unknown], check=True)
        with self.assertRaisesRegex(RevisionBranchConflict, "unavailable"):
            plan_staging_sync(self.repository, remote)
        (self.repository / ".git" / "info" / "grafts").write_text("")
        with self.assertRaisesRegex(RevisionBranchError, "grafts"):
            plan_staging_sync(self.repository, remote)

    def test_staging_sync_updates_only_fast_forwards_and_replays(self):
        remote = self.create_remote()
        self.git("update-ref", "refs/heads/staging", "HEAD")
        self.git("push", str(remote), f"{self.base}:refs/heads/staging")
        before = self.git("show-ref")
        plan = plan_staging_sync(self.repository, remote)
        result = sync_staging(plan)
        self.assertTrue(result.updated)
        self.assertFalse(result.reconciled)
        self.assertEqual(self.git("show-ref"), before)
        self.assertTrue(self.git("ls-remote", str(remote), "refs/heads/staging").startswith(plan.source_commit.encode()))
        self.assertFalse(sync_staging(plan_staging_sync(self.repository, remote)).updated)
        with self.assertRaisesRegex(RevisionBranchConflict, "changed"):
            sync_staging(plan)

    def test_staging_sync_reconciles_lost_push_acknowledgment(self):
        remote = self.create_remote()
        self.git("update-ref", "refs/heads/staging", "HEAD")
        self.git("push", str(remote), f"{self.base}:refs/heads/staging")
        plan = plan_staging_sync(self.repository, remote)
        original_run = subprocess.run

        def lose_acknowledgment(command, **options):
            result = original_run(command, **options)
            if "push" in command:
                raise subprocess.TimeoutExpired(command, options["timeout"])
            return result

        with patch("git_branches.subprocess.run", side_effect=lose_acknowledgment):
            result = sync_staging(plan)
        self.assertTrue(result.updated)
        self.assertTrue(result.reconciled)
        self.assertTrue(self.git("ls-remote", str(remote), "refs/heads/staging").startswith(plan.source_commit.encode()))

    def test_staging_sync_never_overwrites_a_racing_divergent_update(self):
        remote = self.create_remote()
        self.git("update-ref", "refs/heads/staging", "HEAD")
        self.git("push", str(remote), f"{self.base}:refs/heads/staging")
        self.git("checkout", "-qb", "racing", self.base)
        self.file.write_text("racing\n")
        self.git("commit", "-qam", "racing")
        racing = self.git("rev-parse", "HEAD").decode().strip()
        plan = plan_staging_sync(self.repository, remote)
        original_run = subprocess.run

        def race_push(command, **options):
            if "push" in command:
                self.assertNotIn("--force", command)
                self.assertFalse(any(argument.startswith("--force-with-lease") for argument in command))
                original_run([
                    "git", "-C", str(self.repository), "push", str(remote), f"{racing}:refs/heads/staging",
                ], check=True, capture_output=True)
            return original_run(command, **options)

        with patch("git_branches.subprocess.run", side_effect=race_push):
            with self.assertRaises(RevisionBranchUncertain):
                sync_staging(plan)
        self.assertTrue(self.git("ls-remote", str(remote), "refs/heads/staging").startswith(racing.encode()))

    def test_upstream_history_is_read_only_and_does_not_infer_squashed_changes(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-ref", "refs/heads/base-only", self.base)
        included = inspect_upstream_history(self.repository, head, "HEAD")
        self.assertEqual(included.classification, "head_in_history")
        self.assertFalse(included.shallow_repository)
        self.assertTrue(included.tree_matches_revision_head)
        self.assertFalse(included.local_arrival_candidate)
        self.assertEqual(inspect_upstream_history(self.repository, head, "refs/heads/base-only").classification, "not_verified")
        unavailable = inspect_upstream_history(self.repository, "f" * 40, "HEAD")
        self.assertEqual(unavailable.classification, "head_unavailable")
        self.assertIsNone(unavailable.tree_matches_revision_head)
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        patch = message("patch@example.test", "[PATCH 1/1] fixture")
        ledger.record_parsed_message(patch)
        (revision,) = discover_series((patch,))
        ledger.record_series_revision(revision)
        key = ("https://gitea.test/api/v1/repos/o/r", revision.series_key, revision.revision)
        ledger.begin_publication(*key, "digest", self.base, "lore/branch", head)
        ledger.transition_publication(*key, expected=("prepared",), status="complete", pull_index=7)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main([
                "inspect-upstream", "--ledger", str(ledger.path), "--repository", str(self.repository),
                "--api-url", "https://gitea.test/api/v1/", "--owner", "o", "--repo", "r", "--pull-index", "7",
            ]), 0)
        self.assertEqual(json.loads(output.getvalue())["evidence"]["classification"], "head_in_history")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main([
                "inspect-upstream", "--ledger", str(ledger.path), "--repository", str(self.repository),
                "--api-url", "https://gitea.test/api/v1/", "--owner", "o", "--repo", "r", "--pull-index", "7",
                "--patch-equivalence",
            ]), 0)
        evidence = json.loads(output.getvalue())["evidence"]["patch_equivalence"]
        self.assertEqual(evidence["base_commit"], self.base)
        self.assertEqual([item["evidence"] for item in evidence["commits"]], ["ancestor", "ancestor"])
        self.assertEqual(json.loads(output.getvalue())["evidence"]["revision_paths"], {"changed_paths": 1, "matching_paths": 1})
        self.assertTrue(json.loads(output.getvalue())["evidence"]["local_arrival_candidate"])
        (self.repository / ".git" / "info" / "grafts").write_text("")
        with self.assertRaisesRegex(RevisionBranchError, "grafts"):
            inspect_upstream_history(self.repository, head, "HEAD")

    def test_upstream_tree_evidence_detects_changes_and_reverts(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        unrelated = self.repository / "unrelated.txt"
        unrelated.write_text("unrelated\n")
        self.git("add", "unrelated.txt")
        self.git("commit", "-qm", "unrelated")
        changed = inspect_upstream_history(self.repository, head, "HEAD")
        self.assertEqual(changed.classification, "head_in_history")
        self.assertFalse(changed.tree_matches_revision_head)
        self.git("revert", "--no-edit", "HEAD")
        restored = inspect_upstream_history(self.repository, head, "HEAD")
        self.assertTrue(restored.tree_matches_revision_head)
        self.git("revert", "--no-edit", head)
        reverted = inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base)
        self.assertEqual(reverted.classification, "head_in_history")
        self.assertFalse(reverted.tree_matches_revision_head)
        self.assertFalse(reverted.local_arrival_candidate)

    def test_upstream_patch_equivalence_is_bounded_and_not_a_merge_decision(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        first = self.git("rev-parse", "HEAD^").decode().strip()
        self.git("checkout", "-qb", "upstream", self.base)
        self.git("-c", "user.name=Other Committer", "cherry-pick", first)
        partial = inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base)
        self.assertEqual(partial.classification, "not_verified")
        self.assertFalse(partial.local_arrival_candidate)
        self.assertEqual([item.evidence for item in partial.patch_equivalence.commits], ["patch_equivalent", "not_found"])
        self.assertEqual((partial.revision_paths.changed_paths, partial.revision_paths.matching_paths), (1, 0))
        self.git("-c", "user.name=Other Committer", "cherry-pick", head)
        before = (self.git("rev-parse", "HEAD"), self.git("show-ref"), self.git("status", "--porcelain"))
        matched = inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base)
        self.assertEqual([item.evidence for item in matched.patch_equivalence.commits], ["patch_equivalent", "patch_equivalent"])
        self.assertFalse(matched.local_arrival_candidate)
        self.assertEqual((matched.revision_paths.changed_paths, matched.revision_paths.matching_paths), (1, 1))
        self.assertEqual((self.git("rev-parse", "HEAD"), self.git("show-ref"), self.git("status", "--porcelain")), before)
        with self.assertRaisesRegex(RevisionBranchError, "scan limit"):
            inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base, patch_scan_limit=1)
        self.git("revert", "--no-edit", "HEAD")
        reverted = inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base)
        self.assertEqual([item.evidence for item in reverted.patch_equivalence.commits], ["patch_equivalent", "patch_equivalent"])
        self.assertEqual((reverted.revision_paths.changed_paths, reverted.revision_paths.matching_paths), (1, 0))

    def test_upstream_revision_paths_include_renames_modes_and_ignore_unrelated_files(self):
        renamed = "renamed\nfile.txt"
        self.git("mv", "content.txt", renamed)
        self.git("commit", "-qm", "rename")
        head = self.git("rev-parse", "HEAD").decode().strip()
        (self.repository / "unrelated.txt").write_text("unrelated\n")
        self.git("add", "unrelated.txt")
        self.git("commit", "-qm", "unrelated")
        matched = inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base)
        self.assertFalse(matched.tree_matches_revision_head)
        self.assertEqual((matched.revision_paths.changed_paths, matched.revision_paths.matching_paths), (2, 2))
        self.assertTrue(matched.local_arrival_candidate)
        (self.repository / renamed).chmod(0o755)
        self.git("add", renamed)
        self.git("commit", "-qm", "change mode")
        changed = inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base)
        self.assertEqual((changed.revision_paths.changed_paths, changed.revision_paths.matching_paths), (2, 1))
        self.assertFalse(changed.local_arrival_candidate)
        self.assertIsNone(inspect_upstream_history(self.repository, head, "HEAD").revision_paths)

    def test_upstream_revision_paths_report_net_zero_without_inferring_arrival(self):
        self.git("checkout", "-qb", "net-zero", self.base)
        self.file.write_text("temporary\n")
        self.git("commit", "-qam", "temporary")
        self.git("revert", "--no-edit", "HEAD")
        head = self.git("rev-parse", "HEAD").decode().strip()
        evidence = inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base)
        self.assertEqual((evidence.revision_paths.changed_paths, evidence.revision_paths.matching_paths), (0, 0))
        self.assertFalse(evidence.local_arrival_candidate)

    def test_upstream_arrival_candidate_rejects_shallow_history(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        (self.repository / ".git" / "shallow").write_text(f"{self.base}\n")
        evidence = inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base)
        self.assertTrue(evidence.shallow_repository)
        self.assertEqual(evidence.classification, "head_in_history")
        self.assertFalse(evidence.local_arrival_candidate)

    def test_upstream_revision_paths_report_partially_applied_and_reverted_files(self):
        (self.repository / "other.txt").write_text("other\n")
        self.git("add", "other.txt")
        self.git("commit", "-qm", "add other")
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("checkout", "-qb", "partial-upstream", self.base)
        self.git("cherry-pick", head)
        partial = inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base)
        self.assertEqual((partial.revision_paths.changed_paths, partial.revision_paths.matching_paths), (2, 1))
        self.git("revert", "--no-edit", "HEAD")
        reverted = inspect_upstream_history(self.repository, head, "HEAD", base_commit=self.base)
        self.assertEqual((reverted.revision_paths.changed_paths, reverted.revision_paths.matching_paths), (2, 0))

    def test_upstream_patch_equivalence_rejects_unavailable_unrelated_and_merge_history(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        first = self.git("rev-parse", "HEAD^").decode().strip()
        with self.assertRaisesRegex(RevisionBranchError, "must be available"):
            inspect_upstream_history(self.repository, "f" * 40, "HEAD", base_commit=self.base)
        self.git("update-ref", "refs/heads/base-only", self.base)
        with self.assertRaisesRegex(RevisionBranchError, "ancestor of both"):
            inspect_upstream_history(self.repository, head, "refs/heads/base-only", base_commit=first)
        self.git("checkout", "-qb", "side", first)
        (self.repository / "other.txt").write_text("other\n")
        self.git("add", "other.txt")
        self.git("commit", "-qm", "side")
        self.git("checkout", "-qB", "merge-revision", head)
        self.git("merge", "--no-ff", "-qm", "merge", "side")
        merge_head = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(RevisionBranchError, "non-merge"):
            inspect_upstream_history(self.repository, merge_head, "HEAD", base_commit=self.base)

    def test_revision_branch_rejects_mismatched_target_and_patch_count(self):
        remote = self.create_remote()
        arguments = {
            "target_branch": "main", "series_key": "lore:thread@example.test",
            "revision": 1, "expected_patch_count": 2,
        }
        self.git("push", str(remote), "HEAD:refs/heads/main")
        with prepare_patch_mailbox(self.repository, self.base, self.mailbox) as prepared:
            with self.assertRaisesRegex(RevisionBranchConflict, "Target ref"):
                ensure_revision_branch(prepared, remote, write_enabled=True, **arguments)
            arguments["expected_patch_count"] = 1
            with self.assertRaisesRegex(ValueError, "patch count"):
                ensure_revision_branch(prepared, remote, write_enabled=True, **arguments)

    def test_revision_branch_create_only_lease_rejects_racing_fast_forward(self):
        remote = self.create_remote()
        series_key = "lore:thread@example.test"
        branch = revision_branch_name(series_key, 1)
        with prepare_patch_mailbox(self.repository, self.base, self.mailbox) as prepared:
            run_git = type(prepared).run_git

            def race(instance, stage, *arguments, **kwargs):
                if stage == "Create revision branch":
                    self.git("push", str(remote), f"{self.base}:refs/heads/{branch}")
                return run_git(instance, stage, *arguments, **kwargs)

            with patch.object(type(prepared), "run_git", new=race):
                with self.assertRaisesRegex(RevisionBranchConflict, "already differs"):
                    ensure_revision_branch(
                        prepared, remote, target_branch="main", series_key=series_key,
                        revision=1, expected_patch_count=2, write_enabled=True,
                    )

        remote_ref = self.git("ls-remote", str(remote), f"refs/heads/{branch}")
        self.assertTrue(remote_ref.startswith(self.base.encode()))

    def test_revision_branch_reconciles_lost_push_acknowledgment(self):
        remote = self.create_remote()
        with prepare_patch_mailbox(self.repository, self.base, self.mailbox) as prepared:
            run_git = type(prepared).run_git

            def lose_acknowledgment(instance, stage, *arguments, **kwargs):
                result = run_git(instance, stage, *arguments, **kwargs)
                if stage == "Create revision branch":
                    raise GitPreflightError("simulated lost acknowledgment")
                return result

            with patch.object(type(prepared), "run_git", new=lose_acknowledgment):
                result = ensure_revision_branch(
                    prepared, remote, target_branch="main", series_key="lore:thread@example.test",
                    revision=1, expected_patch_count=2, write_enabled=True,
                )

        self.assertFalse(result.created)
        self.assertFalse(result.planned)

    def test_git_http_credentials_use_environment_and_require_secure_origin(self):
        credentials = GitHttpCredentials("operator", "sample-token")
        environment = credentials.environment("http://127.0.0.1:30002/o/r.git")
        self.assertEqual(environment["GIT_CONFIG_KEY_0"], "http.extraHeader")
        self.assertTrue(environment["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
        self.assertEqual(environment["GIT_CONFIG_VALUE_1"], "false")
        self.assertNotIn("sample-token", repr(credentials))
        with self.assertRaisesRegex(ValueError, "require HTTPS"):
            credentials.environment("http://gitea.example/o/r.git")
        with self.assertRaisesRegex(ValueError, "HTTP"):
            credentials.environment("ssh://git@gitea.example/o/r.git")

    def test_rejects_missing_base_and_empty_mailbox(self):
        with self.assertRaisesRegex(GitPreflightError, "Resolve base failed"):
            self.run_with_cleanup_check("missing", self.mailbox)
        with self.assertRaisesRegex(ValueError, "patch mailbox"):
            preflight_patch_mailbox(self.repository, self.base, b"")


if __name__ == "__main__":
    unittest.main()