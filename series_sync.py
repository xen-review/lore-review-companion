"""Preview and explicitly approve one complete series for Gitea publication."""

from __future__ import annotations

import hashlib
import hmac
import json
import mailbox
import tempfile
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import quote, urlsplit

from b4_adapter import assemble_local_series
from git_branches import GitHttpCredentials, RevisionBranchResult, ensure_revision_branch
from gitea_api import authenticated_user
from gitea_comments import DeliveryResult, quote_untrusted_text, with_tool_attribution
from gitea_pulls import GiteaPullPublisher
from ledger import Ledger, PublicationConflict
from preflight import prepare_patch_mailbox
from series import SeriesRevision, discover_series


@dataclass(frozen=True)
class SeriesPlan:
    series_key: str
    revision: int
    source_message_id: str
    source_sha256: tuple[str, ...]
    api_url: str
    owner: str
    repository: str
    git_remote: str
    target_branch: str
    base_commit: str
    revision_branch: str
    head_commit: str
    title: str
    body: str
    labels: tuple[int, ...]
    actor: str
    actor_id: int
    git_username: str | None
    revision_policy: str = "block"

    def approval_digest(self, *, legacy: bool = False) -> str:
        plan = asdict(self)
        if self.revision_policy == "block":
            del plan["revision_policy"]
        if legacy:
            for name in ("actor", "actor_id", "git_username"):
                del plan[name]
        value = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class SeriesSyncResult:
    plan: SeriesPlan
    approval_digest: str
    branch: RevisionBranchResult
    pull_request: DeliveryResult
    applied: bool
    attribution_status: str


class PublicationUncertain(RuntimeError):
    """An earlier PR write must be reconciled or explicitly retried."""


def _attribution_status(pull: DeliveryResult, body: str) -> str:
    if pull.planned:
        return "planned"
    remote_body = pull.remote_object.get("body")
    return "present" if isinstance(remote_body, str) and body in remote_body else "existing_pr_unmodified"


def _validate_destination(publisher: GiteaPullPublisher, remote: str) -> None:
    api = urlsplit(publisher.api.base_url)
    git = urlsplit(remote)
    api_path = api.path.rstrip("/")
    if not api_path.endswith("/api/v1"):
        raise ValueError("Gitea API URL must end in /api/v1")
    prefix = api_path[:-len("/api/v1")]
    expected_path = f"{prefix}/{quote(publisher.owner, safe='')}/{quote(publisher.repo, safe='')}.git"
    default_port = {"http": 80, "https": 443}
    if (
        git.scheme not in default_port or git.scheme != api.scheme
        or git.hostname != api.hostname
        or (git.port or default_port[git.scheme]) != (api.port or default_port[api.scheme])
        or git.path != expected_path or git.username is not None or git.password is not None
        or git.query or git.fragment
    ):
        raise ValueError("Git remote must identify the same HTTP(S) Gitea repository as the API")


def sync_series_revision(
    revision: SeriesRevision,
    repository: Path,
    base_ref: str,
    remote: str,
    publisher: GiteaPullPublisher,
    *,
    target_branch: str,
    inbox_url: str = "https://lore.kernel.org/xen-devel/",
    labels: tuple[int, ...] = (),
    revision_policy: str = "block",
    credentials: GitHttpCredentials | None = None,
    approved_digest: str | None = None,
    ledger: Ledger | None = None,
    retry_uncertain: bool = False,
) -> SeriesSyncResult:
    """Preview publication or apply the exact independently reviewed plan."""
    natural = discover_series(revision.messages)
    if not revision.complete or revision.conflicts or len(natural) != 1:
        raise ValueError("Only one complete, unambiguous series revision can be published")
    if natural[0] != revision:
        candidate = replace(natural[0], series_key=revision.series_key, root_message_id=revision.root_message_id)
        source_id = revision.cover_message_id or revision.messages[0].message_id
        if (
            candidate != revision or ledger is None or source_id is None
            or revision not in ledger.discover_related_series(source_id)
        ):
            raise ValueError("Linked series revision is not verified by the ledger")
    if publisher.write_enabled and not approved_digest:
        raise ValueError("Publishing requires the approval digest from a reviewed dry run")
    if publisher.write_enabled and ledger is None:
        raise ValueError("Publishing requires a durable ledger")
    if retry_uncertain and not publisher.write_enabled:
        raise ValueError("Retrying an uncertain publication requires writes to be enabled")
    if ledger is not None:
        ledger.assert_unconflicted(tuple(message.message_id for message in revision.messages if message.message_id))
    _validate_destination(publisher, remote)
    actor = authenticated_user(publisher.api)
    inbox = urlsplit(inbox_url)
    if (
        inbox.scheme != "https" or not inbox.hostname or inbox.username is not None
        or inbox.password is not None or inbox.query or inbox.fragment
    ):
        raise ValueError("Inbox URL must be a credential-free HTTPS URL")
    source_id = revision.cover_message_id or revision.messages[0].message_id
    if source_id is None or revision.expected_patch_count is None:
        raise ValueError("Series source and patch count are required")
    source_message = next((message for message in revision.messages if message.message_id == source_id), None)
    if source_message is None:
        raise ValueError("Series source message must be part of the revision")
    source_url = f"{inbox_url.rstrip('/')}/{quote(source_id, safe='@')}/"
    legacy_body = f"Source: {source_url}\n\nRevision: v{revision.revision}\nPatches: {revision.expected_patch_count}"
    sender = " ".join(part for part in (source_message.from_name, source_message.from_address) if part) or "unknown"
    body = with_tool_attribution(f"{legacy_body}\n\nFrom (unverified):\n\n{quote_untrusted_text(sender)}", actor.login)

    lock = ledger.worker_lock() if publisher.write_enabled and ledger is not None else nullcontext()
    with lock, tempfile.TemporaryDirectory(prefix="lore-gitea-series-") as temp_dir:
        if ledger is not None:
            destination = publisher.api.base_url + publisher.repository_path
            previous = ledger.publication(destination, revision.series_key, revision.revision)
            if not previous or previous.status != "complete":
                ledger.assert_message_ancestry_available(revision.messages)
        box = mailbox.mbox(Path(temp_dir) / "series.mbox")
        try:
            for message in revision.messages:
                box.add(message.raw_message)
            box.flush()
        finally:
            box.close()
        assembly = assemble_local_series(Path(temp_dir) / "series.mbox")
        with prepare_patch_mailbox(repository, base_ref, assembly.patch_mailbox) as prepared:
            branch_arguments = {
                "target_branch": target_branch,
                "series_key": revision.series_key,
                "revision": revision.revision,
                "expected_patch_count": revision.expected_patch_count,
                "credentials": credentials,
            }
            branch = ensure_revision_branch(prepared, remote, **branch_arguments)
            preview_publisher = GiteaPullPublisher(publisher.api, publisher.owner, publisher.repo)
            pull_arguments = {
                "series_key": revision.series_key,
                "revision": revision.revision,
                "target_branch": target_branch,
                "title": revision.title,
                "body": body,
                "labels": labels,
                "revision_policy": revision_policy,
            }
            pull = preview_publisher.ensure_pull_request(branch=branch, **pull_arguments)
            plan = SeriesPlan(
                revision.series_key, revision.revision, source_id,
                tuple(message.sha256 for message in revision.messages),
                publisher.api.base_url, publisher.owner, publisher.repo, remote, target_branch,
                prepared.result.base_commit, branch.branch, branch.head_commit,
                revision.title, body, labels, actor.login, actor.user_id,
                credentials.username if credentials else None, revision_policy,
            )
            digest = plan.approval_digest()
            if not publisher.write_enabled:
                return SeriesSyncResult(plan, digest, branch, pull, applied=False, attribution_status=_attribution_status(pull, body))
            if not hmac.compare_digest(approved_digest or "", digest):
                raise ValueError("Approval digest does not match the current publication plan")
            if ledger is None:
                raise ValueError("Publishing requires a durable ledger")
            for message in revision.messages:
                ledger.record_parsed_message(message)
            ledger.assert_unconflicted(tuple(message.message_id for message in revision.messages if message.message_id))
            if revision not in ledger._discover_related_series(source_id):
                raise ValueError("Stored series membership changed; rediscover and review the publication plan")
            ledger.record_series_revision(revision)
            destination = publisher.api.base_url + publisher.repository_path
            key = (destination, revision.series_key, revision.revision)
            recorded_publication = ledger.publication(*key)
            if recorded_publication and recorded_publication.status == "complete":
                old_plan = replace(plan, body=legacy_body)
                if (
                    recorded_publication.approval_digest not in (
                        digest, plan.approval_digest(legacy=True), old_plan.approval_digest(), old_plan.approval_digest(legacy=True),
                    )
                    or recorded_publication.base_commit != prepared.result.base_commit
                    or recorded_publication.revision_branch != branch.branch
                    or recorded_publication.head_commit != branch.head_commit
                    or pull.planned or branch.planned
                    or pull.remote_object.get("number") != recorded_publication.pull_index
                ):
                    raise PublicationConflict("Recorded publication intent, pull request, or revision branch changed")
                return SeriesSyncResult(plan, digest, branch, pull, applied=True, attribution_status=_attribution_status(pull, body))
            publication = ledger.begin_publication(
                *key, digest, prepared.result.base_commit, branch.branch, branch.head_commit,
            )
            if publication.status in ("pr_pending", "uncertain"):
                if not pull.planned and not branch.planned:
                    ledger.transition_publication(
                        *key, expected=(publication.status,), status="complete",
                        pull_index=pull.remote_object["number"],
                    )
                    return SeriesSyncResult(plan, digest, branch, pull, applied=True, attribution_status=_attribution_status(pull, body))
                if not retry_uncertain:
                    raise PublicationUncertain(
                        "Earlier PR creation is unconfirmed; inspect Gitea before explicitly retrying"
                    )
            branch = ensure_revision_branch(prepared, remote, write_enabled=True, **branch_arguments)
            if not pull.planned:
                ledger.transition_publication(
                    *key, expected=(publication.status,), status="complete",
                    pull_index=pull.remote_object["number"],
                )
                return SeriesSyncResult(plan, digest, branch, pull, applied=True, attribution_status=_attribution_status(pull, body))
            if publication.status in ("prepared", "branch_ready"):
                ledger.transition_publication(
                    *key, expected=(publication.status,), status="branch_ready",
                )
                pending_from = "branch_ready"
            else:
                pending_from = publication.status
            ledger.transition_publication(*key, expected=(pending_from,), status="pr_pending")
            try:
                pull = publisher.ensure_pull_request(branch=branch, **pull_arguments)
            except Exception as error:
                ledger.transition_publication(
                    *key, expected=("pr_pending",), status="uncertain",
                    last_error=f"{type(error).__name__}: {error}",
                )
                raise
            ledger.transition_publication(
                *key, expected=("pr_pending",), status="complete",
                pull_index=pull.remote_object["number"],
            )
            return SeriesSyncResult(plan, digest, branch, pull, applied=True, attribution_status=_attribution_status(pull, body))