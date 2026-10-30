# SPDX-License-Identifier: GPL-2.0-or-later

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

from lore_review_companion.b4_adapter import assemble_local_series
from lore_review_companion.git_branches import GitHttpCredentials, RevisionBranchResult, RevisionRefsResult, ensure_revision_branch, ensure_revision_refs
from lore_review_companion.forge_api import authenticated_user
from lore_review_companion.forge_comments import DeliveryResult, quote_untrusted_text, with_tool_attribution
from lore_review_companion.forge_pulls import ForgePullPublisher
from lore_review_companion.forge_imports import ForgeImportClient, ImportDeliveryResult
from lore_review_companion.ledger import Ledger, PublicationConflict
from lore_review_companion.preflight import PatchCommit, prepare_patch_mailbox
from lore_review_companion.series import SeriesRevision, discover_series


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


@dataclass(frozen=True)
class ImportedSeriesPlan:
    series_key: str
    revision: int
    source_message_id: str
    source_sha256: tuple[str, ...]
    api_url: str
    owner: str
    repository: str
    git_remote: str
    target_branch: str
    base_branch: str
    head_branch: str
    base_commit: str
    head_commit: str
    patch_commits: tuple[PatchCommit, ...]
    repository_id: int
    actor: str
    actor_id: int
    git_username: str
    source_url: str
    request: dict
    retry_uncertain: bool

    def approval_digest(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ImportedSeriesResult:
    plan: ImportedSeriesPlan
    approval_digest: str
    refs: RevisionRefsResult
    pull_request: ImportDeliveryResult
    applied: bool


def sync_imported_series_revision(
    revision: SeriesRevision, repository: Path, base_ref: str, remote: str, client: ForgeImportClient, *,
    target_branch: str, ledger: Ledger, credentials: GitHttpCredentials,
    approved_digest: str | None = None, retry_uncertain: bool = False,
) -> ImportedSeriesResult:
    if not revision.complete or revision.conflicts or revision.expected_patch_count is None:
        raise ValueError("A complete unambiguous series revision is required")
    if revision not in ledger.discover_related_series(revision.cover_message_id or revision.messages[0].message_id):
        raise ValueError("Series membership must be verified from the retained mail ledger")
    if bool(client.write_enabled) != bool(approved_digest):
        raise ValueError("Imported publication requires paired write opt-in and approval digest")
    _validate_destination(client, remote)
    capabilities = client.capabilities()
    if "pull" not in capabilities["operations"]:
        raise ValueError("This repository does not support participant-authored PR imports")
    source_id = revision.cover_message_id or revision.messages[0].message_id
    source = next(message for message in revision.messages if message.message_id == source_id)
    if not source_id or not source.from_address:
        raise ValueError("The series source must retain its Message-ID and author email")
    ledger.assert_unconflicted(tuple(message.message_id for message in revision.messages if message.message_id))
    ledger.assert_message_ancestry_available(revision.messages)
    destination = client.api.base_url + client.repository_path
    lock = ledger.worker_lock() if client.write_enabled else nullcontext()
    with lock, tempfile.TemporaryDirectory(prefix="lore-import-series-") as temporary:
        mailbox_path = Path(temporary) / "series.mbox"
        box = mailbox.mbox(mailbox_path)
        try:
            for message in revision.messages:
                box.add(message.raw_message)
            box.flush()
        finally:
            box.close()
        assembly = assemble_local_series(mailbox_path, revision=revision.revision)
        with prepare_patch_mailbox(repository, base_ref, assembly.patch_mailbox) as prepared:
            patches = sorted((message for message in revision.messages if message.kind == "patch"), key=lambda message: message.patch_index or 0)
            patch_commits = prepared.patch_commits(tuple(message.message_id for message in patches))
            refs_arguments = {"target_branch": target_branch, "series_key": revision.series_key,
                              "revision": revision.revision, "expected_patch_count": revision.expected_patch_count,
                              "credentials": credentials}
            refs = ensure_revision_refs(prepared, remote, **refs_arguments)
            operation_key = hashlib.sha256(f"{revision.series_key}\0{revision.revision}\0{source_id}\0imported-series-v1".encode()).hexdigest()
            estimate = source.time_estimate
            body = f"Revision: v{revision.revision}\n\n" + quote_untrusted_text(source.body_text)
            previous = ledger.list_publications(destination=destination, series_key=revision.series_key, status="complete", limit=1000)
            if previous.next_cursor is not None:
                raise PublicationConflict("Earlier revision links exceed the reviewed publication limit")
            for entry in previous.publications:
                prior = entry.publication
                if prior.revision >= revision.revision:
                    continue
                site = client.api.base_url.removesuffix("/").removesuffix("/api/v1")
                body += f"\n\nEarlier revision: [v{prior.revision}]({site}/{quote(client.owner, safe='')}/{quote(client.repo, safe='')}/pulls/{prior.pull_index})"
            request = {
                "operation_key": operation_key, "message_id": source_id, "message_sha256": source.sha256,
                "address": source.from_address, "name": source.from_name or "", "body": body,
                "source_time": estimate.timestamp if estimate else None,
                "source_time_basis": "unverified" if estimate else "unknown",
                "title": f"[v{revision.revision}] {revision.title}",
                "head_branch": refs.head.branch, "base_branch": refs.base.branch,
                "head_commit": refs.head.head_commit, "base_commit": refs.base.head_commit,
            }
            plan = ImportedSeriesPlan(
                revision.series_key, revision.revision, source_id, tuple(message.sha256 for message in revision.messages),
                client.api.base_url, client.owner, client.repo, remote, target_branch, refs.base.branch, refs.head.branch,
                prepared.result.base_commit, prepared.result.head_commit, patch_commits, capabilities["repository_id"],
                capabilities["importer_login"], capabilities["importer_id"], credentials.username, client.source_url, request, retry_uncertain,
            )
            digest = plan.approval_digest()
            if not client.write_enabled:
                return ImportedSeriesResult(plan, digest, refs, ImportDeliveryResult({"path": f"{client.path}/pulls", **request}, planned=True), False)
            if not hmac.compare_digest(digest, approved_digest or ""):
                raise ValueError("Approval digest does not match the current imported series plan")
            ledger.assert_unconflicted(tuple(message.message_id for message in revision.messages if message.message_id))
            ledger.record_series_revision(revision)
            key = destination, revision.series_key, revision.revision
            intent_digest = replace(plan, retry_uncertain=False).approval_digest()
            publication = ledger.begin_publication(*key, intent_digest, plan.base_commit, plan.head_branch, plan.head_commit)
            if publication.status in ("pr_pending", "uncertain") and not retry_uncertain:
                raise PublicationUncertain("Earlier PR acceptance is unconfirmed; review same-operation retry before proceeding")
            current_capabilities = client.capabilities()
            if any(current_capabilities[field] != capabilities[field] for field in ("repository_id", "importer_id", "importer_login", "source_url")):
                raise PublicationConflict("Import authorization identity changed before ref publication")
            if "pull" not in current_capabilities["operations"]:
                raise PublicationConflict("PR import capability was revoked before ref publication")
            refs = ensure_revision_refs(prepared, remote, write_enabled=True, **refs_arguments)
            if publication.status == "complete":
                result = client.import_pull(request)
                if result.remote_object["pull_index"] != publication.pull_index:
                    raise PublicationConflict("Imported PR index differs from the durable publication mapping")
                return ImportedSeriesResult(plan, digest, refs, result, True)
            ledger.transition_publication(*key, expected=(publication.status,), status="pr_pending")
            try:
                result = client.import_pull(request)
            except Exception as error:
                ledger.transition_publication(*key, expected=("pr_pending",), status="uncertain", last_error=f"{type(error).__name__}: {error}")
                raise
            ledger.transition_publication(*key, expected=("pr_pending",), status="complete", pull_index=result.remote_object["pull_index"])
            return ImportedSeriesResult(plan, digest, refs, result, True)


def _attribution_status(pull: DeliveryResult, body: str) -> str:
    if pull.planned:
        return "planned"
    remote_body = pull.remote_object.get("body")
    return "present" if isinstance(remote_body, str) and body in remote_body else "existing_pr_unmodified"


def _validate_destination(publisher: ForgePullPublisher, remote: str) -> None:
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
    publisher: ForgePullPublisher,
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
        assembly = assemble_local_series(Path(temp_dir) / "series.mbox", revision=revision.revision)
        with prepare_patch_mailbox(repository, base_ref, assembly.patch_mailbox) as prepared:
            branch_arguments = {
                "target_branch": target_branch,
                "series_key": revision.series_key,
                "revision": revision.revision,
                "expected_patch_count": revision.expected_patch_count,
                "credentials": credentials,
            }
            branch = ensure_revision_branch(prepared, remote, **branch_arguments)
            preview_publisher = ForgePullPublisher(publisher.api, publisher.owner, publisher.repo)
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