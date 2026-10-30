# Implementation Ledger

Updated: 2026-10-29. Design and boundaries: [PLAN.md](PLAN.md).

Statuses: `TODO`, `IN PROGRESS`, `DONE`, `BLOCKED`, `DEFERRED`. `DONE` requires
recorded verification, not just code. Each completed companion topic gets a
commit; record its subject here and use Git history for the resulting hash.
No automatic pushes. Forgejo changes belong to the separate parent repository.

## Current Iteration

- DOC-01 `DONE`: the implementation plan and this living ledger form the first
  commit, before implementation import. The user supplied an empty repository.
- B01 `DONE`: current source/history/tool inventory and offline baseline recorded
  below. B02 imported the unchanged source; installed b4 is still unavailable.
- First behavioral hypothesis: an unsafe inline delivery must not silently
  become a conversation write. The existing publisher does this; B03 removes
  it and immediately runs the focused comment tests.
- Historical sample publications remain background evidence only; the current
  baseline is 295 synthetic tests, with 3 explicitly reported skips.

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
| B03 | TODO | B02 | Remove inline-to-conversation fallback. Stale/disabled/unsafe inline effects make zero conversation POSTs, remain inspectable, and require a separately reviewed fallback. Run existing comment tests immediately after the edit. |
| B04 | TODO | B03 | Rename to neutral project/modules and installable lore_review_companion package with CLI/CI. Preserve legacy replay markers, destination/effect keys and versioned preflight identity. Independent install and old-state replay must not create duplicate writes. |

## Deterministic Multiline Reviews

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| C01 | TODO | B04 | Versioned range/parser/time/import payloads and migrations. Synthetic schema 18/21/22 upgrades, rollback/reopen and non-mutating previews preserve completed effects and approval contracts. |
| C02 | TODO | B04 | Pin one b4 runtime/entrypoint; explicit offline revision assembly without added trailers or partial rerolls. Verify membership/counts; no network, user-checkout mutation, or private draft publication. |
| C03 | TODO | B04 | Preserve complete quote blocks, depth, source offsets and remarks. Cover nested/trimmed quotes, MIME/CRLF, blank lines, preambles, signatures/trailers and multiple comments without losing text. |
| C04 | TODO | C03 | Resolve exact contiguous file/side ranges using pinned patch comparisons. Full context disambiguates repeated last lines; ambiguous/mixed/disjoint cases fail closed and configured range limits apply. |
| C05 | TODO | C01, C02, C04; F04 for live integration | Propagate ranges/evidence through queues, previews, approvals and API. Synthetic parse-to-fake-API tests cover replay, stale snapshots, multiple followups and uncertain writes with stable source-effect keys. |

## Forgejo Extension

These items modify the separate Forgejo fork. Record files, checks, and relevant
revision state here; do not stage them into a companion commit.

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| F01 | TODO | B01 | Explicit repository/importer grant and import-only scope/resource checks. Deny wrong/public repositories, revoked access/grants, transfers, cross-repo objects, Sudo and arbitrary privileged author IDs. |
| F02 | TODO | F01 | Atomic managed participant registry/profile creation and shared non-login invariants. Race-safe collisions cannot adopt existing users; no login/reset/OAuth/key/token/promotion/admin-edit bypass or participant mail. |
| F03 | TODO | F01, F02 | First vertical slice: conversation import onto existing PR with mapped author, importer provenance, source time/hash and unique operation key. Atomic rollback; replay returns same IDs; differing payload conflicts. |
| F04 | TODO | F03 | Add submitter-authored PRs, explicit non-voting COMMENT reviews/ranges and reviewer replies. Reuse services; verify exact patch parent context and no draft theft. Extend before-commit API only when a test proves necessary. |
| F05 | TODO | F03, F04 | Restricted original timestamp initialization/correction with audited actual import/edit time and source quality. Validate UTC/unknown/invalid times; no duplicate notifications or silent reauthoring of legacy content. |
| F06 | TODO | F03, F04, F05 | API and web render both participant and importer, lore link and source chronology/quality. Reuse existing UI; no forged identity verification or layout regressions. |

## Publication And Automation

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| S01 | TODO | B04, C02, F04 | Make separate immutable revision PRs the supported policy with pinned comparisons and cross-links. V2 leaves V1 unchanged; late replies stay with original revision; repeated publication creates no duplicate refs/PRs. |
| S02 | TODO | B04, C01 | Expose configurable recent lookback and durable resumable NNTP discovery/dependency recovery. Older ancestors work on demand; unavailable articles are not deletions; coverage and resource guards remain explicit. |
| S03 | TODO | C05, S01, S02, F04 | Dry-run coordinator for ingest, recovery, preflight, publication/reply plans. No remote writes by default; no nested-lock deadlocks; incomplete/ambiguous/unknown-base states are visible. |
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
- Initial flat-source check: `python3 -m unittest test_gitea_comments`.
- Related slices reuse `test_anchor`, `test_b4_adapter`, `test_review_queue`,
  `test_threads`, `test_series_sync`, `test_preflight`, `test_nntp_source` and
  `test_ledger`. Adapt paths after packaging, not before.
- Full offline baseline: `python3 -m unittest discover -s . -p 'test_*.py'` with
  live acceptance variables unset. Distinguish skipped opt-in tests explicitly.
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