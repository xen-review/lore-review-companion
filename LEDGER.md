# Implementation Ledger

Updated: 2026-10-30. Design and boundaries: [PLAN.md](PLAN.md).

Statuses: `TODO`, `IN PROGRESS`, `DONE`, `BLOCKED`, `DEFERRED`. `DONE` requires
recorded verification, not just code. Each completed companion topic gets a
commit; record its subject here and use Git history for the resulting hash.
No automatic pushes. Forgejo changes belong to the separate parent repository.

## Current Iteration

- DOC-01 `DONE`: the implementation plan and this living ledger form the first
  commit, before implementation import. The user supplied an empty repository.
- B01 `DONE`: source/history/tool baseline recorded below. The initial absence
  of b4 was resolved under C02 with an isolated, fingerprint-verified runtime.
- B03 `DONE`: unsafe inline delivery no longer becomes a conversation write.
  Focused publisher and neighboring queue tests passed.
- B04 `DONE`: neutral adapters, package layout, console/module entry points,
  install documentation and offline CI configuration are verified locally.
- C01 `DONE`: schema setup is transactional; legacy schema 18/21/22 payloads
  and approval digests are preserved, and new ranges use a version-2 envelope.
- C03 `DONE`: decoded mail now preserves quoted blocks and following remarks;
  exact range resolution (C04) is also verified. Versioned delivery is next.
- C05 `IN PROGRESS`: native quote-to-range queue, evidence preview, approval,
  delivery and replay are verified, including imported-author per-patch effects.
  Explicit publication/queue/delivery CLI commands and exact email-to-inline
  thread replies are verified; isolated whole-workflow acceptance remains. No runtime
  b4 parser is needed for native replies.
- C02 `DONE`: exact local b4/liblore sources are installed and fingerprinted;
  real offline assembly and read-only draft checks passed.
- F01 `DONE`: explicit import scopes, repository/source grants, authorization
  gates and transfer revocation are implemented in the Forgejo worktree and
  verified with model/scope tests and two isolated API integration tests.
- F02 `DONE`: repository/source-isolated participant profiles are created
  transactionally without account adoption. Shared login/session, reset,
  OAuth/linking, credentials, email changes, Sudo and admin edits are blocked
  and covered by affected-package tests and an isolated API case.
- F03 `DONE`: atomic participant-authored conversation imports and scoped
  operation lookup are tested on existing PRs. The companion has an opt-in
  import-only client with same-key replay, capability checks and no fallback;
  CLI/outbox wiring follows with F04 and S03.
- F04 `DONE`: participant-authored PRs, fresh COMMENT reviews and threaded
  replies are tested with pinned refs, exact historical patch-parent ranges,
  stable inline IDs and no draft consumption or notifications. Companion
  per-patch publication/outbox wiring remains under C05, S01 and S03.
- F05 `DONE`: validated original chronology initializes only new imported
  objects; actual import/update times remain separate in UTC. Scoped corrections
  are evidence-bound, idempotent and audited without changing bodies/authors/edits.
- S01 `DONE`: new imported publication uses paired immutable base/head refs and
  one participant-authored PR per revision by default. V2 links to V1 without
  rewriting V1. Protected Git/API credentials and uncertainty approvals are
  separate; the legacy lifecycle is unchanged.
- S02 `DONE`: configurable bounded NNTP lookback is exposed, preserving the
  148-hour default and durable fixed checkpoint. Existing numeric discovery,
  recursive older-parent recovery, selected retries and resource guards pass.
- S03 `DONE`: bounded `plan-sync` composes optional source refresh/recovery,
  pinned publication/preflight and independent reply delivery previews in a
  private SQLite snapshot. Original-ledger bytes and remote objects are unchanged.
- Historical sample publications remain background evidence only; the current
  baseline is 343 tests, with only the live-server acceptance test skipped
  when the pinned real-b4 opt-ins are enabled.

## Discovery

| ID | Status | Verified Result |
| --- | --- | --- |
| D01 | DONE | Read prior plan, decisions, ledgers and controlling Python code/tests; identified reusable ingestion/publication and outstanding safety work. |
| D02 | DONE | Inspected Forgejo authentication, permissions, user creation, multiline review model/API and rendering. Global Sudo is not suitably scoped. |
| D03 | DONE | Inspected local b4 CLI/review extraction and adapter. Private alpha helpers need an isolated pinned boundary. |
| D04 | DONE | Confirmed immutable PR per revision, deterministic matching, incoming-only scope, recent configurable discovery, managed profiles and standalone repository. |

Discovery reflects the preceding read-only session. Worktree cleanliness,
installed versions, historical commits and current tests are B01, not assumed.

## Baseline And Extraction

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| DOC-01 | DONE | - | PLAN and LEDGER are the first content; staged whitespace and both local document links checked. |
| B01 | DONE | DOC-01 | Current worktrees/history/tools inspected and narrow/full offline baseline passed; source is an untracked snapshot, b4 runtime absent. See execution record. |
| B02 | DONE | B01 | Unchanged Python/requirements snapshot imported with checksums, legal context, provenance and ignores. Original files untouched; imported offline suite passed. Final distribution-license review remains R01. |
| B03 | DONE | B02 | Removed inline fallback; tests prove stale/disabled/pending-review paths make zero POSTs and preserve unresolved effects. Separate conversation approval still works; 25 publisher and 31 queue tests passed. |
| B04 | DONE | B03 | Neutral adapters and installable package/CLI added with README and offline CI. Wheel installed and entry points tested outside checkout; 296 tests run, 3 skips. Legacy wire identities and token options retained; hosted CI matrix not yet run. |

## Deterministic Multiline Reviews

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| C01 | DONE | B04 | Atomic schema setup, rollback, read-only reopen and schema 18/21/22 payload preservation tested. Version-2 ranges preserve source/parser/time/comparison evidence; legacy approval digests unchanged. Future server import mappings belong to F03/F04. |
| C02 | DONE | B04 | Verified b4 0.17.dev0/liblore 0.10.1 combined source fingerprint; same-interpreter offline assembly pins revision, disables trailers/partial rerolls and checks ordered patch content/count. 12 focused tests pass including real local b4. |
| C03 | DONE | B04 | Added deterministic quoted-block extraction with depth/headers/source offsets, remarks and conservative signature/trailer boundaries. 13 mail-parser tests pass, including nested/trimmed and MIME/CRLF cases. Full message remains canonical. |
| C04 | DONE | C03 | Exact contiguous same-file/side range matching added separately from legacy Anchor. All 13 anchor tests pass; full context disambiguates repeated lines, while mixed/disjoint/headerless/renamed/ambiguous cases fail closed. |
| C05 | IN PROGRESS | C01, C04, F04 | Exact assembled patch mapping, version-3 imported conversation/review/thread-reply queue and digest-approved CLI delivery are tested. Exact parent quotes bind accepted comment IDs and body hashes, including nested replies; missing/ambiguous parents remain unresolved. Source/quote/diff identity is revalidated; changed pins/targets cause zero POSTs, response loss replays the same key, and legacy effects cannot silently change authorship. Queue 38/client 21 tests pass; isolated full workflow acceptance remains. |

## Forgejo Extension

These items modify the separate Forgejo fork. Record files, checks, and relevant
revision state here; do not stage them into a companion commit.

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| F01 | DONE | B01 | Opt-in import scopes (not implied by legacy all), source-bound repository grants, current token-reduced code/PR write checks and owner-bound/transfer revocation implemented. Scope/model tests and authorization/real-transfer API tests pass. Object/author checks follow with F03/F04 operations. |
| F02 | DONE | F01 | Atomic repository/source participant registry, deterministic collision-safe names, inactive/restricted/passwordless private-email profiles. Replay revalidates safety flags; shared authentication, sessions, OAuth/linking, reset, tokens/SSH/GPG, password/email changes, Sudo, activation/admin edits and inactive cleanup are guarded. Rollback, collision, affected-package and isolated API tests pass. |
| F03 | DONE | F01, F02 | Existing-PR conversation import and scoped operation lookup preserve mapped author, importer, source time/hash and atomic operation keys. Replay keeps IDs; changed payload/target/local content or reused Message-ID with different bytes/author conflicts. No notifications, mentions or cross-reference actions. Opt-in companion client rechecks capabilities, safely replays uncertain POSTs and never uses ordinary mutation APIs. |
| F04 | DONE | F03 | Participant-authored PRs retain pinned branches/commits, use CAS-only pull refs and reject duplicate revision-message claims. Fresh non-voting reviews/replies validate exact historical patch parents and old/new ranges without draft theft or notifications. Scoped clients preserve uncertain replay and validate returned IDs/pins. Real-Git API cases pass; per-patch companion wiring is C05/S01/S03. No ordinary before-commit API extension was necessary. |
| F05 | DONE | F03, F04 | New imported PRs, conversations, reviews/ranges and replies receive validated original creation chronology; actual import/update timestamps remain separate in UTC. Scoped original-importer corrections bind prior payload/time/basis, retain body/author/edit time and append idempotent reason/actor/time audits. All six isolated import API tests and affected model/service/migration packages pass; unknown/UTC/future/unsupported quality/stale evidence/revocation/replay cases are covered. |
| F06 | TODO | F03, F04, F05 | API and web render both participant and importer, lore link and source chronology/quality. Reuse existing UI; no forged identity verification or layout regressions. |

## Publication And Automation

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| S01 | DONE | B04, C02, F04 | `sync-imported-series` uses one PR and paired immutable base/head refs per revision, pinned source/patch maps and prior-revision links. V2 leaves V1 unchanged; source-thread checks retain late replies on their recorded revision. Repeated writes reconcile the same operation/refs. Tests cover upstream movement, two revisions and approved uncertain retry; full live workflow remains V01. |
| S02 | DONE | B04, C01 | `discover-nntp --lookback-hours` exposes a bounded 1-8760-hour initial window (148 default), fixed per durable source checkpoint. 42 NNTP tests cover restart/deduplication, older-parent recursion/repair, selected retries, unavailable items retained pending, bounded thread discovery and byte/disk/group-history guards. No verified receipt-time or full thread coverage is claimed. |
| S03 | DONE | C05, S01, S02, F04 | Bounded `plan-sync` previews discovery/recovery, exact publication/preflight and all three imported delivery kinds using a private SQLite backup. Original ledger and remote refs/content stay unchanged; no write flags or blanket approval. Tests cover fixed resource bounds, snapshot isolation, retained-mail/no-NNTP state, missing/incomplete/ambiguous/unknown-base/preflight/pending-publication blockers and independent child plans. |
| S04 | TODO | S03, F05, F06 | Explicit revocable repository policy and supervised unattended worker. Recheck scope/permissions/capabilities per write; bound Git credentials separately; incidents stop and restarts reconcile uncertain writes. No mail sending or blanket self-approval. |

## Acceptance And Handoff

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| V01 | TODO | C05, F06, S04 | Isolated Forgejo fixture covers multi-patch V1/V2, reviewers, late replies, old/new ranges, chronology, identity collisions, duplicate-ID conflicts and crash-after-accept retries. Correct objects exactly once, no mail/draft leakage/shared SSH changes. |
| V02 | TODO | V01 | Focused authorization/account-security regression and desktop/mobile browser checks. Run touched Python/Go suites, formatting/lint and Swagger generation. Record actual results and residual limitations. |
| R01 | TODO | V02 | Finish independent installation, API/auth/operations, migration/backup/recovery, sanitized service templates, licensing/provenance and CI. Complete tested topic commits and final status; operator handles pushes. |

## Verification Rules

- After each substantive edit, run the cheapest check that can falsify the
  change before reading or editing another slice. Repair the same slice first.
- Current focused check: `.venv/bin/python -m unittest discover -s tests -p test_forge_comments.py`.
- Related slices reuse `test_anchor`, `test_b4_adapter`, `test_review_queue`,
  `test_threads`, `test_series_sync`, `test_preflight`, `test_nntp_source` and
  `test_ledger`. Adapt paths after packaging, not before.
- Current full offline check: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
  with live acceptance variables unset. Historical flat-source checks below
  refer to the earlier layout. Distinguish skipped opt-in tests explicitly.
- Forgejo examples: `make 'test-sqlite#TestAPIPullReview'`,
  `make 'test-sqlite#TestPullRequestCommentPlacement'`, and targeted new import
  tests. Follow actual build prerequisites and isolated-root configuration.
- Run `make fmt` for Go and `make generate-swagger` for API changes; use the
  applicable touched-surface lint checks. Do not update dependencies unrelated
  to an implementation slice.
- Browser gate uses existing PR-review Playwright tests and focused assertions
  for imported authorship/provenance, range context, reload and responsive fit.
- Live tests always need a deliberately isolated fixture and separate opt-in.
  Do not post previously pending effects or migrate operational ledgers.

## Deferred Backlog

Full-history archive rollout; web-to-email drafts and sending; dual Gitea
certification; staging-arrival inference/notices; automatic close/merge;
destructive retractions and purge/backup policy; exceptional series-association
correction; original-instance SSH repair; broader production compliance.

These are not completed tasks or prerequisites silently added to this delivery.
Bring them into scope only through an explicit decision.

## Execution Record

### 2026-10-29: Documentation Bootstrap

- Confirmed the operator's cloned companion directory contains only `.git` and
  has no commits. Existing `origin` is operator-managed; do not push or alter it.
- Added the refined plan and task ledger as the first repository content.
- Validation: staged `git diff --check` passed; both linked documents exist.
- Commit subject: `docs: record implementation plan and task ledger`.

### 2026-10-29: B01 Baseline

- Documentation bootstrap commit: `ba35ee0`.
- Forgejo base: `cff8e8831a0695913dd0f383426a487baf899cd3`. Its pre-existing
  companion/b4 directories are untracked; no tracked changes were present.
- Earlier Gitea base: `178d33ed4dce734384caa61a04290b30ab955b21`, clean worktree.
  That checkout has no companion directory or tracked history for it. Import
  the operator-provided snapshot without claiming earlier commit preservation.
- Python `3.13.5`, Git `2.47.3`. `b4` is not on PATH and pipx reports no installed
  applications for this account. Real-b4 checks remain pending under C02.
- b4 source: `ce4c1158edb292caca0ce4a6087e5edecc1b0371`; liblore source:
  `6e8d825a12ae32b98e4da5348d3ade6fce12a895`. An existing untracked b4 prompt
  was left untouched. Checkout metadata is not an installed-runtime pin.
- Source aggregate SHA-256: `c9489494e4d239d01a38253345da030eb0f5bcefc3942db49fa97795378f6bea`,
  calculated with sorted shell globs for the legacy Python files and requirements
  including their legacy relative paths. B02 records individual file checksums.
- Narrow check: `python3 -m unittest discover -s contrib/lore-gitea-mirror -p test_gitea_comments.py`:
  24 tests passed.
- Full check: `python3 -m unittest discover -s contrib/lore-gitea-mirror -p 'test_*.py'`:
  295 run, 292 passed, 3 skipped in 18.798 seconds. Skips: one disposable-server
  test and two optional installed-b4 tests. All live opt-ins were unset and
  bytecode writes disabled for both checks. No operational state was opened.
- The source scan found no old instance paths, real example recipient, or
  GitHub token prefixes in Python files; only synthetic/configurable endpoints
  and environment-based credentials were observed. No private docs/state enter
  the source import.
- Commit subject: `docs: record verified companion baseline`.

### 2026-10-29: B02 Source Snapshot

- Imported only legacy Python sources/tests and requirements, plus the Gitea
  license as source context. [ORIGIN.md](ORIGIN.md) describes history limits,
  licensing questions, exclusions and compatibility boundaries.
- `SOURCE.sha256` records the individual unchanged source hashes at this import.
  Ignore rules exclude bytecode, environments, local mail and database state.
- Imported-tree check: `python3 -m unittest discover -s lore-review-companion -p 'test_*.py'`:
  295 run, 292 passed, 3 skipped in 20.235 seconds, with live opt-ins unset and
  bytecode disabled. This matches the baseline without parent-source imports.
- No operational docs, databases, mail, credentials, caches, services or nested
  repositories were imported. The original source directory is unchanged.
- Commit subject: `chore: import reviewed companion source snapshot`.

### 2026-10-29: B03 Inline-Only Delivery

- Source snapshot commit: `5707bd3`. Removed the catch-and-post fallback in
  `outbox._publish_comment`; safety failures now reach existing error handling.
- Extended the existing stale-head test and added approved-plan subtests for
  stale head, disabled inline writes, and an existing pending review. They
  assert zero POSTs, retained inline errors, and an untouched full-reply effect.
  A separately approved conversation plan can still deliver that full reply.
- Immediate check: 25 comment-publisher tests passed. Neighboring check: 31
  review-queue tests passed. Both used synthetic fixtures, live opt-ins unset,
  and bytecode disabled. Editor diagnostics reported no errors.
- Existing uncertain-write state handling is unchanged; failures remain visible
  for inspection rather than silently authorizing an alternate write.
- Commit subject: `fix: keep unsafe inline deliveries from changing write type`.

### 2026-10-29: B04 Adapter Naming

- B03 commit: `5250716`. Renamed the API and publisher modules/classes to
  `forge_api`, `forge_comments`, `forge_pulls`, `ForgeAPI`,
  `ForgeCommentPublisher` and `ForgePullPublisher`, including test references.
- The Python rename provider is unavailable, so exact mechanical identifier
  replacement and Git file renames were used. The first full check caught two
  cross-test helper imports; repaired those locally and reran the same check.
- Full offline result: 296 run, 293 passed, 3 opt-in skips in 19.123 seconds.
  The added test is B03's approved-inline regression; no tests were removed.
- Legacy wire markers, source-body wording, environment variables, serialized
  ledger data and the Git committer identity were not renamed in this slice.
- Commit subject: `refactor: use neutral forge adapter names`.

### 2026-10-29: B04 Standalone Package

- Neutral naming commit: `bfde1c2`. Runtime modules moved into
  `lore_review_companion/`, synthetic/opt-in tests into `tests/`. Imports and
  mock targets are package-qualified; cross-test helper imports remain local
  to the test suite. Fixed the omitted `threads` imports found by the first
  layout check and restored accidentally rewritten temporary filenames.
- Added setuptools metadata, console/module entry points, a setup/status
  README and read-only-permission offline CI for Python 3.11/3.12/3.13. The
  hosted matrix is configured, not executed in this session.
- Built and installed `lore-review-companion==0.1.0.dev0` as a wheel into an
  ignored `.venv`, including `standard-nntplib==3.13.0` for local Python 3.13.
  No packages were installed globally and b4 was not implicitly installed.
- Outside-checkout checks from `/tmp`, with PYTHONPATH unset: the installed
  console command returned the expected operator-specified epoch JSON, and
  `python -m lore_review_companion --help` returned the supported commands.
  These checks did not fetch mail, open an operational ledger or post content.
- Installed-package suite: 296 run, 293 passed, 3 opt-in skips in 19.115 seconds,
  with live opt-ins and PYTHONPATH unset and bytecode writes disabled. Earlier
  source-layout reruns also passed after local import/filename repairs.
- Optional live worker invocation now uses the package module, not a path to
  the removed flat CLI script. Its real-server acceptance remains skipped.
- Legacy environment variables remain supported through existing token options;
  wire markers, serialized bodies and preflight committer data are unchanged.
  Fresh scoped Forgejo imports and a final distribution license remain future
  tasks, explicitly noted in the README rather than claimed implemented.
- Commit subject: `build: package standalone lore review companion`.

### 2026-10-30: C01 Atomic Schema Setup

- Resumed at `e1e795e`; package outbox has no tracked changes. An untracked
  legacy test at repository root is operator-owned and left untouched.
- Schema initialization now begins an explicit SQLite transaction before the
  DDL script. Previously executescript could commit new tables even when later
  version validation failed. Tests prove failure rollback and future-version
  rejection without residual tables or a changed version.
- Synthetic schema 18/21/22 reopen tests retain exact pending deliveries,
  payloads and IDs, including read-only access. No database schema bump or
  legacy payload rewrite is needed for additive JSON contract work.
- Immediate check against current source: 43 ledger tests passed, with live
  opt-ins unset. Operational ledgers were not opened or migrated.
- Deployment supplied by operator: `https://review-forge.be.free.or.at`, source
  mirror `xen-project/xen` (8-hour upstream sync), review fork `xen-review/xen`.
  Fork ownership is available to the operator's integration identity, but the
  running companion will use scoped write access rather than Owners/admin.
- Commit subject: `fix: roll back failed ledger schema initialization`.

### 2026-10-30: C03 Quoted Block Extraction

- Added `QuotedRemark` and `extract_quoted_remarks` alongside MIME parsing.
  Unlike the private b4 single-line representation, they retain full quoted
  blocks, diff headers, depth and decoded-body line offsets for the subsequent
  remark. Multiple paragraphs/followups retain source order.
- Extraction does not select an anchor or lose the original full message.
  Preambles and ambiguous/non-diff material remain in the conversation; known
  review trailers and signature blocks are not turned into inline prose.
- Immediate check: all 13 mail-parser tests passed, including nested/trimmed
  markers, changed quote depth, multiple remarks and quoted-printable CRLF.
- Commit subject: `feat: preserve quoted review blocks and source evidence`.

### 2026-10-30: C04 Exact Range Matching

- Added `RangeAnchor` and `DiffIndex.resolve_quote` without changing the
  serialized legacy `Anchor` contract or the old single-line resolver.
- The full quoted sequence must match a unique contiguous range on one file,
  side and hunk. Default maximum is 50 lines; mixed sides, disjoint hunks,
  headerless context, renamed paths and repeated ambiguous sequences decline
  inline placement. Context-only quotes select the proposed side.
- Immediate check: all 13 anchor tests passed, including exact old/new ranges,
  complete-context disambiguation and explicit range-size limits.
- Operator confirmed the integration username is `review-companion` and
  authorized rebuilding/restarting the development Forgejo. Credentials are
  never recorded in source/docs/commands. Before a restart, identify the exact
  checkout/configuration because multiple gitea processes exist.
- Commit subject: `feat: resolve exact multiline review quote ranges`.

### 2026-10-30: C01/C05 Versioned Range Delivery

- Added explicit range capability limits to the existing publisher. Legacy
  single-line requests omit range fields; ranges send `extra_lines_count` and
  require an explicitly configured limit. Base and merge-base are rechecked
  alongside head before a new versioned write.
- Outbox version 2 requires a complete range/comparison and matching retained
  source hash/parser evidence. Unknown versions are rejected. Legacy payloads
  remain unchanged; their approval digest deliberately omits the new range
  capability field. Version-2 digests include it so capability changes require
  a fresh approval.
- Focused checks: 30 publisher tests and 31 legacy queue tests passed, with
  synthetic transports and live opt-ins disabled. Tests cover range replay,
  source-hash mismatch, stale base, limits and legacy digest compatibility.
- Commit subject: `feat: deliver versioned ranges with approval-bound evidence`.

### 2026-10-30: C01/C05 Native Range Queue Integration

- New reply queues default to deterministic native quote blocks. Explicit
  `--b4-python` retains the legacy single-line path. Both queueing and delivery
  expose `--max-inline-lines` with a conservative default of 1; deployments
  explicitly select the verified supported limit (50 on this Forgejo build).
- Each range retains parser version, complete quote/headers, source offsets,
  raw-mail hash, source URL/time quality and pinned PR comparison. Existing
  full-reply payloads/effect keys remain compatible. Native continuation
  cursors bind the range limit; legacy cursors retain their original shape.
- A message with legacy inline effects is not silently reprocessed under a
  new parser: a separate reviewed migration is required to prevent duplicate
  excerpts. New source-message effects are deterministic and replay-safe.
- Immediate/adjacent checks: 34 queue tests and 30 publisher tests passed. Full
  current-source offline suite: 317 run, 314 passed, 3 skips. Fixed two fixture
  connection resource warnings; all 43 ledger tests then passed with resource
  warnings promoted to errors. No operational ledger or API writes were used.
- The native path is independent of b4's review parser. b4 pinning is still
  required for patch assembly (C02), not for deterministic quote matching.
- Commit subject: `feat: queue native multiline reviews with replay-safe evidence`.

### 2026-10-30: C02 Pinned Offline b4 Runtime

- Installed reviewed local b4/liblore sources into the ignored `.venv`, not
  globally. `requirements-b4.txt` records exact source commits and observed
  dependency pins. Combined Python-code SHA-256 is
  `3b5a059e8544da3166d1a40fc68986110a55defe9c25ac128ac106ef4021679e`.
- Assembly and private legacy adapters verify the fingerprint and versions,
  not just a development version string. `check-b4-runtime` prints module
  paths/versions/hash without retrieving mail. Same interpreter executes b4
  with isolated config, closed stdin, offline mode, explicit revision, no
  collected trailers and no partial-reroll reconstruction.
- Input must contain one complete revision; output must retain expected patch
  count, order, author/title and diff content. The publication pipeline now
  explicitly passes its selected revision. No user checkout or draft refs are
  changed by assembly.
- Full offline suite with local real-b4 opt-in: 319 run, 318 passed, only the
  live-server test skipped. Fixed the new preflight CLI's dictionary output
  formatting and added coverage; immediate rerun: all 12 focused b4 tests pass
  and the actual runtime-preflight command succeeds. The real subprocess test
  explicitly imports current source rather than an older installed wheel.
- Commit subject: `feat: pin and validate offline b4 assembly runtime`.

### 2026-10-30: F01 Scoped Forgejo Import Authorization

- Forgejo changes remain in the separate parent worktree, not companion Git.
  Added `read:review-import`/`write:review-import`; legacy all/admin/repository
  tokens do not automatically acquire import capability. Repository-selected
  tokens may explicitly request it without general mutation scopes.
- Added source-inbox/repository/importer grants, idempotent same-setting grant
  creation, explicit conflict/revoke handling and an owner binding. Actual
  repository transfer deletes grants inside the transfer transaction.
- Added administration routes under `review-import-grants/{importer}` and
  capability inspection under `review-imports/capabilities`. Import routes
  authenticate as the companion, forbid Sudo, require a matching grant and
  current reduced write access to both code and PR units, and reject mirrors
  or archived repositories. No participant/content mutation exists yet.
- Tests pass for scope normalization, repository-token validation, grant
  persistence/replay/conflict/revoke, selected-vs-other public repository
  tokens, non-writers, changed ownership, Sudo, and actual transfer deletion.
  The two SQLite API cases passed in 2.524 seconds including fixture startup.
- `make generate-swagger`, `make fmt` and focused auth/authz/repo Go tests
  passed. Generated schema adds only the three new operations/types; generator
  warnings concern existing unrelated definitions. Live binary not restarted.
- Commit subject: `docs: record tested Forgejo import authorization contract`.

### 2026-10-30: F02 Managed Participant Isolation

- Changes remain uncommitted in the parent Forgejo worktree. Added `IsImported`
  and a unique repository/source/email participant registry with a migration.
  The source address remains an unverified claim, never a login credential;
  account email is private under `review-import.invalid` with mail disabled.
- Creation wraps the user and registry in one transaction, preserves local-part
  case while normalizing domain case, and never adopts occupied usernames or
  ordinary accounts. Reuse checks the persisted safety invariants and cannot
  silently promote a profile. Failed imports roll back both records.
- Blocked shared HTTP authentication, session/OAuth completion before key/team
  synchronization, reset token verification and reset mail, external linking
  and updates, password and email mutation, token/SSH/GPG issuance, ordinary
  activation/admin edits, and post-authentication Sudo. Inactive-user cleanup
  excludes these intentionally inactive profiles. Profile changes must use a
  future dedicated import operation rather than ordinary account editing.
- Full affected package checks pass for `models/user`, `models/auth`,
  `models/asymkey`, `services/user`, `routers/common` and `routers/web/auth`;
  `services/externalaccount` compiles and its guards are tested from auth tests.
  Regression cases include collisions, transaction rollback, domain-case
  replay, repository separation, unsafe reuse, successful upstream-auth
  rejection and mail-free recovery. One test import was corrected and rerun.
- `make 'test-sqlite#TestAPIReviewImport' TAGS='sqlite sqlite_unlock_notify'
  TEST_LOGGER=file`: all three API tests pass (3.226 seconds with fixture
  startup), including admin token/promotion/Sudo rejection and unchanged
  profile flags/email. `make fmt` and editor diagnostics passed. No live
  database/content writes or binary restart occurred.
- Commit subject: `docs: record tested managed participant isolation`.

### 2026-10-30: F03 Atomic Conversation Imports

- Parent Forgejo worktree adds `ReviewImport` and `ReviewImportMessage` tables
  with unique repository/source operation keys and Message-ID bindings. One
  transaction creates the managed participant, canonical comment and retained
  provenance. Raw-mail SHA-256 and author cannot change under a source ID.
- New import-only routes: POST `review-imports/pulls/{index}/comments` and GET
  `review-imports/operations/{operation}`. Both enforce F01 gates; normal repo
  scopes cannot import and read-only import scopes cannot write. PR target,
  evidence bounds/hash/email and source-time quality are validated. A changed
  payload or deleted/modified canonical comment is never silently overwritten.
- Import uses the canonical comment model without notification/mention
  delivery; a default-off skip-cross-reference option prevents imported text
  from creating reference actions. Tests cover no hooks/actions, original
  author vs importer, rollback of profile/comment/provenance, replay IDs,
  source conflicts, bad targets/evidence, lookup and grant revocation.
- Capability response now supplies importer login, avoiding a broad `read:user`
  scope for repository-selected import tokens. Added `ForgeImportClient` with
  source/capability validation, dry run by default, bounded identical-POST
  reconciliation and explicit uncertainty if revocation prevents confirmation.
  Lookup alone never declares a payload successfully delivered. Legacy
  publisher paths remain unchanged and are not switched without F04/S03.
- All affected Go model/service/auth packages passed. Four isolated SQLite API
  cases passed in 4.113 seconds including startup. Full Python suite: 324 run,
  323 passed, one live-server skip in 13.848 seconds; real pinned-b4 tests ran.
  Client-focused suite: 19 passed. No live content or database was changed.
- Swagger assertion initially found missing model definitions despite valid
  paths: fixed via existing response/parameter wrappers, not warning suppression.
  Grant now returns an explicit API DTO. Forced generation plus structured JSON
  assertions confirm both endpoints and all five models. Existing unrelated
  generator warnings remain. Formatting and editor diagnostics passed.
- Commit subject: `feat: add scoped replay-safe conversation import client`.

### 2026-10-30: F04 Review And Reply Substep

- Shared the F03 atomic evidence/participant/operation boundary without changing
  conversation payload serialization. Added import-only review and reply routes,
  request schemas and advertised capabilities. No voting or pending-review
  selection occurs: each imported review is new `COMMENT`, `Official=false`.
- Import validates the actual PR head/base/merge base before and after writes,
  and each range's full patch commit plus exact sole parent within that PR.
  Existing Git/diff parsers validate contiguous same-hunk lines; truncated,
  binary, renamed, ambiguous, invalid, oversized and stale cases fail closed.
  Canonical code comments retain their selected old/new side, exact commit and
  nonempty context rather than silently remapping through blame.
- Provenance retains stable inline IDs. Replies may target only existing
  same-source imported code comments/replies in this PR, never private drafts
  or normal comments; they inherit the original range and keep their own
  participant identity. Review replay verifies the retained root IDs rather
  than rejecting legitimate later replies. Local edits cause a conflict.
- Real-Git API fixture covers a range on the first patch after a second patch
  becomes PR head, both three-line sides, a second reviewer, two private drafts,
  identical replay, bad parent, disjoint hunks, invalid positions, stale head,
  private/non-code reply targets and locally edited roots. An initial test
  selected an older fixture draft; corrected to assert exact test draft IDs.
- Added explicit review/reply client methods using the same capability-aware
  idempotent retry; wrong/duplicate/boolean inline IDs stay uncertain. No
  ordinary API fallback, implicit approvals, outbox migration or live writes.
- Verification: 325 Python tests run, 324 passed, only live-server skipped
  (13.924 seconds; real pinned b4 ran); focused client 20 passed. Full affected
  Go packages passed. Five import API cases passed in 6.356 seconds, and all
  seven ordinary `TestAPIPullReview*` cases passed in 12.040 seconds. Formatting,
  editor diagnostics and generated Swagger body/model assertions passed.
- This is not all of F04: participant-authored PRs and companion per-patch
  integration remain next. Parent Forgejo changes are still uncommitted.
- Commit subject: `feat: add scoped imported review and reply clients`.

### 2026-10-30: F04 Participant-Authored PR Substep

- Added a canonical imported-PR model entry point that preserves ordinary
  issue/PR defaults and suppresses cross-references only for imports. Dedicated
  import service reuses the atomic grant/source/participant/operation boundary;
  there is no session impersonation, notification/mention/code-owner delivery,
  automation dispatch, label/assignee mutation or remote fetch.
- New POST `review-imports/pulls` accepts already-published same-repository
  branches with full commit pins and an ancestor base. It creates a participant-
  authored PR with maintainer edits disabled, canonical counters and a CAS-only
  pull ref. Provenance retains pins/index; replay validates them and later new
  imports refuse a moved branch. One source revision message cannot claim a
  second PR through another operation key.
- Ref creation cannot overwrite an existing ref. Error cleanup uses an expected
  commit; a real-Git collision test proves the old ref survives and SQL records
  roll back. Git refs and SQL are not one atomic storage system: a process crash
  between ref creation and SQL commit may leave a ref collision requiring
  visible operator recovery, never automatic overwrite. S04/R01 must document
  and supervise that incident path.
- The collision test exposed a real F02 nested-transaction defect: successful
  existing-participant lookup omitted `Commit` before deferred `Close`, which
  rolls back a reused Forgejo transaction. Fixed it and added a caller-rollback
  regression. The previously failing collision test now leaves no issue,
  message or provenance residue. A stale fixture-base assumption and one test
  import were also corrected, then the same focused checks reran.
- Added the companion's explicit PR method and strict result pin/index checks.
  No legacy publication command or persisted effect was silently switched.
- Verification: full `models/issues`, `models/user`, `services/issue` and
  `services/pull` passed. All six isolated import API cases passed in 7.460
  seconds. Python: 325 run, 324 passed, only live-server skipped (13.654 seconds;
  real pinned-b4 checks ran). Formatting, editor diagnostics and generated
  PR schema/response assertions passed. No live migration/restart/content writes.
- F04 server primitives are complete. C05/S01/S03 now wire per-patch source
  plans and publication to these endpoints; F05/F06 add source chronology/UI.
- Commit subject: `feat: add pinned participant-authored PR import client`.

### 2026-10-30: C05 Imported Per-Patch Queue And Delivery

- `PreparedMailbox.patch_commits` maps ordered original patch Message-IDs to
  an exact single-parent Git chain reaching the recorded immutable head. Wrong
  count, duplicate membership or nonlinear chains fail closed; legacy preflight
  results/digests are unchanged.
- Added separate `IMPORT_CONVERSATION` and `IMPORT_REVIEW` events with payload
  version 3, source identity/time and retained patch/quote/diff evidence. Exact
  matches group into a fresh non-voting review; the complete reply is retained
  separately. Legacy effects, including completed effects, cannot silently
  switch into imported authorship, nor vice versa.
- Imported delivery has its own digest-approved planner/write path. It reruns
  quote extraction/range resolution against retained mail and exact diff, checks
  source/patch hashes and original sender, then verifies current server pins.
  It never adds tool attribution or falls back to ordinary comment APIs.
  Uncertain sends require explicit same-key retry approval.
- Added import-only comparison and exact patch-parent diff reads, behind the
  existing grant/scope/permission gate and actual PR comparison. No broad repo
  or user scope is needed. Diff bytes are UTF-8, SHA-256 checked and capped at
  8 MiB, including bulk-copy writes. Private drafts are not read.
- Tests cover an earlier patch behind a later PR head, non-cumulative matching,
  stable queue replay, scoped reads, changed server pins with zero POSTs,
  response-loss reconciliation, sender tampering, bad hashes and migration
  refusal. A fixture mapping conflict and indentation error were repaired and
  the same focused checks reran.
- Python: 328 run, 327 passed, only live-server skipped in 13.989 seconds (real
  pinned-b4 ran). Focused preflight 23, queue 35 and client 21 pass. Full affected
  Go packages and six import API cases passed (7.558 seconds). Formatting,
  diagnostics and comparison Swagger assertions passed; unrelated old schema
  warnings remain. Live instance untouched.
- These are callable Python boundaries, not yet the supported CLI workflow.
  C05/S01/S03 still need publication/queue CLI wiring and isolated full workflow
  acceptance. No legacy payloads, IDs or approvals were auto-migrated.
- Commit subject: `feat: queue and deliver exact imported patch reviews`.

### 2026-10-30: C05 Imported Queue And Delivery Commands

- Added explicit `queue-imports` and `deliver-imports` commands, separate from
  legacy comment commands. Tokens come from owner-only regular files. Queueing
  selects retained reply Message-IDs, reassembles the recorded series using
  pinned offline b4 in an isolated clone, verifies the exact immutable head,
  and forwards its ordered patch map plus server-verified diffs. Reassembly
  mismatch queues no effects. Worker locks are not nested across helpers.
- Delivery previews open the ledger read-only; remote writes require paired
  `--write`/`--approve`. Conversation and review batches have separate approval
  digests and explicit uncertain-retry flags. Legacy effect migration remains
  separate and unsupported by these new commands.
- Focused queue/CLI/reconstruction suite: 37 passed. Full Python: 330 run,
  329 passed, only live-server skipped in 14.069 seconds (real b4 enabled).
  Editor diagnostics passed. No live writes, operational ledgers or credentials
  were used in tests. C05 full isolated workflow acceptance and S01/S03
  publication/discovery coordination still remain.
- Commit subject: `feat: expose scoped import queue and delivery commands`.

### 2026-10-30: S01 Immutable Imported Revision Publication

- Added paired base/head ref publication with atomic create-only Git leases.
  Both refs are pinned per revision; moving upstream cannot change an older
  comparison. Existing refs must match exactly and are never overwritten.
  Partial/uncertain writes are reconciled before any retry.
- New imported publication planner reassembles retained sources with pinned
  offline b4, binds source hashes, ordered patch commits, actor/repository,
  destination, immutable refs, request and Git username into a new approval
  digest. It creates one participant-authored PR per revision by default and
  adds links to completed earlier revisions only in the new PR body.
- `sync-imported-series` requires separate private API/Git token files and an
  explicit Git username. Preview opens an existing ledger read-only and makes
  no writes. Application rechecks source/approval/capabilities before refs,
  then uses the scoped idempotent import API and durable publication states.
  Retry approval binds `retry_uncertain` without rewriting the original intent
  digest. Existing complete publications verify their original PR index.
- Tests cover real Git paired refs, upstream advancement, independent revisions,
  differing existing refs, no-write preview/changed approvals, response loss,
  explicit uncertainty retry, and V2 linking while V1 payload/refs stay unchanged.
  CLI routing tests verify read-only previews and separate credentials. Legacy
  series commands, digests and publication records are unchanged.
- Full Python: 334 run, 333 passed, only live-server skipped in 14.651 seconds
  (real b4 enabled). Focused preflight 24 and series lifecycle 23 pass; CLI help
  smoke test passed. No live refs/content or operational state were modified.
- C05 still needs automatic email-to-existing-inline-thread reply queueing and
  isolated full workflow acceptance; S02/S03 add bounded discovery/coordinator.
- Commit subject: `feat: publish immutable participant-authored revision PRs`.

### 2026-10-30: C05 Exact Imported Inline Thread Replies

- Added a bounded retained-effect lookup, including completed effects, so quoted
  responses can identify the accepted inline comment without creating a new
  root review. An explicit parent Message-ID and one exact complete parent
  remark are required. Missing acceptance and duplicate text remain unresolved
  in the separately retained full-message conversation. No subject/fuzzy match
  or runtime LLM is used.
- New `IMPORT_REPLY` effects have deterministic per-remark identities and retain
  parent source/effect/operation/position evidence. Scoped lookup supplies root
  or nested reply comment IDs. Approval re-extracts both emails, verifies the
  same revision thread, source hashes/type, exact text, operation and target ID,
  and binds the parent body SHA-256. `deliver-imports --kind reply` uses its own
  reviewed digest and the existing same-key uncertainty handling.
- Forgejo now requires `parent_body_sha256` for reply imports and rejects an
  edited parent. Eligibility checks precede the hash check so private drafts
  remain invalid targets without exposing content-integrity information.
  Mandatory API field and generated Swagger were updated in the uncommitted
  parent worktree; no parent commit or live migration was performed.
- Focused queue/CLI tests: 38 passed. Full Python: 335 run, 334 passed, only the
  live-server test skipped in 15.095 seconds; both real pinned-b4 tests ran.
  All six isolated import API cases passed in 7.616 seconds. Formatting,
  editor diagnostics and structured generated reply-schema assertions passed.
- Initial validation exhausted root disk via regenerable Go build cache. Only
  that cache was cleared; subsequent Go tools use `GOCACHE` under `/tmp`.
  The disk-full run left the disposable integration database unable to reset
  fixtures; recreating only that generated database restored the test. The
  running instance and its database/configuration were untouched.
- C05 remains in progress only for isolated whole-workflow acceptance. Discovery,
  coordinator, chronology/provenance and unattended policy tasks remain open.
- Commit subject: `feat: route exact email replies into imported inline threads`.

### 2026-10-30: S02 Configurable Recent Discovery

- Exposed the existing durable bootstrap window as `discover-nntp
  --lookback-hours`, keeping the 148-hour default and bounded 1-8760-hour range.
  Invalid values fail before network access or checkpoint creation; an existing
  source's window cannot silently change. No schema migration or old checkpoint
  rewrite was needed.
- New tests exercise the CLI at the minimum, 24 hours and maximum, durable
  reopen/deduplication, changed-window refusal and invalid input. All 42 NNTP
  tests pass, including older-parent recursion, repair/selected retries,
  unavailable-parent blocking, partial/timeout resumption and source-history
  resource guards. Documentation makes index-time/coverage limitations explicit.
- Full Python: 337 run, 336 passed, only live-server acceptance skipped in
  14.946 seconds. Both real pinned-b4 tests ran; editor diagnostics passed.
- No live discovery, operational ledger access or remote writes were performed.
  S02 is complete; multi-stage dry-run coordination is S03.
- Commit subject: `feat: expose durable configurable recent NNTP discovery`.

### 2026-10-30: S03 Read-Only Sync Coordinator

- Added a create-only mode-0600 SQLite backup helper; source bytes remain
  unchanged when the copy is modified, and an existing destination is never
  overwritten. The bounded `plan-sync` coordinator uses only this private
  temporary ledger for optional recent discovery, numeric continuation, parent
  repair, thread recovery, ingestion, indexing and reply queue simulation.
- Existing exact publication/preflight and conversation/review/reply planners
  are composed without a write-enabled client or an encompassing worker lock.
  Published revisions use their retained base. Missing immutable refs and
  pending/uncertain publications block progress; new PR previews do not invent
  a pull index for replies. Independent child digests are not blanket approval.
- Missing/incomplete/ambiguous series, absent NNTP coverage, unknown bases,
  preflight errors and resource limits are explicit results. Refused write
  clients, original-ledger byte equality, snapshot permissions, CLI credential
  separation, bounded source calls and delivery-kind composition are tested.
- Focused lifecycle/coordinator tests: 29 passed. Full Python: 343 run, 342
  passed, only live-server acceptance skipped in 15.132 seconds; real pinned-b4
  tests ran. Editor diagnostics and whitespace checks passed. No live state,
  credentials, services or remote refs/content were changed.
- S03 is complete. C05 whole-workflow acceptance, F05/F06 chronology/provenance,
  S04 unattended authorization and V/R handoff remain open.
- Commit subject: `feat: add snapshot-isolated import sync planning`.

### 2026-10-30: F05 Imported Source Chronology

- The separate Forgejo worktree now initializes canonical creation/update
  timestamps from validated source evidence for new imported PRs, conversations,
  root reviews/ranges and replies. Unknown source time retains actual creation
  time. Provenance keeps actual import/update times separately and returns UTC.
  A reply never changes its parent review's chronology.
- Added the scoped `PATCH operations/{operation}/source-time` endpoint and
  advertised capability. Corrections require the original importer/current grant,
  matching prior payload hash/source time/basis, unique correction key and a
  reason. A transaction CAS-updates creation chronology and provenance and
  appends an audit with old/new quality/time, actor and actual correction time.
  Body, author and existing edit time are unchanged. Replay appends no duplicate
  audit; original import replay cannot reset corrected chronology.
- Tests cover all object types, offset-to-UTC normalization, unknown-to-estimated
  correction, immutable import time/hash, unchanged edit/content/author, same-key
  replay, changed intent, stale evidence, read-only scope, invalid future/quality/
  reason and revoked grants. Initial checks exposed XORM's auto-created field
  filter and local-zone conversion; explicit timestamp expressions and UTC
  projection repaired both, followed by the same focused checks.
- Final six isolated API tests passed in 9.236 seconds. Full `models/issues`,
  `services/issue` and `models/forgejo_migrations` passed. Formatting, editor
  diagnostics, whitespace and structured correction Swagger assertions passed;
  unrelated pre-existing Swagger warnings remain. Parent changes are uncommitted.
- No live migration/build/restart/content changes occurred. Correction is an
  explicit operator API, not worker self-approval or a legacy migration. F05 is
  complete; readable API/web provenance and correction history are F06.
- Commit subject: `docs: record verified imported source chronology`.