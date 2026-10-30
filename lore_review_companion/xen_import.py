# SPDX-License-Identifier: GPL-2.0-or-later

"""Preview and explicitly approve one Xen lore revision and its discussion."""

from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path

from lore_review_companion.coordinator import plan_imported_sync
from lore_review_companion.forge_api import ForgeAPI, read_private_token
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.git_branches import GitHttpCredentials
from lore_review_companion.ledger import Ledger
from lore_review_companion.nntp_source import discover_nntp_thread, fetch_nntp
from lore_review_companion.outbox import deliver_approved_imports, plan_import_delivery
from lore_review_companion.review_queue import queue_imported_replies
from lore_review_companion.series_sync import sync_imported_series_revision


def read_deployment(config: Path) -> dict:
    descriptor = os.open(config / "import-policy.json", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 16384
        ):
            raise ValueError(
                "Deployment configuration must be a private regular file owned by this user"
            )
        deployment = json.load(stream)
    if deployment.get("source_url") != "https://lore.kernel.org/xen-devel/":
        raise ValueError("This script is for the Xen lore deployment")
    return deployment


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir", type=Path, default=Path.home() / ".config/lore-review-companion"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local/state/lore-review-companion",
    )
    parser.add_argument(
        "--seed",
        required=True,
        help="Retained cover/patch Message-ID, without angle brackets",
    )
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--limit", type=int, default=32)
    parser.add_argument(
        "--kind", choices=("conversation", "review", "reply"), default="conversation"
    )
    parser.add_argument(
        "--approve", help="Exact digest returned by the corresponding preview command"
    )
    parser.add_argument(
        "command",
        choices=("recover", "plan", "publish", "queue", "plan-delivery", "deliver"),
    )
    options = parser.parse_args(arguments)
    if not 1 <= options.limit <= 1000 or options.revision < 1:
        parser.error("Use a positive revision and a limit between 1 and 1000")
    if options.command in ("publish", "deliver") and not options.approve:
        parser.error("Remote writes require --approve with the exact reviewed digest")
    if options.approve and options.command not in ("publish", "deliver"):
        parser.error("--approve is only accepted for publish/deliver")
    try:
        deployment = read_deployment(options.config_dir)
        ledger = Ledger(
            options.state_dir / "mail.sqlite3",
            read_only=options.command in ("plan", "plan-delivery"),
        )
        if options.command == "recover":
            result = discover_nntp_thread(
                ledger,
                options.seed,
                inbox_url=deployment["source_url"],
                host=deployment["nntp_host"],
                group=deployment["nntp_group"],
            )
            print(json.dumps(asdict(result)))
            print(
                json.dumps(
                    asdict(
                        fetch_nntp(
                            ledger,
                            host=deployment["nntp_host"],
                            group=deployment["nntp_group"],
                            limit=options.limit,
                        )
                    )
                )
            )
            return 0
        api = ForgeAPI(
            deployment["api_url"],
            token=read_private_token(options.config_dir / "import-token"),
        )
        client = ForgeImportClient(
            api,
            deployment["owner"],
            deployment["repository"],
            source_url=deployment["source_url"],
            write_enabled=options.command in ("publish", "deliver"),
        )
        capabilities = client.capabilities()
        if any(
            capabilities.get(key) != deployment[key]
            for key in ("repository_id", "importer_id", "importer_login", "source_url")
        ):
            raise ValueError(
                "Current importer/repository/source differs from the deployment configuration"
            )
        credentials = GitHttpCredentials(
            deployment["git_username"],
            read_private_token(options.config_dir / "git-token"),
        )
        repository = options.state_dir / "xen.git"
        if options.command == "plan":
            preview = plan_imported_sync(
                ledger,
                client,
                repository,
                deployment["git_remote"],
                series_seed=options.seed,
                revision=options.revision,
                target_branch=deployment["target_branch"],
                credentials=credentials,
                base_ref=deployment["base_commit"],
                message_limit=options.limit,
            )
            print(json.dumps(asdict(preview), indent=2))
            return 0
        revisions = [
            item
            for item in ledger.discover_related_series(options.seed)
            if item.revision == options.revision
        ]
        if len(revisions) != 1:
            raise ValueError("Seed/revision must identify exactly one retained series")
        revision = revisions[0]
        destination = api.base_url + client.repository_path
        publication = ledger.publication(
            destination, revision.series_key, revision.revision
        )
        if options.command == "publish":
            result = sync_imported_series_revision(
                revision,
                repository,
                publication.base_commit if publication else deployment["base_commit"],
                deployment["git_remote"],
                client,
                target_branch=deployment["target_branch"],
                ledger=ledger,
                credentials=credentials,
                approved_digest=options.approve,
            )
            print(json.dumps(asdict(result), indent=2))
            return 0
        if (
            publication is None
            or publication.status != "complete"
            or publication.pull_index is None
            or publication.pull_index <= 0
        ):
            raise ValueError(
                "Publish and confirm this revision before queueing or delivering discussion"
            )
        members = tuple(
            message.message_id for message in revision.messages if message.message_id
        )
        replies = tuple(
            message
            for message in ledger._related_thread_messages(members)
            if message.kind == "reply"
        )
        if options.command == "queue":
            if len(replies) > options.limit:
                raise ValueError(
                    "Reply count exceeds --limit; choose an explicit bounded limit up to 1000"
                )
            queued = queue_imported_replies(
                ledger,
                client,
                publication.pull_index,
                revision.series_key,
                revision.revision,
                repository,
                replies,
                max_inline_lines=deployment["max_inline_lines"],
            )
            print(json.dumps(asdict(queued), indent=2))
            return 0
        kind = {
            "conversation": "IMPORT_CONVERSATION",
            "review": "IMPORT_REVIEW",
            "reply": "IMPORT_REPLY",
        }[options.kind]
        if options.command == "plan-delivery":
            plan = plan_import_delivery(
                ledger, client, publication.pull_index, kind, limit=options.limit
            )
            print(
                json.dumps(
                    {"approval_digest": plan.approval_digest(), "plan": asdict(plan)},
                    indent=2,
                )
            )
        else:
            outcome = deliver_approved_imports(
                ledger,
                client,
                publication.pull_index,
                kind,
                approved_digest=options.approve,
                limit=options.limit,
            )
            print(json.dumps(asdict(outcome)))
            return int(bool(outcome.failed))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        print(f"Import stopped: {type(error).__name__}: {error}")
        return 1
