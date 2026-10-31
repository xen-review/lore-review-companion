# SPDX-License-Identifier: GPL-2.0-or-later

"""Operator commands for the local lore review companion."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

from lore_review_companion.b4_adapter import (
    B4AssemblyError,
    B4ReviewError,
    inspect_b4_runtime,
    read_local_review_drafts,
)
from lore_review_companion.git_branches import (
    GitHttpCredentials,
    RevisionBranchError,
    UpstreamEvidence,
    inspect_upstream_history,
    plan_staging_sync,
)
from lore_review_companion.forge_api import (
    ForgeAPI,
    ForgeAPIError,
    authenticated_user,
    read_private_token,
)
from lore_review_companion.forge_comments import (
    DeliveryUncertainError,
    ForgeCommentPublisher,
)
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.forge_pulls import (
    ForgePullPublisher,
    PullRequestConflict,
    StagingWorkerBusy,
    inspect_review_staging,
    sync_review_staging,
)
from lore_review_companion.ingest import (
    EpochArchive,
    backfill_epoch_round,
    backfill_public_inbox,
    check_epoch_health,
    plan_archive_discontinuity_resolution,
    resolve_archive_discontinuity,
    sync_epoch_round,
    sync_public_inbox,
    watch_epoch_rounds,
)
from lore_review_companion.ledger import (
    ArchiveCheckpointConflict,
    ArchiveDeletion,
    ArchiveDeletionPresence,
    ArchiveDiscontinuity,
    ArchiveDiscontinuityResolution,
    ArchiveSafetyBlocked,
    DeliveryStateConflict,
    Ledger,
    MessageConflictError,
    NNTPIncident,
    PublicationConflict,
    WorkerBusyError,
)
from lore_review_companion.mail_parser import parse_mbox
from lore_review_companion.coordinator import plan_imported_sync
from lore_review_companion.import_policy import read_import_policy
from lore_review_companion.import_worker import run_import_worker_round
from lore_review_companion.nntp_source import (
    DEFAULT_GROUP,
    DEFAULT_HOST,
    DEFAULT_INBOX,
    DEFAULT_LOOKBACK_HOURS,
    MAX_ARTICLE_BYTES,
    MAX_BATCH_BYTES,
    MAX_DISCOVERY_BYTES,
    MIN_FREE_BYTES,
    NNTPSourceError,
    discover_nntp,
    discover_nntp_numbers,
    discover_nntp_thread,
    extend_nntp_window,
    fetch_nntp,
    inspect_nntp_window,
    nntp_source,
)
from lore_review_companion.outbox import (
    CommentBatchSummary,
    CommentDeliveryPlan,
    deliver_approved_comments,
    migrate_legacy_deliveries,
    plan_comment_delivery,
    plan_legacy_migration,
    deliver_approved_imports,
    plan_import_delivery,
    queue_imported_conversation_recreation,
)
from lore_review_companion.preflight import GitPreflightError
from lore_review_companion.public_inbox import (
    MAX_MANIFEST_COMPRESSED,
    PublicInboxError,
    fetch_public_inbox_manifest,
    inspect_public_inbox_deletion,
    parse_public_inbox_manifest,
    public_inbox_epochs,
)
from lore_review_companion.review_queue import (
    queue_archive_replies,
    queue_b4_drafts,
    queue_imported_replies,
    queue_thread_replies,
)
from lore_review_companion.series import (
    change_id_candidates,
    discover_series,
    has_invalid_change_id_claims,
)
from lore_review_companion.series_sync import (
    PublicationUncertain,
    sync_imported_series_revision,
    sync_series_revision,
)


@dataclass(frozen=True)
class CommentPreview:
    plan: CommentDeliveryPlan
    approval_digest: str


@dataclass(frozen=True)
class RetentionPreview:
    plan: object
    approval_digest: str


@dataclass(frozen=True)
class MigrationPreview:
    plan: object
    approval_digest: str


@dataclass(frozen=True)
class RevisionLinkPreview:
    plan: object
    approval_digest: str


@dataclass(frozen=True)
class OutboxListing:
    deliveries: tuple[object, ...]
    next_after: int | None


@dataclass(frozen=True)
class UpstreamInspection:
    destination: str
    pull_index: int
    series_key: str
    revision: int
    evidence: UpstreamEvidence


@dataclass(frozen=True)
class ArchiveSafetyStatus:
    deletions: tuple[ArchiveDeletion, ...]
    discontinuities: tuple[ArchiveDiscontinuity, ...]
    resolutions: tuple[ArchiveDiscontinuityResolution, ...]
    nntp_incidents: tuple[NNTPIncident, ...]


@dataclass(frozen=True)
class ArchiveDeletionDetails:
    commit_id: str
    blob_id: str
    message_id: str
    size: int
    local_presence: ArchiveDeletionPresence


@dataclass(frozen=True)
class ArchiveResolutionPreview:
    plan: object
    approval_digest: str


@dataclass(frozen=True)
class StagingSyncPreview:
    plan: object
    approval_digest: str


@dataclass(frozen=True)
class DiscoveredRevision:
    series_key: str
    revision: int
    title: str
    complete: bool
    conflicts: tuple[str, ...]
    message_ids: tuple[str, ...]
    change_id_candidates: tuple[str, ...]
    invalid_change_id_claims: bool


@dataclass(frozen=True)
class SeriesDiscovery:
    revisions: tuple[DiscoveredRevision, ...]


def main(argv: list[str] | None = None) -> int:
    """Parse and execute a bounded companion command."""
    parser = argparse.ArgumentParser(
        prog="lore-review-companion",
        description="Local lore-to-Forgejo review operations",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    runtime = commands.add_parser(
        "check-b4-runtime",
        help="Verify reviewed b4/liblore source pins without retrieving mail",
    )
    runtime.add_argument(
        "--python",
        default=sys.executable,
        help="Interpreter used for assembly and legacy parsing",
    )
    epochs = commands.add_parser(
        "discover-epochs",
        help="List manifest or operator-specified public-inbox epochs",
    )
    epochs.add_argument("--inbox-url", default="https://lore.kernel.org/xen-devel/")
    epoch_source = epochs.add_mutually_exclusive_group()
    epoch_source.add_argument(
        "--manifest", type=Path, help="Use a local manifest.js.gz"
    )
    epoch_source.add_argument(
        "--epoch", type=int, action="append", help="Specify an epoch number explicitly"
    )
    nntp = commands.add_parser(
        "discover-nntp",
        help="Queue recent lore Message-IDs over TLS without fetching mail bodies",
    )
    nntp.add_argument("--ledger", type=Path, required=True)
    nntp.add_argument("--host", default=DEFAULT_HOST)
    nntp.add_argument("--group", default=DEFAULT_GROUP)
    nntp.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    nntp.add_argument("--max-discovery-bytes", type=int, default=MAX_DISCOVERY_BYTES)
    nntp_extend = commands.add_parser(
        "extend-nntp-window",
        help="Explicitly backfill and widen an NNTP bootstrap window",
    )
    nntp_extend.add_argument("--ledger", type=Path, required=True)
    nntp_extend.add_argument("--host", default=DEFAULT_HOST)
    nntp_extend.add_argument("--group", default=DEFAULT_GROUP)
    nntp_extend.add_argument("--lookback-hours", type=int, required=True)
    nntp_extend.add_argument(
        "--max-discovery-bytes", type=int, default=MAX_DISCOVERY_BYTES
    )
    nntp_numbers = commands.add_parser(
        "discover-nntp-numbers",
        help="Queue a bounded range of new NNTP article numbers",
    )
    nntp_numbers.add_argument("--ledger", type=Path, required=True)
    nntp_numbers.add_argument("--host", default=DEFAULT_HOST)
    nntp_numbers.add_argument("--group", default=DEFAULT_GROUP)
    nntp_numbers.add_argument("--limit", type=int, default=128)
    nntp_numbers.add_argument(
        "--max-discovery-bytes", type=int, default=MAX_DISCOVERY_BYTES
    )
    nntp_thread = commands.add_parser(
        "discover-nntp-thread",
        help="Queue candidate messages from a bounded public-inbox thread export",
    )
    nntp_thread.add_argument("--ledger", type=Path, required=True)
    nntp_thread.add_argument("--message-id", required=True)
    nntp_thread.add_argument("--inbox-url", default=DEFAULT_INBOX)
    nntp_thread.add_argument("--host", default=DEFAULT_HOST)
    nntp_thread.add_argument("--group", default=DEFAULT_GROUP)
    nntp_thread.add_argument(
        "--max-compressed-bytes", type=int, default=2 * 1024 * 1024
    )
    nntp_thread.add_argument(
        "--max-uncompressed-bytes", type=int, default=8 * 1024 * 1024
    )
    nntp_thread.add_argument(
        "--from-index-time",
        type=int,
        help="Inclusive indexed-time bound in Unix seconds, not verified receipt time",
    )
    nntp_thread.add_argument(
        "--through-index-time",
        type=int,
        help="Inclusive indexed-time bound in Unix seconds, not verified receipt time",
    )
    nntp_queue = commands.add_parser(
        "list-nntp-queue",
        help="Inspect NNTP queue counts and pending Message-IDs without writes",
    )
    nntp_queue.add_argument("--ledger", type=Path, required=True)
    nntp_queue.add_argument("--host", default=DEFAULT_HOST)
    nntp_queue.add_argument("--group", default=DEFAULT_GROUP)
    nntp_queue.add_argument("--limit", type=int, default=100)
    nntp_queue.add_argument("--after", type=int, default=0)
    nntp_repair = commands.add_parser(
        "repair-nntp-parents",
        help="Rescan acknowledged NNTP messages for missing parent queue entries",
    )
    nntp_repair.add_argument("--ledger", type=Path, required=True)
    nntp_repair.add_argument("--host", default=DEFAULT_HOST)
    nntp_repair.add_argument("--group", default=DEFAULT_GROUP)
    nntp_repair.add_argument("--limit", type=int, default=100)
    nntp_repair.add_argument("--after", type=int, default=0)
    nntp_window = commands.add_parser(
        "inspect-nntp-window",
        help="Compare an unverified message-time estimate with the frozen bootstrap window",
    )
    nntp_window.add_argument("--ledger", type=Path, required=True)
    nntp_window.add_argument("--message-id", required=True)
    nntp_window.add_argument("--host", default=DEFAULT_HOST)
    nntp_window.add_argument("--group", default=DEFAULT_GROUP)
    nntp_searches = commands.add_parser(
        "list-nntp-thread-searches",
        help="Inspect recorded candidate thread-search ranges",
    )
    nntp_searches.add_argument("--ledger", type=Path, required=True)
    nntp_searches.add_argument("--message-id", required=True)
    nntp_searches.add_argument("--host", default=DEFAULT_HOST)
    nntp_searches.add_argument("--group", default=DEFAULT_GROUP)
    nntp_searches.add_argument("--after", type=int, default=0)
    nntp_searches.add_argument("--limit", type=int, default=100)
    nntp_fetch = commands.add_parser(
        "fetch-nntp", help="Fetch a bounded batch of queued lore articles over TLS"
    )
    nntp_fetch.add_argument("--ledger", type=Path, required=True)
    nntp_fetch.add_argument("--host", default=DEFAULT_HOST)
    nntp_fetch.add_argument("--group", default=DEFAULT_GROUP)
    nntp_fetch.add_argument("--limit", type=int, default=32)
    nntp_selection = nntp_fetch.add_mutually_exclusive_group()
    nntp_selection.add_argument(
        "--message-id", help="Queue and fetch only this Message-ID"
    )
    nntp_selection.add_argument(
        "--after", type=int, default=0, help="Fetch pending items after a queue cursor"
    )
    nntp_fetch.add_argument("--max-article-bytes", type=int, default=MAX_ARTICLE_BYTES)
    nntp_fetch.add_argument("--max-batch-bytes", type=int, default=MAX_BATCH_BYTES)
    nntp_fetch.add_argument("--min-free-bytes", type=int, default=MIN_FREE_BYTES)
    nntp_fetch.add_argument(
        "--timeout-retries",
        type=int,
        default=0,
        help="Retry timed-out articles on a new connection (maximum 3 per batch)",
    )
    sync = commands.add_parser(
        "sync-archive", help="Ingest one bounded public-inbox Git batch"
    )
    sync.add_argument("--archive", type=Path, required=True)
    sync.add_argument("--ledger", type=Path, required=True)
    sync.add_argument("--limit", type=int, default=128)
    sync.add_argument("--fetch", action="store_true")
    sync.add_argument("--allow-incomplete-bootstrap", action="store_true")
    backfill = commands.add_parser(
        "backfill-archive", help="Ingest one bounded older archive batch"
    )
    backfill.add_argument("--archive", type=Path, required=True)
    backfill.add_argument("--ledger", type=Path, required=True)
    backfill.add_argument("--limit", type=int, default=128)
    backfill.add_argument("--fetch", action="store_true")
    backfill.add_argument(
        "--deepen", type=int, help="Fetch at most this many older commits"
    )
    for name in ("sync-epochs", "backfill-epochs", "watch-epochs", "check-epochs"):
        help_text = {
            "sync-epochs": "Process one bounded forward archive round",
            "backfill-epochs": "Process one bounded backfill archive round",
            "watch-epochs": "Poll configured archive epochs in bounded cycles",
            "check-epochs": "Check local archive coverage and ledger health without writes",
        }[name]
        epoch_round = commands.add_parser(name, help=help_text)
        epoch_round.add_argument(
            "--epoch", action="append", required=True, metavar="NUMBER=PATH"
        )
        epoch_round.add_argument("--ledger", type=Path, required=True)
        if name != "check-epochs":
            epoch_round.add_argument("--limit", type=int, default=128)
            epoch_round.add_argument("--fetch", action="store_true")
        else:
            epoch_round.add_argument(
                "--ref",
                default="HEAD",
                help="Local ref to compare with each checkpoint",
            )
        if name in ("sync-epochs", "watch-epochs"):
            epoch_round.add_argument(
                "--allow-incomplete-bootstrap", action="store_true"
            )
        if name == "watch-epochs":
            epoch_round.add_argument("--backfill", action="store_true")
            epoch_round.add_argument("--interval", type=float, default=60)
            epoch_round.add_argument(
                "--once", action="store_true", help="Run exactly one polling cycle"
            )
    listing = commands.add_parser(
        "list-messages", help="Inspect a bounded page of ingested message metadata"
    )
    listing.add_argument("--ledger", type=Path, required=True)
    listing.add_argument("--kind", choices=("patch", "cover", "reply", "other"))
    listing.add_argument("--after", type=int, default=0)
    listing.add_argument("--limit", type=int, default=100)
    status = commands.add_parser(
        "status", help="Summarize local archive, quarantine, and delivery state"
    )
    status.add_argument("--ledger", type=Path, required=True)
    upstream = commands.add_parser(
        "inspect-upstream",
        help="Inspect recorded revision ancestry in a local upstream ref",
    )
    upstream.add_argument("--ledger", type=Path, required=True)
    upstream.add_argument("--repository", type=Path, required=True)
    upstream.add_argument("--upstream-ref", default="HEAD")
    upstream.add_argument("--api-url", required=True)
    upstream.add_argument("--owner", required=True)
    upstream.add_argument("--repo", required=True)
    upstream.add_argument("--pull-index", type=int, required=True)
    upstream.add_argument(
        "--patch-equivalence",
        action="store_true",
        help="Compare individual revision patches with bounded local upstream history",
    )
    upstream.add_argument("--patch-scan-limit", type=int, default=1000)
    outbox = commands.add_parser(
        "list-outbox", help="Inspect a bounded page of undelivered outbox entries"
    )
    outbox.add_argument("--ledger", type=Path, required=True)
    outbox.add_argument("--destination")
    outbox.add_argument("--event-type", choices=("ISSUE_COMMENT", "INLINE_COMMENT"))
    outbox.add_argument("--after", type=int, default=0)
    outbox.add_argument("--limit", type=int, default=100)
    publications = commands.add_parser(
        "list-publications", help="Inspect a bounded page of publication mappings"
    )
    publications.add_argument("--ledger", type=Path, required=True)
    publications.add_argument("--series-key")
    publications.add_argument("--destination")
    publications.add_argument(
        "--status",
        choices=("prepared", "branch_ready", "pr_pending", "uncertain", "complete"),
    )
    publications.add_argument("--after", type=int, default=0)
    publications.add_argument("--limit", type=int, default=100)
    retention = commands.add_parser(
        "prune-unmatched", help="Preview or approve retention pruning of unkeyed mail"
    )
    retention.add_argument("--ledger", type=Path, required=True)
    retention.add_argument(
        "--before", required=True, help="ISO 8601 cutoff with timezone"
    )
    retention.add_argument(
        "--actor", required=True, help="Operator-supplied audit identity"
    )
    retention.add_argument("--reason", required=True)
    retention.add_argument("--limit", type=int, default=100)
    retention.add_argument("--write", action="store_true")
    retention.add_argument(
        "--approve", help="Approval digest from the reviewed retention plan"
    )
    links = commands.add_parser(
        "index-links", help="Index one bounded batch of stored message references"
    )
    links.add_argument("--ledger", type=Path, required=True)
    links.add_argument("--limit", type=int, default=500)
    discovery = commands.add_parser(
        "discover-series", help="Discover stored series related to a patch or cover"
    )
    discovery.add_argument("--ledger", type=Path, required=True)
    discovery.add_argument("--message-id", required=True)
    discovery.add_argument(
        "--limit", type=int, default=1000, help="Maximum message links per local query"
    )
    change_search = commands.add_parser(
        "search-change-id",
        help="Scan a bounded local page for unverified change-ID claims",
    )
    change_search.add_argument("--ledger", type=Path, required=True)
    change_search.add_argument("--change-id", required=True)
    change_search.add_argument("--after", type=int, default=0)
    change_search.add_argument("--limit", type=int, default=100)
    links_list = commands.add_parser(
        "list-revision-links", help="Inspect audited revision associations"
    )
    links_list.add_argument("--ledger", type=Path, required=True)
    links_list.add_argument("--after", type=int, default=0)
    links_list.add_argument("--limit", type=int, default=100)
    revision_link = commands.add_parser(
        "link-revision", help="Preview or approve an independent revision association"
    )
    revision_link.add_argument("--ledger", type=Path, required=True)
    revision_link.add_argument("--series-key", required=True)
    revision_link.add_argument("--canonical-revision", type=int, required=True)
    revision_link.add_argument("--candidate-seed", required=True)
    revision_link.add_argument("--candidate-revision", type=int, required=True)
    revision_link.add_argument(
        "--actor", required=True, help="Operator-supplied audit identity"
    )
    revision_link.add_argument("--reason", required=True)
    revision_link.add_argument(
        "--allow-unmatched-change-id",
        action="store_true",
        help="Review an association without matching change-ID claims",
    )
    revision_link.add_argument("--write", action="store_true")
    revision_link.add_argument(
        "--approve", help="Approval digest from the reviewed link plan"
    )
    safety = commands.add_parser(
        "archive-safety", help="Show archive incidents blocking publication"
    )
    safety.add_argument("--ledger", type=Path, required=True)
    deletion = commands.add_parser(
        "inspect-archive-deletion",
        help="Inspect the Message-ID of a recorded deletion without printing its content",
    )
    deletion.add_argument("--ledger", type=Path, required=True)
    deletion.add_argument("--archive", type=Path, required=True)
    deletion.add_argument("--commit", required=True)
    deletion.add_argument(
        "--link-limit",
        type=int,
        default=1000,
        help="Maximum indexed descendant links to inspect",
    )
    resolve = commands.add_parser(
        "resolve-discontinuity",
        help="Preview or approve resolution of proven missing archive history",
    )
    resolve.add_argument("--ledger", type=Path, required=True)
    resolve.add_argument("--archive", type=Path, required=True)
    resolve.add_argument("--checkpoint", required=True)
    resolve.add_argument("--incident-head", required=True)
    resolve.add_argument(
        "--ref",
        default="HEAD",
        help="Local HEAD or named branch ref used for ingestion",
    )
    resolve.add_argument(
        "--actor", required=True, help="Operator-supplied audit identity"
    )
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--write", action="store_true")
    resolve.add_argument(
        "--approve", help="Approval digest from the reviewed resolution plan"
    )
    drafts = commands.add_parser(
        "preview-b4-drafts", help="Read one reviewer's local b4 draft snapshot"
    )
    drafts.add_argument("--repository", type=Path, required=True)
    drafts.add_argument("--branch", required=True)
    drafts.add_argument("--reviewer-email", required=True)
    drafts.add_argument("--b4-python", required=True)
    identity = commands.add_parser(
        "map-identity", help="Map an email address to a verified Gitea account ID"
    )
    identity.add_argument("--ledger", type=Path, required=True)
    identity.add_argument("--api-url", required=True)
    identity.add_argument("--email", required=True)
    identity.add_argument("--username", required=True)
    identity.add_argument("--reason", required=True)
    identity.add_argument("--replace", action="store_true")
    identity.add_argument("--token-env", default="LORE_FORGE_TOKEN")
    unmap = commands.add_parser(
        "unmap-identity", help="Remove an email-to-account mapping"
    )
    unmap.add_argument("--ledger", type=Path, required=True)
    unmap.add_argument("--api-url", required=True)
    unmap.add_argument("--email", required=True)
    unmap.add_argument("--reason", required=True)
    unmap.add_argument("--token-env", default="LORE_FORGE_TOKEN")
    access = commands.add_parser(
        "check-review-access",
        help="Inspect the authenticated account and reported repository push permission",
    )
    access.add_argument("--api-url", required=True)
    access.add_argument("--owner", required=True)
    access.add_argument("--repo", required=True)
    access.add_argument("--token-env", default="LORE_FORGE_TOKEN")
    staging = commands.add_parser(
        "inspect-review-staging",
        help="Compare source and review-fork branch tips without updating either",
    )
    staging.add_argument("--api-url", required=True)
    staging.add_argument("--source-owner", required=True)
    staging.add_argument("--source-repo", required=True)
    staging.add_argument("--review-owner", required=True)
    staging.add_argument("--review-repo", required=True)
    staging.add_argument("--branch", default="staging")
    staging.add_argument(
        "--token-env", help="Optional environment variable for private repository reads"
    )
    staging_plan = commands.add_parser(
        "plan-staging-sync",
        help="Preview a locally verified fast-forward without updating either repository",
    )
    staging_plan.add_argument("--source-repository", type=Path, required=True)
    staging_plan.add_argument("--remote", required=True)
    staging_plan.add_argument("--branch", default="staging")
    staging_plan.add_argument(
        "--token-env", help="Optional environment variable for private Git reads"
    )
    staging_plan.add_argument(
        "--git-username", help="Git username for the selected token"
    )
    staging_sync = commands.add_parser(
        "sync-review-staging",
        help="Synchronize a review branch using a verified fast-forward",
    )
    staging_sync.add_argument("--ledger", type=Path, required=True)
    staging_sync.add_argument("--api-url", required=True)
    staging_sync.add_argument("--source-owner", required=True)
    staging_sync.add_argument("--source-repo", required=True)
    staging_sync.add_argument("--review-owner", required=True)
    staging_sync.add_argument("--review-repo", required=True)
    staging_sync.add_argument("--source-repository", type=Path, required=True)
    staging_sync.add_argument("--remote", required=True)
    staging_sync.add_argument("--branch", default="staging")
    staging_sync.add_argument("--token-file", type=Path, required=True)
    staging_sync.add_argument(
        "--write", action="store_true", help="Explicitly enable a target branch update"
    )
    series = commands.add_parser(
        "sync-series", help="Preview or publish one complete mbox series"
    )
    series_source = series.add_mutually_exclusive_group(required=True)
    series_source.add_argument("--mbox", type=Path)
    series_source.add_argument(
        "--message-id", action="append", help="Select a stored source Message-ID"
    )
    series_source.add_argument(
        "--series-seed",
        help="Discover stored series from one patch or cover Message-ID",
    )
    series.add_argument("--repository", type=Path, required=True)
    series.add_argument("--base", required=True)
    series.add_argument("--remote", required=True)
    series.add_argument("--api-url", required=True)
    series.add_argument("--owner", required=True)
    series.add_argument("--repo", required=True)
    series.add_argument("--target-branch", required=True)
    series.add_argument("--series-key")
    series.add_argument("--revision", type=int)
    series.add_argument("--label", type=int, action="append", default=[])
    series.add_argument(
        "--revision-policy", choices=("block", "separate"), default="block"
    )
    series.add_argument("--inbox-url", default="https://lore.kernel.org/xen-devel/")
    series.add_argument("--git-username")
    series.add_argument("--token-env", default="LORE_FORGE_TOKEN")
    series.add_argument("--ledger", type=Path, help="Required for publication")
    series.add_argument("--write", action="store_true")
    series.add_argument("--approve", help="Approval digest from the reviewed dry run")
    series.add_argument("--retry-uncertain", action="store_true")
    queue = commands.add_parser(
        "queue-comments", help="Queue threaded mailbox replies for a published series"
    )
    queue_source = queue.add_mutually_exclusive_group(required=True)
    queue_source.add_argument("--mbox", type=Path)
    queue_source.add_argument(
        "--message-id", action="append", help="Select a stored reply Message-ID"
    )
    queue_source.add_argument(
        "--related",
        action="store_true",
        help="Discover stored replies from explicit ancestry",
    )
    queue.add_argument("--ledger", type=Path, required=True)
    queue.add_argument("--api-url", required=True)
    queue.add_argument("--owner", required=True)
    queue.add_argument("--repo", required=True)
    queue.add_argument("--pull-index", type=int, required=True)
    queue.add_argument("--series-key", required=True)
    queue.add_argument("--revision", type=int, required=True)
    queue.add_argument(
        "--b4-python",
        help="Use the legacy pinned b4 single-line parser instead of native quote blocks",
    )
    queue.add_argument(
        "--max-inline-lines",
        type=int,
        default=1,
        help="Explicitly supported Forgejo range limit; default single-line only",
    )
    queue.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum related replies queued per batch",
    )
    queue.add_argument("--cursor", help="Continue a previous --related reply batch")
    queue.add_argument("--inbox-url", default="https://lore.kernel.org/xen-devel/")
    queue.add_argument("--token-env", default="LORE_FORGE_TOKEN")
    draft_queue = commands.add_parser(
        "queue-b4-drafts", help="Queue local drafts for later approved comment delivery"
    )
    draft_queue.add_argument("--ledger", type=Path, required=True)
    draft_queue.add_argument("--api-url", required=True)
    draft_queue.add_argument("--owner", required=True)
    draft_queue.add_argument("--repo", required=True)
    draft_queue.add_argument("--pull-index", type=int, required=True)
    draft_queue.add_argument("--series-key", required=True)
    draft_queue.add_argument("--revision", type=int, required=True)
    draft_queue.add_argument("--repository", type=Path, required=True)
    draft_queue.add_argument("--branch", required=True)
    draft_queue.add_argument("--reviewer-email", required=True)
    draft_queue.add_argument("--b4-python", required=True)
    draft_queue.add_argument("--token-env", default="LORE_FORGE_TOKEN")
    deliver = commands.add_parser(
        "deliver-comments", help="Preview or approve one queued comment batch"
    )
    deliver.add_argument("--ledger", type=Path, required=True)
    deliver.add_argument("--api-url", required=True)
    deliver.add_argument("--owner", required=True)
    deliver.add_argument("--repo", required=True)
    deliver.add_argument("--pull-index", type=int, required=True)
    deliver.add_argument("--kind", choices=("conversation", "inline"), required=True)
    deliver.add_argument("--limit", type=int, default=100)
    deliver.add_argument("--token-env", default="LORE_FORGE_TOKEN")
    deliver.add_argument("--allow-inline-writes", action="store_true")
    deliver.add_argument(
        "--max-inline-lines",
        type=int,
        default=1,
        help="Explicitly supported Forgejo range limit; bound to approval",
    )
    deliver.add_argument("--retry-uncertain", action="store_true")
    deliver.add_argument("--write", action="store_true")
    deliver.add_argument(
        "--approve", help="Approval digest from the reviewed comment plan"
    )
    migration = commands.add_parser(
        "migrate-legacy-outbox",
        help="Preview or approve migration of untouched legacy replies",
    )
    migration.add_argument("--ledger", type=Path, required=True)
    migration.add_argument("--api-url", required=True)
    migration.add_argument("--owner", required=True)
    migration.add_argument("--repo", required=True)
    migration.add_argument("--pull-index", type=int, required=True)
    migration.add_argument("--reason", required=True)
    migration.add_argument("--limit", type=int, default=100)
    migration.add_argument(
        "--scoped-delivery-id",
        type=int,
        action="append",
        help="Select a server-scoped reply lacking thread provenance",
    )
    migration.add_argument("--token-env", default="LORE_FORGE_TOKEN")
    migration.add_argument("--write", action="store_true")
    migration.add_argument(
        "--approve", help="Approval digest from the reviewed migration plan"
    )
    import_queue = commands.add_parser(
        "queue-imports",
        help="Queue retained replies using exact imported patch comparisons",
    )
    recreate_import = commands.add_parser(
        "recreate-import",
        help="Queue a v6 conversation recreation without changing its original effect",
    )
    import_deliver = commands.add_parser(
        "deliver-imports",
        help="Preview or approve imported conversation/review/reply effects",
    )
    for command in (import_queue, recreate_import, import_deliver):
        command.add_argument("--ledger", type=Path, required=True)
        command.add_argument("--api-url", required=True)
        command.add_argument("--owner", required=True)
        command.add_argument("--repo", required=True)
        command.add_argument("--pull-index", type=int, required=True)
        command.add_argument(
            "--inbox-url", default="https://lore.kernel.org/xen-devel/"
        )
        command.add_argument("--token-file", type=Path, required=True)
    import_queue.add_argument("--message-id", action="append", required=True)
    import_queue.add_argument("--series-key", required=True)
    import_queue.add_argument("--revision", type=int, required=True)
    import_queue.add_argument("--repository", type=Path, required=True)
    import_queue.add_argument("--max-inline-lines", type=int, default=50)
    recreate_import.add_argument("--message-id", required=True)
    recreate_import.add_argument("--original-effect-key", required=True)
    recreate_import.add_argument("--reason", required=True)
    import_deliver.add_argument(
        "--kind", choices=("conversation", "review", "reply"), required=True
    )
    import_deliver.add_argument("--limit", type=int, default=100)
    import_deliver.add_argument(
        "--delivery-id",
        type=int,
        action="append",
        help="Select one exact pending delivery effect (repeat only for a reviewed batch)",
    )
    import_deliver.add_argument("--write", action="store_true")
    import_deliver.add_argument("--approve")
    import_deliver.add_argument("--retry-uncertain", action="store_true")
    imported_series = commands.add_parser(
        "sync-imported-series",
        help="Preview or publish one immutable participant-authored revision PR",
    )
    imported_series.add_argument("--ledger", type=Path, required=True)
    imported_series.add_argument("--api-url", required=True)
    imported_series.add_argument("--owner", required=True)
    imported_series.add_argument("--repo", required=True)
    imported_series.add_argument(
        "--inbox-url", default="https://lore.kernel.org/xen-devel/"
    )
    imported_series.add_argument("--series-seed", required=True)
    imported_series.add_argument("--series-key")
    imported_series.add_argument("--revision", type=int, required=True)
    imported_series.add_argument("--repository", type=Path, required=True)
    imported_series.add_argument("--base", required=True)
    imported_series.add_argument("--target-branch", required=True)
    imported_series.add_argument("--remote", required=True)
    imported_series.add_argument("--token-file", type=Path, required=True)
    imported_series.add_argument("--git-token-file", type=Path, required=True)
    imported_series.add_argument("--git-username", required=True)
    imported_series.add_argument("--write", action="store_true")
    imported_series.add_argument("--approve")
    imported_series.add_argument("--retry-uncertain", action="store_true")
    sync_plan = commands.add_parser(
        "plan-sync",
        help="Plan bounded source recovery, publication and replies without writes",
    )
    sync_plan.add_argument("--ledger", type=Path, required=True)
    sync_plan.add_argument("--api-url", required=True)
    sync_plan.add_argument("--owner", required=True)
    sync_plan.add_argument("--repo", required=True)
    sync_plan.add_argument("--inbox-url", default=DEFAULT_INBOX)
    sync_plan.add_argument("--series-seed", required=True)
    sync_plan.add_argument("--series-key")
    sync_plan.add_argument("--revision", type=int)
    sync_plan.add_argument("--repository", type=Path, required=True)
    sync_plan.add_argument("--base")
    sync_plan.add_argument("--target-branch", required=True)
    sync_plan.add_argument("--remote", required=True)
    sync_plan.add_argument("--token-file", type=Path, required=True)
    sync_plan.add_argument("--git-token-file", type=Path, required=True)
    sync_plan.add_argument("--git-username", required=True)
    sync_plan.add_argument("--host", default=DEFAULT_HOST)
    sync_plan.add_argument("--group", default=DEFAULT_GROUP)
    sync_plan.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    sync_plan.add_argument("--refresh-source", action="store_true")
    sync_plan.add_argument("--recover-thread", action="store_true")
    sync_plan.add_argument("--message-limit", type=int, default=100)
    sync_plan.add_argument("--revision-limit", type=int, default=8)
    sync_plan.add_argument("--max-inline-lines", type=int, default=50)
    import_worker = commands.add_parser(
        "run-import-worker",
        help="Run one bounded round under an explicit private import policy",
    )
    import_worker.add_argument("--policy-file", type=Path, required=True)
    import_worker.add_argument("--ledger", type=Path, required=True)
    import_worker.add_argument("--repository", type=Path, required=True)
    import_worker.add_argument("--state", type=Path, required=True)
    import_worker.add_argument("--token-file", type=Path, required=True)
    import_worker.add_argument("--git-token-file", type=Path, required=True)
    import_worker.add_argument("--no-refresh-source", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "run-import-worker":
            policy = read_import_policy(args.policy_file)
            ledger = Ledger(args.ledger)
            api = ForgeAPI(policy.api_url, token=read_private_token(args.token_file))
            credentials = GitHttpCredentials(
                policy.git_username, read_private_token(args.git_token_file)
            )
            result = run_import_worker_round(
                ledger,
                args.repository,
                args.policy_file,
                args.state,
                api,
                credentials,
                refresh_source=not args.no_refresh_source,
            )
        elif args.command in (
            "queue-imports",
            "recreate-import",
            "deliver-imports",
        ):
            write_enabled = args.command == "deliver-imports" and args.write
            if args.command == "deliver-imports" and bool(args.write) != bool(
                args.approve
            ):
                raise ValueError("--write and --approve must be supplied together")
            token = read_private_token(args.token_file)
            client = ForgeImportClient(
                ForgeAPI(args.api_url, token=token),
                args.owner,
                args.repo,
                source_url=args.inbox_url,
                write_enabled=write_enabled,
            )
            ledger = Ledger(
                args.ledger,
                read_only=args.command == "deliver-imports" and not write_enabled,
            )
            if args.command == "queue-imports":
                result = queue_imported_replies(
                    ledger,
                    client,
                    args.pull_index,
                    args.series_key,
                    args.revision,
                    args.repository,
                    ledger.messages_by_id(tuple(args.message_id)),
                    max_inline_lines=args.max_inline_lines,
                )
            elif args.command == "recreate-import":
                result = queue_imported_conversation_recreation(
                    ledger,
                    client,
                    args.pull_index,
                    args.message_id,
                    args.original_effect_key,
                    reason=args.reason,
                )
            else:
                event_type = {
                    "conversation": "IMPORT_CONVERSATION",
                    "review": "IMPORT_REVIEW",
                    "reply": "IMPORT_REPLY",
                }[args.kind]
                if write_enabled:
                    result = deliver_approved_imports(
                        ledger,
                        client,
                        args.pull_index,
                        event_type,
                        approved_digest=args.approve,
                        limit=args.limit,
                        retry_uncertain=args.retry_uncertain,
                        delivery_ids=tuple(args.delivery_id)
                        if args.delivery_id is not None
                        else None,
                    )
                else:
                    plan = plan_import_delivery(
                        ledger,
                        client,
                        args.pull_index,
                        event_type,
                        limit=args.limit,
                        retry_uncertain=args.retry_uncertain,
                        delivery_ids=tuple(args.delivery_id)
                        if args.delivery_id is not None
                        else None,
                    )
                    result = RetentionPreview(plan, plan.approval_digest())
        elif args.command == "plan-sync":
            ledger = Ledger(args.ledger, read_only=True)
            client = ForgeImportClient(
                ForgeAPI(args.api_url, token=read_private_token(args.token_file)),
                args.owner,
                args.repo,
                source_url=args.inbox_url,
            )
            result = plan_imported_sync(
                ledger,
                client,
                args.repository,
                args.remote,
                series_seed=args.series_seed,
                target_branch=args.target_branch,
                credentials=GitHttpCredentials(
                    args.git_username, read_private_token(args.git_token_file)
                ),
                base_ref=args.base,
                series_key=args.series_key,
                revision=args.revision,
                host=args.host,
                group=args.group,
                lookback_hours=args.lookback_hours,
                refresh_source=args.refresh_source,
                recover_thread=args.recover_thread,
                message_limit=args.message_limit,
                revision_limit=args.revision_limit,
                max_inline_lines=args.max_inline_lines,
            )
        elif args.command == "sync-imported-series":
            if bool(args.write) != bool(args.approve):
                raise ValueError("--write and --approve must be supplied together")
            ledger = Ledger(args.ledger, read_only=not args.write)
            revisions = tuple(
                revision
                for revision in ledger.discover_related_series(args.series_seed)
                if revision.revision == args.revision
                and (args.series_key is None or revision.series_key == args.series_key)
            )
            if len(revisions) != 1:
                raise ValueError("Select exactly one complete retained series revision")
            client = ForgeImportClient(
                ForgeAPI(args.api_url, token=read_private_token(args.token_file)),
                args.owner,
                args.repo,
                source_url=args.inbox_url,
                write_enabled=args.write,
            )
            credentials = GitHttpCredentials(
                args.git_username, read_private_token(args.git_token_file)
            )
            result = sync_imported_series_revision(
                revisions[0],
                args.repository,
                args.base,
                args.remote,
                client,
                target_branch=args.target_branch,
                ledger=ledger,
                credentials=credentials,
                approved_digest=args.approve,
                retry_uncertain=args.retry_uncertain,
            )
        elif args.command == "check-b4-runtime":
            print(json.dumps(inspect_b4_runtime(args.python), sort_keys=True))
            return 0
        elif args.command == "discover-epochs":
            if args.epoch is not None:
                result = public_inbox_epochs(args.inbox_url, tuple(args.epoch))
            else:
                if args.manifest is None:
                    compressed = fetch_public_inbox_manifest(args.inbox_url)
                else:
                    with args.manifest.open("rb") as manifest:
                        compressed = manifest.read(MAX_MANIFEST_COMPRESSED + 1)
                result = parse_public_inbox_manifest(args.inbox_url, compressed)
        elif args.command == "discover-nntp":
            result = discover_nntp(
                Ledger(args.ledger),
                host=args.host,
                group=args.group,
                hours=args.lookback_hours,
                response_bytes=args.max_discovery_bytes,
            )
        elif args.command == "extend-nntp-window":
            if not args.ledger.is_file():
                raise ValueError(
                    "An existing NNTP ledger is required to extend its window"
                )
            result = extend_nntp_window(
                Ledger(args.ledger),
                host=args.host,
                group=args.group,
                hours=args.lookback_hours,
                response_bytes=args.max_discovery_bytes,
            )
        elif args.command == "discover-nntp-numbers":
            if not args.ledger.is_file():
                raise ValueError(
                    "An existing NNTP ledger is required to scan article numbers"
                )
            result = discover_nntp_numbers(
                Ledger(args.ledger),
                host=args.host,
                group=args.group,
                limit=args.limit,
                response_bytes=args.max_discovery_bytes,
            )
        elif args.command == "discover-nntp-thread":
            if not args.ledger.is_file():
                raise ValueError(
                    "An existing NNTP ledger is required to discover a thread"
                )
            result = discover_nntp_thread(
                Ledger(args.ledger),
                args.message_id,
                inbox_url=args.inbox_url,
                host=args.host,
                group=args.group,
                compressed_limit=args.max_compressed_bytes,
                uncompressed_limit=args.max_uncompressed_bytes,
                from_index_time=args.from_index_time,
                through_index_time=args.through_index_time,
            )
        elif args.command == "list-nntp-queue":
            result = Ledger(args.ledger, read_only=True).nntp_queue_status(
                nntp_source(args.host, args.group),
                limit=args.limit,
                after=args.after,
            )
        elif args.command == "repair-nntp-parents":
            Ledger(args.ledger, read_only=True)
            result = Ledger(args.ledger).repair_nntp_parents(
                nntp_source(args.host, args.group),
                limit=args.limit,
                after=args.after,
            )
        elif args.command == "inspect-nntp-window":
            result = inspect_nntp_window(
                Ledger(args.ledger, read_only=True),
                args.message_id,
                host=args.host,
                group=args.group,
            )
        elif args.command == "list-nntp-thread-searches":
            result = Ledger(args.ledger, read_only=True).list_nntp_thread_searches(
                nntp_source(args.host, args.group),
                args.message_id,
                after=args.after,
                limit=args.limit,
            )
        elif args.command == "fetch-nntp":
            if not args.ledger.is_file():
                raise ValueError(
                    "An existing NNTP ledger is required to fetch articles"
                )
            result = fetch_nntp(
                Ledger(args.ledger),
                host=args.host,
                group=args.group,
                limit=args.limit,
                article_bytes=args.max_article_bytes,
                batch_bytes=args.max_batch_bytes,
                min_free_bytes=args.min_free_bytes,
                message_id=args.message_id,
                after=args.after,
                timeout_retries=args.timeout_retries,
            )
        elif args.command == "sync-archive":
            result = sync_public_inbox(
                args.archive,
                Ledger(args.ledger),
                limit=args.limit,
                fetch=args.fetch,
                allow_incomplete_bootstrap=args.allow_incomplete_bootstrap,
            )
        elif args.command == "backfill-archive":
            result = backfill_public_inbox(
                args.archive,
                Ledger(args.ledger),
                limit=args.limit,
                fetch=args.fetch,
                deepen=args.deepen,
            )
        elif args.command in (
            "sync-epochs",
            "backfill-epochs",
            "watch-epochs",
            "check-epochs",
        ):
            configured = []
            for entry in args.epoch:
                number, separator, path = entry.partition("=")
                if (
                    not separator
                    or not number.isascii()
                    or not number.isdecimal()
                    or not path
                ):
                    raise ValueError("Each --epoch must be NUMBER=PATH")
                configured.append(EpochArchive(int(number), Path(path)))
            ledger = Ledger(args.ledger, read_only=args.command == "check-epochs")
            if args.command == "sync-epochs":
                result = sync_epoch_round(
                    tuple(configured),
                    ledger,
                    limit=args.limit,
                    fetch=args.fetch,
                    allow_incomplete_bootstrap=args.allow_incomplete_bootstrap,
                )
            elif args.command == "backfill-epochs":
                result = backfill_epoch_round(
                    tuple(configured), ledger, limit=args.limit, fetch=args.fetch
                )
            elif args.command == "check-epochs":
                result = check_epoch_health(tuple(configured), ledger, ref=args.ref)
            else:
                for cycle in watch_epoch_rounds(
                    tuple(configured),
                    ledger,
                    limit=args.limit,
                    fetch=args.fetch,
                    allow_incomplete_bootstrap=args.allow_incomplete_bootstrap,
                    backfill=args.backfill,
                    interval=args.interval,
                    once=args.once,
                ):
                    print(
                        json.dumps(
                            {"command": args.command, **asdict(cycle)}, sort_keys=True
                        ),
                        flush=True,
                    )
                    rounds = (
                        (cycle.forward, cycle.backfill)
                        if cycle.backfill
                        else (cycle.forward,)
                    )
                    if any(
                        item.summary.messages.conflicts
                        for current in rounds
                        for item in current.results
                    ):
                        return 1
                return 0
        elif args.command == "list-messages":
            if not args.ledger.is_file():
                raise ValueError("An existing ledger is required to list messages")
            result = Ledger(args.ledger, read_only=True).list_messages(
                kind=args.kind, after=args.after, limit=args.limit
            )
        elif args.command == "status":
            if not args.ledger.is_file():
                raise ValueError("An existing ledger is required to inspect status")
            result = Ledger(args.ledger, read_only=True).status()
        elif args.command == "inspect-upstream":
            if not args.ledger.is_file() or args.pull_index < 1:
                raise ValueError(
                    "An existing ledger and positive pull request index are required"
                )
            publisher = ForgeCommentPublisher(
                ForgeAPI(args.api_url), args.owner, args.repo
            )
            destination = publisher.api.base_url + publisher.repository_path
            publications = Ledger(args.ledger, read_only=True).publications_for_pull(
                destination, args.pull_index
            )
            if len(publications) != 1:
                raise ValueError(
                    "Exactly one recorded completed pull request mapping is required"
                )
            publication = publications[0]
            evidence = inspect_upstream_history(
                args.repository,
                publication.head_commit,
                args.upstream_ref,
                base_commit=publication.base_commit if args.patch_equivalence else None,
                patch_scan_limit=args.patch_scan_limit,
            )
            result = UpstreamInspection(
                destination,
                args.pull_index,
                publication.series_key,
                publication.revision,
                evidence,
            )
        elif args.command == "list-outbox":
            if not args.ledger.is_file():
                raise ValueError(
                    "An existing ledger is required to list outbox entries"
                )
            deliveries = tuple(
                Ledger(args.ledger, read_only=True).pending(
                    args.limit,
                    destination=args.destination,
                    event_type=args.event_type,
                    after_id=args.after,
                )
            )
            result = OutboxListing(
                deliveries, deliveries[-1].id if len(deliveries) == args.limit else None
            )
        elif args.command == "list-publications":
            result = Ledger(args.ledger, read_only=True).list_publications(
                series_key=args.series_key,
                destination=args.destination,
                status=args.status,
                after=args.after,
                limit=args.limit,
            )
        elif args.command == "prune-unmatched":
            if not args.ledger.is_file():
                raise ValueError("An existing ledger is required for retention pruning")
            if bool(args.write) != bool(args.approve):
                raise ValueError("--write and --approve must be supplied together")
            ledger = Ledger(args.ledger, read_only=not args.write)
            if args.write:
                result = ledger.prune_unmatched(
                    args.before,
                    args.actor,
                    args.reason,
                    approved_digest=args.approve,
                    limit=args.limit,
                )
            else:
                plan = ledger.preview_unmatched_retention(
                    args.before, args.actor, args.reason, limit=args.limit
                )
                result = RetentionPreview(plan, plan.approval_digest())
        elif args.command == "index-links":
            if not args.ledger.is_file():
                raise ValueError("An existing ledger is required to index links")
            result = Ledger(args.ledger).index_message_links(limit=args.limit)
        elif args.command == "discover-series":
            if not args.ledger.is_file():
                raise ValueError("An existing ledger is required to discover series")
            revisions = Ledger(args.ledger, read_only=True).discover_related_series(
                args.message_id, limit=args.limit
            )
            result = SeriesDiscovery(
                tuple(
                    DiscoveredRevision(
                        revision.series_key,
                        revision.revision,
                        revision.title,
                        revision.complete,
                        revision.conflicts,
                        tuple(
                            message.message_id
                            for message in revision.messages
                            if message.message_id
                        ),
                        change_id_candidates(revision),
                        has_invalid_change_id_claims(revision),
                    )
                    for revision in revisions
                )
            )
        elif args.command == "search-change-id":
            if not args.ledger.is_file():
                raise ValueError(
                    "An existing ledger is required to search change-ID claims"
                )
            result = Ledger(args.ledger, read_only=True).search_change_id(
                args.change_id, after=args.after, limit=args.limit
            )
        elif args.command == "list-revision-links":
            if not args.ledger.is_file():
                raise ValueError(
                    "An existing ledger is required to inspect revision links"
                )
            result = Ledger(args.ledger, read_only=True).revision_links(
                after=args.after, limit=args.limit
            )
        elif args.command == "link-revision":
            if not args.ledger.is_file():
                raise ValueError("An existing ledger is required to link revisions")
            if bool(args.write) != bool(args.approve):
                raise ValueError("--write and --approve must be supplied together")
            ledger = Ledger(args.ledger, read_only=not args.write)
            arguments = (
                args.series_key,
                args.canonical_revision,
                args.candidate_seed,
                args.candidate_revision,
                args.actor,
                args.reason,
            )
            if args.write:
                result = ledger.link_revision(
                    *arguments,
                    approved_digest=args.approve,
                    allow_unmatched_change_id=args.allow_unmatched_change_id,
                )
            else:
                plan = ledger.plan_revision_link(
                    *arguments, allow_unmatched_change_id=args.allow_unmatched_change_id
                )
                result = RevisionLinkPreview(plan.audit_value(), plan.approval_digest())
        elif args.command == "archive-safety":
            if not args.ledger.is_file():
                raise ValueError(
                    "An existing ledger is required to inspect archive safety"
                )
            ledger = Ledger(args.ledger, read_only=True)
            result = ArchiveSafetyStatus(
                ledger.archive_deletions(),
                ledger.archive_discontinuities(),
                ledger.archive_discontinuity_resolutions(),
                ledger.status().nntp_incidents,
            )
        elif args.command == "inspect-archive-deletion":
            ledger = Ledger(args.ledger, read_only=True)
            source = str(args.archive.resolve(strict=True))
            if not ledger.has_archive_deletion(source, args.commit):
                raise ValueError(
                    "No recorded archive deletion matches this source and commit"
                )
            inspection = inspect_public_inbox_deletion(args.archive, args.commit)
            result = ArchiveDeletionDetails(
                **asdict(inspection),
                local_presence=ledger.archive_deletion_presence(
                    inspection.message_id, link_limit=args.link_limit
                ),
            )
        elif args.command == "resolve-discontinuity":
            if not args.ledger.is_file():
                raise ValueError(
                    "An existing ledger is required to resolve an archive discontinuity"
                )
            if bool(args.write) != bool(args.approve):
                raise ValueError("--write and --approve must be supplied together")
            ledger = Ledger(args.ledger, read_only=not args.write)
            arguments = (
                args.archive,
                ledger,
                args.checkpoint,
                args.incident_head,
                args.actor,
                args.reason,
            )
            if args.write:
                result = resolve_archive_discontinuity(
                    *arguments, ref=args.ref, approved_digest=args.approve
                )
            else:
                plan = plan_archive_discontinuity_resolution(*arguments, ref=args.ref)
                result = ArchiveResolutionPreview(plan, plan.approval_digest())
        elif args.command == "preview-b4-drafts":
            result = read_local_review_drafts(
                args.repository,
                args.branch,
                args.reviewer_email,
                python_executable=args.b4_python,
            )
        elif args.command in ("map-identity", "unmap-identity"):
            if not args.ledger.is_file():
                raise ValueError("An existing ledger is required for identity mappings")
            token = os.environ.get(args.token_env)
            if not token:
                raise ValueError(f"Set the token environment variable {args.token_env}")
            api = ForgeAPI(args.api_url, token=token)
            authenticated = authenticated_user(api)
            actor = f"{authenticated.login} (id {authenticated.user_id})"
            ledger = Ledger(args.ledger)
            if args.command == "map-identity":
                account = api.request_json(f"users/{quote(args.username, safe='')}")
                if (
                    not isinstance(account, dict)
                    or not isinstance(account.get("id"), int)
                    or isinstance(account.get("id"), bool)
                    or account["id"] < 1
                    or not isinstance(account.get("login"), str)
                    or account["login"].casefold() != args.username.casefold()
                ):
                    raise ValueError(
                        "Gitea did not return the requested account and ID"
                    )
                result = ledger.set_identity_mapping(
                    api.base_url,
                    args.email,
                    account["id"],
                    account["login"],
                    actor=actor,
                    reason=args.reason,
                    replace=args.replace,
                )
            else:
                result = ledger.remove_identity_mapping(
                    api.base_url, args.email, actor=actor, reason=args.reason
                )
        elif args.command == "check-review-access":
            token = os.environ.get(args.token_env)
            if not token:
                raise ValueError(f"Set the token environment variable {args.token_env}")
            api = ForgeAPI(args.api_url, token=token)
            result = ForgePullPublisher(api, args.owner, args.repo).inspect_access()
        elif args.command == "inspect-review-staging":
            token = os.environ.get(args.token_env) if args.token_env else None
            if args.token_env and not token:
                raise ValueError(f"Set the token environment variable {args.token_env}")
            result = inspect_review_staging(
                ForgeAPI(args.api_url, token=token),
                args.source_owner,
                args.source_repo,
                args.review_owner,
                args.review_repo,
                args.branch,
            )
        elif args.command == "plan-staging-sync":
            if bool(args.token_env) != bool(args.git_username):
                raise ValueError(
                    "--token-env and --git-username must be supplied together"
                )
            token = os.environ.get(args.token_env) if args.token_env else None
            if args.token_env and not token:
                raise ValueError(f"Set the token environment variable {args.token_env}")
            credentials = (
                GitHttpCredentials(args.git_username, token) if token else None
            )
            plan = plan_staging_sync(
                args.source_repository,
                args.remote,
                branch=args.branch,
                credentials=credentials,
            )
            result = StagingSyncPreview(plan, plan.approval_digest())
        elif args.command == "sync-review-staging":
            if not args.write:
                raise ValueError("Staging synchronization requires explicit --write")
            ledger = Ledger(args.ledger, read_only=True)
            token = read_private_token(args.token_file)
            result = sync_review_staging(
                ledger,
                ForgeAPI(args.api_url, token=token),
                args.source_owner,
                args.source_repo,
                args.review_owner,
                args.review_repo,
                args.source_repository,
                args.remote,
                branch=args.branch,
            )
        elif args.command == "sync-series":
            if bool(args.write) != bool(args.approve):
                raise ValueError("--write and --approve must be supplied together")
            if args.write and args.ledger is None:
                raise ValueError("--ledger is required for publication")
            if args.retry_uncertain and not args.write:
                raise ValueError("--retry-uncertain requires --write")
            token = os.environ.get(args.token_env)
            if not token:
                raise ValueError(f"Set the token environment variable {args.token_env}")
            if (args.message_id or args.series_seed) and (
                args.ledger is None or not args.ledger.is_file()
            ):
                raise ValueError(
                    "An existing --ledger is required to select stored Message-IDs"
                )
            ledger = (
                Ledger(args.ledger, read_only=not args.write) if args.ledger else None
            )
            if args.series_seed:
                revisions = ledger.discover_related_series(args.series_seed)
            else:
                messages = (
                    ledger.messages_by_id(tuple(args.message_id))
                    if args.message_id
                    else parse_mbox(args.mbox)
                )
                revisions = discover_series(messages)
            revisions = tuple(
                revision
                for revision in revisions
                if (args.series_key is None or revision.series_key == args.series_key)
                and (args.revision is None or revision.revision == args.revision)
            )
            if len(revisions) != 1:
                raise ValueError(
                    "Select exactly one revision using --series-key and --revision"
                )
            api = ForgeAPI(args.api_url, token=token)
            publisher = ForgePullPublisher(
                api, args.owner, args.repo, write_enabled=args.write
            )
            credentials = GitHttpCredentials(args.git_username or args.owner, token)
            result = sync_series_revision(
                revisions[0],
                args.repository,
                args.base,
                args.remote,
                publisher,
                target_branch=args.target_branch,
                inbox_url=args.inbox_url,
                labels=tuple(args.label),
                revision_policy=args.revision_policy,
                credentials=credentials,
                approved_digest=args.approve,
                ledger=ledger,
                retry_uncertain=args.retry_uncertain,
            )
        else:
            if not args.ledger.is_file():
                raise ValueError(
                    "An existing ledger is required for comment operations"
                )
            token = os.environ.get(args.token_env)
            if not token:
                raise ValueError(f"Set the token environment variable {args.token_env}")
            api = ForgeAPI(args.api_url, token=token)
            preview = (
                args.command in ("migrate-legacy-outbox", "deliver-comments")
                and not args.write
            )
            ledger = Ledger(args.ledger, read_only=preview)
            if args.command == "migrate-legacy-outbox":
                if bool(args.write) != bool(args.approve):
                    raise ValueError("--write and --approve must be supplied together")
                publisher = ForgeCommentPublisher(api, args.owner, args.repo)
                if args.write:
                    result = migrate_legacy_deliveries(
                        ledger,
                        publisher,
                        args.pull_index,
                        args.reason,
                        approved_digest=args.approve,
                        limit=args.limit,
                        scoped_delivery_ids=tuple(args.scoped_delivery_id)
                        if args.scoped_delivery_id is not None
                        else None,
                    )
                else:
                    plan = plan_legacy_migration(
                        ledger,
                        publisher,
                        args.pull_index,
                        args.reason,
                        limit=args.limit,
                        scoped_delivery_ids=tuple(args.scoped_delivery_id)
                        if args.scoped_delivery_id is not None
                        else None,
                    )
                    result = MigrationPreview(plan, plan.approval_digest())
            elif args.command == "queue-b4-drafts":
                publisher = ForgeCommentPublisher(api, args.owner, args.repo)
                result = queue_b4_drafts(
                    ledger,
                    publisher,
                    args.pull_index,
                    args.series_key,
                    args.revision,
                    args.repository,
                    args.branch,
                    args.reviewer_email,
                    b4_python=args.b4_python,
                )
            elif args.command == "queue-comments":
                publisher = ForgeCommentPublisher(
                    api, args.owner, args.repo, max_inline_lines=args.max_inline_lines
                )
                if args.related:
                    result = queue_archive_replies(
                        ledger,
                        publisher,
                        args.pull_index,
                        args.series_key,
                        args.revision,
                        b4_python=args.b4_python,
                        inbox_url=args.inbox_url,
                        limit=args.limit,
                        cursor=args.cursor,
                    )
                else:
                    if args.cursor:
                        raise ValueError(
                            "--cursor applies only to --related reply discovery"
                        )
                    messages = (
                        ledger.messages_by_id(tuple(args.message_id))
                        if args.message_id
                        else parse_mbox(args.mbox)
                    )
                    result = queue_thread_replies(
                        ledger,
                        publisher,
                        args.pull_index,
                        args.series_key,
                        args.revision,
                        messages,
                        b4_python=args.b4_python,
                        inbox_url=args.inbox_url,
                    )
            else:
                if bool(args.write) != bool(args.approve):
                    raise ValueError("--write and --approve must be supplied together")
                if args.allow_inline_writes and args.kind != "inline":
                    raise ValueError(
                        "--allow-inline-writes applies only to inline delivery"
                    )
                publisher = ForgeCommentPublisher(
                    api,
                    args.owner,
                    args.repo,
                    write_enabled=args.write,
                    allow_inline_writes=args.allow_inline_writes,
                    max_inline_lines=args.max_inline_lines,
                )
                event_type = (
                    "ISSUE_COMMENT" if args.kind == "conversation" else "INLINE_COMMENT"
                )
                if args.write:
                    result = deliver_approved_comments(
                        ledger,
                        publisher,
                        args.pull_index,
                        event_type,
                        limit=args.limit,
                        retry_uncertain=args.retry_uncertain,
                        approved_digest=args.approve,
                    )
                else:
                    plan = plan_comment_delivery(
                        ledger,
                        publisher,
                        args.pull_index,
                        event_type,
                        limit=args.limit,
                        retry_uncertain=args.retry_uncertain,
                    )
                    result = CommentPreview(plan, plan.approval_digest())
    except KeyboardInterrupt:
        return 130
    except StagingWorkerBusy as error:
        print(
            json.dumps(
                {
                    "command": args.command,
                    "error": str(error),
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return os.EX_TEMPFAIL
    except (
        ArchiveCheckpointConflict,
        ArchiveSafetyBlocked,
        B4AssemblyError,
        B4ReviewError,
        DeliveryStateConflict,
        DeliveryUncertainError,
        ForgeAPIError,
        GitPreflightError,
        NNTPSourceError,
        PublicInboxError,
        PullRequestConflict,
        RevisionBranchError,
        MessageConflictError,
        PublicationConflict,
        PublicationUncertain,
        WorkerBusyError,
        OSError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {
                    "command": args.command,
                    "error": str(error),
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(asdict(result), sort_keys=True))
    return (
        1
        if (isinstance(result, CommentBatchSummary) and result.failed)
        or (args.command == "check-epochs" and not result.healthy)
        else 0
    )


if __name__ == "__main__":
    sys.exit(main())
