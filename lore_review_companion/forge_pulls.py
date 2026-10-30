# SPDX-License-Identifier: GPL-2.0-or-later

"""Create and reconcile pull requests for immutable series revisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lore_review_companion.git_branches import GitHttpCredentials, RevisionBranchConflict, RevisionBranchResult, RevisionBranchUncertain, StagingSyncResult, plan_staging_sync, revision_branch_name, sync_staging
from lore_review_companion.forge_api import AuthenticatedUser, ForgeAPI, ForgeAPIError, authenticated_user
from lore_review_companion.forge_comments import DeliveryResult, DeliveryUncertainError
from lore_review_companion.ledger import Ledger, WorkerBusyError


class PullRequestConflict(RuntimeError):
    """The existing repository or pull request requires operator intervention."""


class StagingWorkerBusy(WorkerBusyError):
    """Staging synchronization could not acquire its initial worker lock."""


@dataclass(frozen=True)
class ReviewAccess:
    destination: str
    repository_id: int
    actor: AuthenticatedUser
    account_push_reported: bool = True
    token_write_scopes_verified: bool = False
    branch_permissions_verified: bool = False


@dataclass(frozen=True)
class ReviewStagingComparison:
    source_repository: str
    review_repository: str
    branch: str
    source_commit: str
    review_commit: str
    same_tip: bool


@dataclass(frozen=True)
class ReviewStagingSync:
    actor: AuthenticatedUser
    repository_id: int
    result: StagingSyncResult


def inspect_review_staging(
    api: ForgeAPI, source_owner: str, source_repo: str, review_owner: str, review_repo: str, branch: str,
) -> ReviewStagingComparison:
    names = (source_owner, source_repo, review_owner, review_repo, branch)
    if any(not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None for name in names):
        raise ValueError("Repository and branch names must be simple explicit names")
    if (source_owner.casefold(), source_repo.casefold()) == (review_owner.casefold(), review_repo.casefold()):
        raise ValueError("Source and review repositories must be distinct")
    paths = tuple(f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}" for owner, repo in (
        (source_owner, source_repo), (review_owner, review_repo),
    ))
    commits = []
    for path in paths:
        response = api.request_json(f"{path}/branches/{quote(branch, safe='')}")
        commit = response.get("commit") if isinstance(response, dict) else None
        object_id = commit.get("id") if isinstance(commit, dict) else None
        if (
            not isinstance(response, dict) or response.get("name") != branch
            or not isinstance(object_id, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", object_id) is None
        ):
            raise ForgeAPIError(200, "Gitea returned invalid branch comparison data")
        commits.append(object_id)
    return ReviewStagingComparison(
        api.base_url + paths[0], api.base_url + paths[1], branch, commits[0], commits[1], commits[0] == commits[1],
    )


def sync_review_staging(
    ledger: Ledger, api: ForgeAPI, source_owner: str, source_repo: str, review_owner: str, review_repo: str,
    source_repository: Path, remote: str, *, branch: str = "staging",
) -> ReviewStagingSync:
    from lore_review_companion.series_sync import _validate_destination

    if not api.token:
        raise ValueError("Staging synchronization requires an authenticated token")
    publisher = ForgePullPublisher(api, review_owner, review_repo)
    _validate_destination(publisher, remote)
    pending = Path(f"{ledger.path.resolve()}.staging-sync-pending")
    try:
        pending.lstat()
    except FileNotFoundError:
        pass
    else:
        raise RevisionBranchUncertain("Earlier staging synchronization requires inspection before retry")
    with ExitStack() as stack:
        try:
            stack.enter_context(ledger.worker_lock())
        except WorkerBusyError as error:
            raise StagingWorkerBusy("Another worker holds the ledger worker lock; staging was not attempted") from error
        try:
            descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except FileExistsError as error:
            raise RevisionBranchUncertain("Earlier staging synchronization requires inspection before retry") from error
        try:
            os.fsync(descriptor)
            directory = os.open(pending.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            ledger.assert_unconflicted(())
            if ledger.status().conflicted_message_ids:
                raise RevisionBranchConflict("Message-ID conflicts require inspection before staging synchronization")
            actor = authenticated_user(api)
            repository_id = publisher.check_repository()
            comparison = inspect_review_staging(api, source_owner, source_repo, review_owner, review_repo, branch)
            credentials = GitHttpCredentials(actor.login, api.token)
            plan = plan_staging_sync(source_repository, remote, branch=branch, credentials=credentials)
            if (plan.source_commit, plan.target_commit) != (comparison.source_commit, comparison.review_commit):
                raise RevisionBranchConflict("Gitea and Git staging refs do not match")
            plan_bytes = json.dumps(asdict(plan), sort_keys=True).encode("utf-8")
            if os.write(descriptor, plan_bytes) != len(plan_bytes):
                raise RevisionBranchUncertain("Staging synchronization plan could not be recorded completely")
            os.fsync(descriptor)
            result = ReviewStagingSync(actor, repository_id, sync_staging(plan, credentials=credentials))
            pending.unlink()
            return result
        finally:
            os.close(descriptor)


def _series_marker(series_key: str) -> str:
    digest = hashlib.sha256(series_key.encode("utf-8")).hexdigest()
    return f"<!-- lore-gitea-mirror:series:v1:{digest} -->"


class ForgePullPublisher:
    def __init__(self, api: ForgeAPI, owner: str, repo: str, *, write_enabled: bool = False):
        if not owner or not repo:
            raise ValueError("owner and repository must be non-empty")
        self.api = api
        self.owner = owner
        self.repo = repo
        self.repository_path = f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        self.write_enabled = write_enabled

    def inspect_access(self) -> ReviewAccess:
        if not self.api.token:
            raise ValueError("Review access inspection requires an authenticated token")
        actor = authenticated_user(self.api)
        return ReviewAccess(self.api.base_url + self.repository_path, self.check_repository(), actor)

    def check_repository(self) -> int:
        repository = self.api.request_json(self.repository_path)
        if not isinstance(repository, dict):
            raise ForgeAPIError(200, "Expected a repository object")
        repository_id = repository.get("id")
        permissions = repository.get("permissions")
        if (
            not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id < 1
            or repository.get("mirror") is not False
            or repository.get("has_pull_requests") is not True
            or not isinstance(permissions, dict) or permissions.get("push") is not True
        ):
            raise PullRequestConflict("Repository must be a writable non-mirror with pull requests enabled")
        return repository_id

    def ensure_pull_request(
        self,
        *,
        series_key: str,
        revision: int,
        branch: RevisionBranchResult,
        target_branch: str,
        title: str,
        body: str,
        labels: tuple[int, ...] = (),
        revision_policy: str = "block",
    ) -> DeliveryResult:
        expected_branch = revision_branch_name(series_key, revision)
        if branch.branch != expected_branch or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", branch.head_commit):
            raise ValueError("revision branch does not match the prepared series")
        if not target_branch or not title.strip():
            raise ValueError("target branch and pull request title must be non-empty")
        if any(not isinstance(label, int) or isinstance(label, bool) or label < 1 for label in labels):
            raise ValueError("label IDs must be positive integers")
        if revision_policy not in ("block", "separate"):
            raise ValueError("revision policy must be block or separate")
        if self.write_enabled and branch.planned:
            raise ValueError("revision branch has not been published")

        repository_id = self.check_repository()
        path = f"{self.repository_path}/pulls"
        marker = _series_marker(series_key)

        def find_existing() -> dict[str, Any] | None:
            matches = []
            others = []
            for pull in self.api.get_pages(path, params={"state": "all"}):
                if not isinstance(pull, dict):
                    raise ForgeAPIError(200, "Expected pull request objects")
                head = pull.get("head") or {}
                base = pull.get("base") or {}
                if marker in str(pull.get("body", "")):
                    if isinstance(head, dict) and head.get("ref") == branch.branch:
                        matches.append(pull)
                    else:
                        others.append(pull)
                elif (
                    isinstance(head, dict) and isinstance(base, dict)
                    and head.get("repo_id") == repository_id and head.get("ref") == branch.branch
                    and base.get("repo_id") == repository_id and base.get("ref") == target_branch
                ):
                    raise PullRequestConflict("An unmarked pull request already uses this branch")
            if len(matches) > 1:
                raise PullRequestConflict("Multiple pull requests have the same revision provenance")
            if matches and not self._matches(matches[0], marker, repository_id, branch, target_branch):
                raise PullRequestConflict("Series is already mapped to a different revision or pull request head")
            if not matches and others and revision_policy == "block":
                raise PullRequestConflict("Series is already mapped to a different revision or pull request head")
            if revision_policy == "separate":
                prefix = expected_branch.rsplit("/v", 1)[0]
                for other in others:
                    head = other.get("head") or {}
                    base = other.get("base") or {}
                    number = other.get("number")
                    if (
                        not isinstance(number, int) or isinstance(number, bool) or number < 1
                        or not isinstance(head, dict) or head.get("repo_id") != repository_id
                        or not isinstance(head.get("ref"), str)
                        or re.fullmatch(re.escape(prefix) + r"/v[1-9][0-9]*", head["ref"]) is None
                        or not isinstance(base, dict) or base.get("repo_id") != repository_id or base.get("ref") != target_branch
                    ):
                        raise PullRequestConflict("Related pull request has inconsistent series provenance")
            return matches[0] if matches else None

        existing = find_existing()
        if existing:
            return DeliveryResult(existing, created=False)
        description = body.rstrip()
        payload = {
            "head": branch.branch,
            "base": target_branch,
            "title": title.strip(),
            "body": f"{description}\n\n{marker}" if description else marker,
            "labels": list(labels),
        }
        if not self.write_enabled:
            return DeliveryResult({"path": path, **payload}, created=False, planned=True)

        try:
            created = self.api.request_json(path, method="POST", payload=payload)
        except ForgeAPIError as error:
            try:
                existing = find_existing()
            except ForgeAPIError as reconciliation_error:
                raise DeliveryUncertainError("Pull request write and reconciliation both failed") from reconciliation_error
            if existing:
                return DeliveryResult(existing, created=False)
            if error.status is None or error.status >= 500 or 200 <= error.status < 300:
                raise DeliveryUncertainError("Pull request write could not be confirmed") from error
            raise

        if isinstance(created, dict) and self._matches(created, marker, repository_id, branch, target_branch):
            return DeliveryResult(created, created=True)
        existing = find_existing()
        if existing:
            return DeliveryResult(existing, created=True)
        raise DeliveryUncertainError("Gitea accepted the pull request but it is not yet visible")

    @staticmethod
    def _matches(
        pull: dict[str, Any], marker: str, repository_id: int,
        branch: RevisionBranchResult, target_branch: str,
    ) -> bool:
        head = pull.get("head")
        base = pull.get("base")
        number = pull.get("number")
        return (
            isinstance(number, int) and not isinstance(number, bool) and number > 0
            and marker in str(pull.get("body", ""))
            and isinstance(head, dict) and head.get("repo_id") == repository_id
            and head.get("ref") == branch.branch and head.get("sha") == branch.head_commit
            and isinstance(base, dict) and base.get("repo_id") == repository_id
            and base.get("ref") == target_branch
        )