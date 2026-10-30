# Lore Review Companion

Mirror mailing-list patch series and review discussions into a dedicated
Forgejo repository, starting with `xen-devel` on lore.kernel.org. The companion
is deterministic Python software; it does not require a runtime LLM.

**Status:** an independently packaged prototype, based on the previous bridge.
Scoped import authorization and non-login participant profiles are implemented
and tested in the separate Forgejo worktree. The first atomic conversation
import and lookup endpoints, non-voting review/range imports and threaded
replies are also tested. Participant-authored PR creation with pinned refs is
tested as well. Imported conversation/review/reply queue and approved outbox delivery
and publication are available as Python APIs and explicit CLI commands;
bounded read-only coordination is available; unattended policy/setup remains pending.
Native exact multiline
quote matching and versioned range delivery are implemented and tested locally;
whole-workflow acceptance remains pending. Read [PLAN.md](PLAN.md) and [LEDGER.md](LEDGER.md)
for scope, current results, and next steps.

## Install

Python 3.11 or later and Git are required. From this repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/lore-review-companion --help
```

The package installs the NNTP compatibility dependency on Python 3.13 or later.
b4 is an external runtime for patch assembly and the optional legacy quoted-
reply adapter; native reply matching does not need b4. It is not automatically
installed here. Install the reviewed source/dependency pins into the same
environment and check them before assembly:

```sh
.venv/bin/python -m pip install -r requirements-b4.txt
.venv/bin/python -m lore_review_companion check-b4-runtime
```

The adapter verifies versions and the combined Python source fingerprint of b4
and liblore, including module paths in the diagnostic output. Assembly uses the
verified interpreter rather than an unrelated `b4` on PATH. Pin updates require
an explicit adapter review and tests; a development version string alone is not
a compatible runtime. The optional legacy parser/draft reader uses the same pin.

The CLI also supports module execution:

```sh
.venv/bin/python -m lore_review_companion --help
```

## Safe Starting Point

List explicitly configured public-inbox epochs without fetching anything:

```sh
.venv/bin/lore-review-companion discover-epochs --epoch 0
```

Use command-specific `--help` before opening a ledger or configuring an API
destination. Individual commands can write local ingestion state; do not use
an operational database as a test fixture. Remote PR/comment publication is
separately gated by write flags and reviewed plan digests. Do not enable the
old live-test opt-ins against a real instance.

This repository is not ready for unattended deployment. It does not yet provide
the proposed scoped participant-import endpoints in stock Forgejo. Existing
publication paths still use an integration account; no administrator/Sudo
credential is required or recommended for the companion.

The new `ForgeImportClient` uses only the dedicated review-import API. It checks
the configured source and repository capabilities before writes, is read-only
unless explicitly enabled, and replays the identical operation after an
uncertain response. Repository-selected import tokens do not need `read:user`:
capabilities provide the integration identity. Legacy publication commands do
not use this client, and no existing effects are auto-migrated. Imported queue
and delivery use distinct version-3 effects with exact patch-parent diff
evidence and independent reviewed approval digests.

New `queue-comments` operations use native quote blocks unless `--b4-python`
explicitly selects legacy extraction. Range queueing and delivery require an
explicitly verified `--max-inline-lines` capability (default 1). The current
Forgejo checkout supports 50 selected lines. Plans bind this limit; changing it
requires approval again. Existing legacy inline effects are not automatically
duplicated or migrated to the new parser.

## Recent Discovery And Recovery

Discovery uses TLS NNTP and a durable Message-ID queue. Configure the initial
window explicitly, or retain the 148-hour default:

```sh
.venv/bin/lore-review-companion discover-nntp --ledger state.sqlite3 --lookback-hours 148
.venv/bin/lore-review-companion discover-nntp-numbers --ledger state.sqlite3 --limit 128
.venv/bin/lore-review-companion fetch-nntp --ledger state.sqlite3 --limit 128
```

The lookback is bounded to 1 through 8760 hours. It is fixed when that source's
checkpoint is first created; repeat discovery with the same setting to resume.
A changed duration is refused, not used to silently reset coverage or truncate
retained mail. Discovery uses server index time, which may depend on sender
Date, and does not establish verified receipt-time or complete thread coverage.

Fetched messages queue missing older parents recursively, outside the initial
window. `repair-nntp-parents` repairs dependencies in retained messages, and
`discover-nntp-thread --message-id MESSAGE_ID` queues bounded thread candidates
for subsequent group-verified NNTP fetch. `fetch-nntp --message-id MESSAGE_ID`
retries one pending item without letting an unavailable earlier item hide it.
Unavailable messages remain pending; they are not treated as source deletions.
Partial downloads never acknowledge the current item. Group renumbering,
validity changes, retention gaps and Message-ID conflicts halt discovery
instead of guessing. Byte/disk/batch limits remain explicit command options.

## Scoped Imports

The extension must be built and enabled in the separate Forgejo instance first.
Use an explicitly granted repository-selected `write:review-import` token in a
private token file. These commands do not use the ordinary comment APIs:

```sh
.venv/bin/lore-review-companion sync-imported-series \
  --ledger state.sqlite3 --api-url https://forge.example/api/v1/ \
  --owner review --repo xen --series-seed cover@example.test --revision 1 \
  --repository /path/to/xen --base BASE_COMMIT --target-branch staging \
  --remote https://forge.example/review/xen.git \
  --token-file /private/import-token --git-token-file /private/git-token \
  --git-username review-companion

.venv/bin/lore-review-companion queue-imports \
  --ledger state.sqlite3 --api-url https://forge.example/api/v1/ \
  --owner review --repo xen --pull-index 7 --token-file /private/import-token \
  --series-key 'lore:cover@example.test' --revision 1 \
  --repository /path/to/xen --message-id reply@example.test

.venv/bin/lore-review-companion deliver-imports \
  --ledger state.sqlite3 --api-url https://forge.example/api/v1/ \
  --owner review --repo xen --pull-index 7 --token-file /private/import-token \
  --kind review
```

Publication previews a separate PR per revision, with immutable revision base
and head branches. The Git token is separate from the import-only API token
and needs narrowly scoped code-push authority for this review repository.
Applying publication also requires paired `--write --approve DIGEST`; it never
rewrites an existing revision ref or PR. Newer PRs link to older revisions.

Queueing requires the recorded completed publication and pinned b4 runtime;
it verifies reassembly and exact historical patch diffs before adding effects.
The delivery command above is read-only and prints its plan/digest. After
reviewing it, supply paired `--write --approve DIGEST` for that exact batch.
Use `--kind conversation` to plan complete-message imports independently.
Use `--kind reply` for responses to existing imported inline comments. Queue
the parent email's review first and deliver it before queueing its responses.
A response must quote exactly one complete retained parent remark and have one
explicit parent Message-ID (`In-Reply-To`, or the final `References` entry).
Nested responses use the accepted parent reply's comment ID. Missing acceptance
or ambiguous text stays unresolved in the full-message conversation; it never
becomes an unrelated new review. Delivery binds the parent body hash, so a
locally edited parent is not silently reused.
Uncertain earlier writes need a newly reviewed `--retry-uncertain` plan.

The extension initializes imported creation timestamps from a supplied source
time, labeled `unverified` or `estimated`; unknown source time retains actual
creation time. Provenance keeps actual import/update times separately in UTC.
The scoped `PATCH review-imports/operations/OPERATION/source-time` endpoint
supports explicit corrections with a unique correction key, matching prior
payload hash/source time/basis and a reason. It appends an audit record and
changes creation chronology only, not content, author or existing edit time.
Same-key retries are idempotent; stale evidence, changed intent, unsupported
verification claims and revoked grants are refused. Original import replay
does not reset a correction or rewrite legacy content. Corrections are not
automatic or part of the companion worker; provenance UI is still pending.

## Coordinated Dry Run

`plan-sync` composes source recovery, exact preflight/publication and reply
delivery previews for one explicitly selected thread:

```sh
.venv/bin/lore-review-companion plan-sync \
  --ledger state.sqlite3 --api-url https://forge.example/api/v1/ \
  --owner review --repo xen --series-seed cover@example.test \
  --repository /path/to/xen --base BASE_COMMIT --target-branch staging \
  --remote https://forge.example/review/xen.git \
  --token-file /private/import-token --git-token-file /private/git-token \
  --git-username review-companion
```

The original ledger is opened read-only. SQLite's backup API creates a private
temporary copy for discovery, indexing and queue simulation, then removes it.
No refs, PRs or comments are written; this command accepts no write/approve
flags. Tokens are private files and never appear in plans. Individual approved
write commands remain separate; regenerate their current plans after retaining
real source/queue changes rather than treating this preview as blanket approval.

Use `--refresh-source --lookback-hours HOURS` to preview bounded recent NNTP
discovery, numeric continuation, parent repair and fetching in that copy.
`--recover-thread` previews older thread candidates and a bounded fetch.
Neither option retains newly fetched mail in the original ledger. Message and
revision limits default to 100 and 8 (maximum 1000 and 32).

Output distinguishes missing/incomplete/ambiguous series, unknown bases,
pending/uncertain publication, blocked preflight and ready publication/delivery.
Published revisions use their recorded base, not a moving configured base.
Reply plans require a confirmed publication and retained immutable refs; a new
PR preview does not invent an index to plan replies against.

## Develop And Test

With the package installed in the virtual environment:

```sh
env -u LORE_GITEA_ACCEPTANCE -u LORE_GITEA_ACCEPTANCE_DELETIONS \
  -u LORE_GITEA_ACCEPTANCE_B4_PYTHON \
  .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

For the inline-only delivery slice, use `-p test_forge_comments.py` instead.
The default suite uses synthetic mail and fake API transports. Three optional
tests require explicit opt-in: one disposable server and two real-b4 checks.
Do not copy private mail or operational ledgers into this repository.

Runtime modules live in `lore_review_companion/`; tests live in `tests/`.
Update the ledger with actual checks and limitations, then commit each verified
topic separately. The operator handles pushing; no automatic push is part of
the workflow.

## Compatibility And Provenance

Legacy `LORE_GITEA_*` environment variables, body markers and commit identities
remain where changing them could break existing reviewed plans or replay.
They do not imply general Gitea product certification. Compatibility changes
must be explicit and tested, not global search-and-replace of persisted data.

[ORIGIN.md](ORIGIN.md) records the source snapshot and license context, including
the outstanding distribution-license review. No b4 or Forgejo implementation
is vendored. Forgejo extensions are developed in a separate local fork, not
submitted as AI-authored upstream contributions.