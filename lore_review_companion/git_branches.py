# SPDX-License-Identifier: GPL-2.0-or-later

"""Create immutable revision branches from an already prepared mailbox."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from lore_review_companion.preflight import GitPreflightError, PreparedMailbox


class RevisionBranchError(RuntimeError):
    """The revision branch could not be safely created or reconciled."""


class RevisionBranchConflict(RevisionBranchError):
    """A target or immutable revision ref does not have the expected value."""


class RevisionBranchUncertain(RevisionBranchError):
    """A branch write could not be confirmed and requires reconciliation."""


@dataclass(frozen=True)
class RevisionBranchResult:
    branch: str
    head_commit: str
    created: bool
    planned: bool = False


@dataclass(frozen=True)
class RevisionRefsResult:
    base: RevisionBranchResult
    head: RevisionBranchResult


def ensure_revision_refs(
    prepared: PreparedMailbox,
    remote: str,
    *,
    target_branch: str,
    series_key: str,
    revision: int,
    expected_patch_count: int,
    write_enabled: bool = False,
    credentials: GitHttpCredentials | None = None,
) -> RevisionRefsResult:
    if (
        type(expected_patch_count) is not int
        or expected_patch_count < 1
        or prepared.result.patch_count != expected_patch_count
    ):
        raise ValueError("assembled patch count does not match the complete revision")
    branch = revision_branch_name(series_key, revision)
    branches = (branch + "-base", branch)
    commits = (prepared.result.base_commit, prepared.result.head_commit)
    remote_address = _remote_address(remote)
    environment = credentials.environment(remote_address) if credentials else {}
    refs = tuple(f"refs/heads/{name}" for name in branches)
    observed = tuple(
        _remote_ref(prepared, remote_address, ref, environment) for ref in refs
    )
    for ref, expected, current in zip(refs, commits, observed, strict=True):
        if current is not None and current != expected:
            raise RevisionBranchConflict(
                f"Immutable revision ref already differs: {ref}"
            )
    if observed[0] is None:
        target_ref = f"refs/heads/{target_branch}"
        prepared.run_git("Validate target branch", "check-ref-format", target_ref)
        if (
            _remote_ref(prepared, remote_address, target_ref, environment)
            != prepared.result.base_commit
        ):
            raise RevisionBranchConflict(
                "Selected upstream target does not match the pinned series base"
            )
    missing = tuple(
        position for position, current in enumerate(observed) if current is None
    )
    if not write_enabled or not missing:
        results = tuple(
            RevisionBranchResult(name, commit, False, planned=position in missing)
            for position, (name, commit) in enumerate(
                zip(branches, commits, strict=True)
            )
        )
        return RevisionRefsResult(*results)
    try:
        prepared.run_git(
            "Create immutable revision refs",
            "push",
            "--atomic",
            "--porcelain",
            *(f"--force-with-lease={refs[position]}:" for position in missing),
            "--",
            remote_address,
            *(f"{commits[position]}:{refs[position]}" for position in missing),
            environment_overrides=environment,
        )
    except (GitPreflightError, subprocess.TimeoutExpired) as error:
        try:
            confirmed = tuple(
                _remote_ref(prepared, remote_address, ref, environment) for ref in refs
            )
        except (
            GitPreflightError,
            subprocess.TimeoutExpired,
            RevisionBranchError,
        ) as read_error:
            raise RevisionBranchUncertain(
                "Immutable revision ref write could not be reconciled"
            ) from read_error
        if confirmed != commits:
            raise RevisionBranchUncertain(
                "Immutable revision ref creation was not confirmed"
            ) from error
        return RevisionRefsResult(
            *(
                RevisionBranchResult(name, commit, False)
                for name, commit in zip(branches, commits, strict=True)
            )
        )
    confirmed = tuple(
        _remote_ref(prepared, remote_address, ref, environment) for ref in refs
    )
    if confirmed != commits:
        raise RevisionBranchUncertain("Immutable revision refs differ after creation")
    return RevisionRefsResult(
        *(
            RevisionBranchResult(name, commit, position in missing)
            for position, (name, commit) in enumerate(
                zip(branches, commits, strict=True)
            )
        )
    )


@dataclass(frozen=True)
class StagingSyncPlan:
    source_repository: str
    target_remote: str
    branch: str
    source_commit: str
    target_commit: str

    def approval_digest(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class StagingSyncResult:
    plan: StagingSyncPlan
    updated: bool
    reconciled: bool = False


@dataclass(frozen=True)
class PatchEquivalence:
    revision_commit: str
    evidence: str


@dataclass(frozen=True)
class PatchEquivalenceSummary:
    base_commit: str
    upstream_commits_scanned: int
    commits: tuple[PatchEquivalence, ...]


@dataclass(frozen=True)
class RevisionPathEvidence:
    changed_paths: int
    matching_paths: int


@dataclass(frozen=True)
class UpstreamEvidence:
    upstream_ref: str
    upstream_commit: str
    revision_head: str
    classification: str
    shallow_repository: bool
    patch_equivalence: PatchEquivalenceSummary | None = None
    tree_matches_revision_head: bool | None = None
    revision_paths: RevisionPathEvidence | None = None
    local_arrival_candidate: bool = False


def inspect_upstream_history(
    repository: Path,
    revision_head: str,
    upstream_ref: str,
    *,
    base_commit: str | None = None,
    patch_scan_limit: int = 1000,
) -> UpstreamEvidence:
    repository = repository.resolve(strict=True)
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision_head) is None:
        raise ValueError("Revision head must be a full Git object ID")
    if upstream_ref != "HEAD" and not re.fullmatch(
        r"refs/(?:heads|remotes)/[A-Za-z0-9._/-]+", upstream_ref
    ):
        raise ValueError("Upstream ref must be HEAD or a named branch ref")
    if (
        not isinstance(patch_scan_limit, int)
        or isinstance(patch_scan_limit, bool)
        or not 1 <= patch_scan_limit <= 1000
    ):
        raise ValueError("Patch scan limit must be between 1 and 1000 commits")
    if (
        base_commit is not None
        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base_commit) is None
    ):
        raise ValueError("Revision base must be a full Git object ID")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"})

    def git(*args: str, allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["git", "--no-replace-objects", "-C", str(repository), *args],
                capture_output=True,
                timeout=30,
                env=environment,
            )
        except subprocess.TimeoutExpired as error:
            raise RevisionBranchError(
                "Local upstream history check timed out"
            ) from error
        if result.returncode not in allowed:
            raise RevisionBranchError("Local upstream history could not be inspected")
        return result

    grafts = (
        git("rev-parse", "--git-path", "info/grafts").stdout.decode("utf-8").strip()
    )
    if (repository / grafts).exists():
        raise RevisionBranchError("Upstream history cannot be verified with Git grafts")
    current = (
        git("rev-parse", "--verify", "--end-of-options", f"{upstream_ref}^{{commit}}")
        .stdout.decode("ascii")
        .strip()
    )
    shallow = git("rev-parse", "--is-shallow-repository").stdout.strip() == b"true"
    available = (
        git(
            "cat-file", "-e", f"{revision_head}^{{commit}}", allowed=(0, 1, 128)
        ).returncode
        == 0
    )
    tree_matches = None
    if not available:
        classification = "head_unavailable"
    else:
        ancestor = (
            git(
                "merge-base", "--is-ancestor", revision_head, current, allowed=(0, 1)
            ).returncode
            == 0
        )
        classification = "head_in_history" if ancestor else "not_verified"
        revision_tree = git(
            "rev-parse", "--verify", "--end-of-options", f"{revision_head}^{{tree}}"
        ).stdout.strip()
        upstream_tree = git(
            "rev-parse", "--verify", "--end-of-options", f"{current}^{{tree}}"
        ).stdout.strip()
        tree_matches = revision_tree == upstream_tree
    patch_equivalence = None
    revision_paths = None
    if base_commit is not None:
        if (
            not available
            or git(
                "cat-file", "-e", f"{base_commit}^{{commit}}", allowed=(0, 1, 128)
            ).returncode
            != 0
        ):
            raise RevisionBranchError(
                "Revision base and head must be available for patch comparison"
            )
        if any(
            git(
                "merge-base", "--is-ancestor", base_commit, commit, allowed=(0, 1)
            ).returncode
            for commit in (revision_head, current)
        ):
            raise RevisionBranchError(
                "Revision base must be an ancestor of both the revision and upstream"
            )
        upstream_count = int(
            git(
                "rev-list",
                "--count",
                f"--max-count={patch_scan_limit + 1}",
                current,
                f"^{base_commit}",
            ).stdout
        )
        if upstream_count > patch_scan_limit:
            raise RevisionBranchError("Upstream history exceeds the patch scan limit")
        lines = (
            git(
                "rev-list",
                "--parents",
                "--reverse",
                "--max-count=501",
                revision_head,
                f"^{base_commit}",
            )
            .stdout.decode("ascii")
            .splitlines()
        )
        if (
            not lines
            or len(lines) > 500
            or any(len(line.split()) != 2 for line in lines)
        ):
            raise RevisionBranchError(
                "Patch comparison requires 1..500 non-merge revision commits"
            )
        commits = tuple(line.split()[0] for line in lines)
        unreachable = set(
            git(
                "rev-list",
                "--max-count=501",
                revision_head,
                f"^{base_commit}",
                f"^{current}",
            )
            .stdout.decode("ascii")
            .splitlines()
        )
        output = (
            git("cherry", "--no-abbrev", current, revision_head, base_commit)
            .stdout.decode("ascii")
            .splitlines()
        )
        matches = {}
        for line in output:
            fields = line.split()
            if (
                len(fields) != 2
                or fields[0] not in ("+", "-")
                or fields[1] not in unreachable
                or fields[1] in matches
            ):
                raise RevisionBranchError(
                    "Git returned unexpected patch comparison evidence"
                )
            matches[fields[1]] = "patch_equivalent" if fields[0] == "-" else "not_found"
        if set(matches) != unreachable:
            raise RevisionBranchError(
                "Git returned incomplete patch comparison evidence"
            )
        patch_equivalence = PatchEquivalenceSummary(
            base_commit,
            upstream_count,
            tuple(
                PatchEquivalence(
                    commit, matches[commit] if commit in unreachable else "ancestor"
                )
                for commit in commits
            ),
        )
        diff_options = (
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            "--no-renames",
            "--name-only",
            "-z",
        )
        changed_paths = set(
            git(*diff_options, base_commit, revision_head, "--").stdout.split(b"\0")
        ) - {b""}
        differing_paths = set(
            git(*diff_options, revision_head, current, "--").stdout.split(b"\0")
        ) - {b""}
        revision_paths = RevisionPathEvidence(
            len(changed_paths), len(changed_paths - differing_paths)
        )
    local_candidate = bool(
        not shallow
        and classification == "head_in_history"
        and patch_equivalence
        and revision_paths
        and revision_paths.changed_paths > 0
        and revision_paths.matching_paths == revision_paths.changed_paths
        and all(commit.evidence == "ancestor" for commit in patch_equivalence.commits)
    )
    return UpstreamEvidence(
        upstream_ref,
        current,
        revision_head,
        classification,
        shallow,
        patch_equivalence,
        tree_matches,
        revision_paths,
        local_candidate,
    )


@dataclass(frozen=True)
class GitHttpCredentials:
    username: str
    token: str = field(repr=False)

    def environment(self, remote: str) -> dict[str, str]:
        parsed = urlsplit(remote)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Git HTTP credentials require an HTTP(S) remote")
        if parsed.scheme == "http":
            try:
                is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                is_loopback = parsed.hostname.lower() == "localhost"
            if not is_loopback:
                raise ValueError("Git credentials require HTTPS except on loopback")
        if (
            not self.username
            or ":" in self.username
            or not self.token
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.username + self.token
            )
        ):
            raise ValueError("invalid Git HTTP credentials")
        encoded = base64.b64encode(f"{self.username}:{self.token}".encode()).decode(
            "ascii"
        )
        return {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
            "GIT_CONFIG_KEY_1": "http.followRedirects",
            "GIT_CONFIG_VALUE_1": "false",
        }


def revision_branch_name(series_key: str, revision: int) -> str:
    if not isinstance(series_key, str) or not series_key.strip():
        raise ValueError("series key must be non-empty")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("revision must be a positive integer")
    digest = hashlib.sha256(series_key.encode("utf-8")).hexdigest()
    return f"lore/{digest}/v{revision}"


def _remote_address(remote: str | Path) -> str:
    address = str(remote)
    if any(character in address for character in "\r\n\0"):
        raise ValueError("invalid Git remote address")
    if Path(address).is_absolute():
        return address
    parsed = urlsplit(address)
    if (
        parsed.scheme not in ("http", "https", "ssh")
        or not parsed.hostname
        or parsed.password is not None
        or (parsed.scheme != "ssh" and parsed.username is not None)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Git remote must be an absolute path or a credential-free HTTP(S)/SSH URL"
        )
    return address


def plan_staging_sync(
    source_repository: Path,
    remote: str | Path,
    *,
    branch: str = "staging",
    credentials: GitHttpCredentials | None = None,
) -> StagingSyncPlan:
    source_repository = source_repository.resolve(strict=True)
    remote_address = _remote_address(remote)
    if (
        Path(remote_address).is_absolute()
        and Path(remote_address).exists()
        and source_repository.samefile(remote_address)
    ):
        raise ValueError("Source and target repositories must be distinct")
    if not isinstance(branch, str) or not branch:
        raise ValueError("A target branch is required")
    ref = f"refs/heads/{branch}"
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    if credentials:
        environment.update(credentials.environment(remote_address))

    def git(*args: str, allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["git", "--no-replace-objects", "-C", str(source_repository), *args],
                capture_output=True,
                timeout=30,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RevisionBranchError(
                "Staging synchronization planning failed"
            ) from error
        if result.returncode not in allowed:
            raise RevisionBranchError("Staging synchronization planning failed")
        return result

    git("check-ref-format", ref)
    grafts = (
        git("rev-parse", "--git-path", "info/grafts").stdout.decode("utf-8").strip()
    )
    if (source_repository / grafts).exists():
        raise RevisionBranchError("Staging synchronization cannot use Git grafts")
    if git("rev-parse", "--is-shallow-repository").stdout.strip() != b"false":
        raise RevisionBranchError(
            "Staging synchronization requires complete local history"
        )
    source_commit = (
        git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        .stdout.decode("ascii")
        .strip()
    )
    output = (
        git("ls-remote", "--refs", "--heads", "--", remote_address, ref)
        .stdout.decode("ascii")
        .splitlines()
    )
    if len(output) != 1:
        raise RevisionBranchConflict("Target staging branch is missing or ambiguous")
    fields = output[0].split("\t")
    if (
        len(fields) != 2
        or fields[1] != ref
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]) is None
    ):
        raise RevisionBranchError("Target staging branch returned an invalid ref")
    target_commit = fields[0]
    if git(
        "cat-file", "-e", f"{target_commit}^{{commit}}", allowed=(0, 1, 128)
    ).returncode:
        raise RevisionBranchConflict(
            "Target staging commit is unavailable in the source repository"
        )
    if git(
        "merge-base", "--is-ancestor", target_commit, source_commit, allowed=(0, 1)
    ).returncode:
        raise RevisionBranchConflict(
            "Target staging is not an ancestor of source staging"
        )
    return StagingSyncPlan(
        str(source_repository), remote_address, branch, source_commit, target_commit
    )


def sync_staging(
    plan: StagingSyncPlan,
    *,
    credentials: GitHttpCredentials | None = None,
) -> StagingSyncResult:
    current = plan_staging_sync(
        Path(plan.source_repository),
        plan.target_remote,
        branch=plan.branch,
        credentials=credentials,
    )
    if current != plan:
        raise RevisionBranchConflict("Staging refs changed before synchronization")
    if plan.target_commit == plan.source_commit:
        return StagingSyncResult(plan, updated=False)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    if credentials:
        environment.update(credentials.environment(plan.target_remote))
    command = [
        "git",
        "--no-replace-objects",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "push.gpgSign=false",
        "-C",
        plan.source_repository,
        "push",
        "--porcelain",
        "--no-follow-tags",
        "--no-recurse-submodules",
        "--",
        plan.target_remote,
        f"{plan.source_commit}:refs/heads/{plan.branch}",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=120, env=environment
        )
        failed = result.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    try:
        observed = plan_staging_sync(
            Path(plan.source_repository),
            plan.target_remote,
            branch=plan.branch,
            credentials=credentials,
        )
    except (RevisionBranchError, ValueError) as error:
        raise RevisionBranchUncertain(
            "Staging update could not be confirmed"
        ) from error
    if observed.target_commit != plan.source_commit:
        raise RevisionBranchUncertain("Staging update did not reach the planned commit")
    return StagingSyncResult(plan, updated=True, reconciled=failed)


def _remote_ref(
    prepared: PreparedMailbox,
    remote: str,
    ref: str,
    environment: Mapping[str, str],
) -> str | None:
    output = prepared.run_git(
        "Inspect remote ref",
        "ls-remote",
        "--refs",
        "--heads",
        "--",
        remote,
        ref,
        environment_overrides=environment,
    )
    if not output:
        return None
    lines = output.splitlines()
    if len(lines) != 1:
        raise RevisionBranchError(f"Remote returned ambiguous ref: {ref}")
    fields = lines[0].split("\t")
    if (
        len(fields) != 2
        or fields[1] != ref
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0])
    ):
        raise RevisionBranchError(f"Remote returned an invalid ref: {ref}")
    return fields[0]


def ensure_revision_branch(
    prepared: PreparedMailbox,
    remote: str | Path,
    *,
    target_branch: str,
    series_key: str,
    revision: int,
    expected_patch_count: int,
    write_enabled: bool = False,
    credentials: GitHttpCredentials | None = None,
) -> RevisionBranchResult:
    """Preview or create a revision ref, never replacing an existing ref."""
    branch = revision_branch_name(series_key, revision)
    if (
        not isinstance(expected_patch_count, int)
        or isinstance(expected_patch_count, bool)
        or expected_patch_count < 1
        or prepared.result.patch_count != expected_patch_count
    ):
        raise ValueError("assembled patch count does not match the complete revision")
    if not isinstance(target_branch, str) or not target_branch:
        raise ValueError("target branch must be non-empty")
    target_ref = f"refs/heads/{target_branch}"
    prepared.run_git("Validate target branch", "check-ref-format", target_ref)
    remote_address = _remote_address(remote)
    environment = credentials.environment(remote_address) if credentials else {}
    revision_ref = f"refs/heads/{branch}"
    target_head = _remote_ref(prepared, remote_address, target_ref, environment)
    if target_head != prepared.result.base_commit:
        raise RevisionBranchConflict(
            f"Target ref {target_ref} is {target_head or 'missing'},"
            f" expected {prepared.result.base_commit}"
        )
    existing_head = _remote_ref(prepared, remote_address, revision_ref, environment)
    if existing_head:
        if existing_head != prepared.result.head_commit:
            raise RevisionBranchConflict(
                f"Immutable revision ref already differs: {revision_ref}"
            )
        return RevisionBranchResult(branch, existing_head, created=False)
    if not write_enabled:
        return RevisionBranchResult(
            branch, prepared.result.head_commit, created=False, planned=True
        )

    try:
        prepared.run_git(
            "Create revision branch",
            "push",
            "--porcelain",
            f"--force-with-lease={revision_ref}:",
            "--",
            remote_address,
            f"{prepared.result.head_commit}:{revision_ref}",
            environment_overrides=environment,
        )
    except (GitPreflightError, subprocess.TimeoutExpired) as error:
        try:
            existing_head = _remote_ref(
                prepared, remote_address, revision_ref, environment
            )
        except (
            GitPreflightError,
            subprocess.TimeoutExpired,
            RevisionBranchError,
        ) as reconciliation_error:
            raise RevisionBranchUncertain(
                "Branch write and reconciliation both failed"
            ) from reconciliation_error
        if existing_head == prepared.result.head_commit:
            return RevisionBranchResult(branch, existing_head, created=False)
        if existing_head:
            raise RevisionBranchConflict(
                f"Immutable revision ref already differs: {revision_ref}"
            ) from error
        raise RevisionBranchUncertain(
            f"Branch write was not confirmed: {error}"
        ) from error

    try:
        existing_head = _remote_ref(prepared, remote_address, revision_ref, environment)
    except (GitPreflightError, subprocess.TimeoutExpired, RevisionBranchError) as error:
        raise RevisionBranchUncertain("Branch write could not be confirmed") from error
    if existing_head != prepared.result.head_commit:
        raise RevisionBranchUncertain(
            f"Branch write returned an unexpected ref: {revision_ref}"
        )
    return RevisionBranchResult(branch, existing_head, created=True)
