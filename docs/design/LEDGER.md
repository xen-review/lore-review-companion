# Implementation Ledger

Read [../../CONSTITUTION.md](../../CONSTITUTION.md) first. Historical production,
private-policy and incident-latch restrictions in this ledger are superseded.
This is an early pre-alpha experiment with disposable development fixtures.

## Pre-Alpha Runtime Correction

- Added agent-first constitutions in both repositories and explicitly documented
  that this experiment has no production users or production deployment.
- Removed worker and on-demand incident latches (`64dc519`). Failed attempts
  report errors and later invocations retry. Changed policy digests reconcile
  cursors automatically while preserving effect enrollment.
- Added the public packaged Xen development profile: 240-hour overridable
  lookback, no arbitrary expiry, idempotent same-key uncertain retry enabled.
  Setup validates it instead of writing a private configuration copy. Tokens
  and credential-bearing server configuration remain secret.
- Added a committed development deployer with clean/pushed-source checks,
  matched server assets, packaged-profile verification and service/worker
  checks. Scoped fork/state rebuilds are authorized when useful; no upstream
  mirror or unrelated repository may be deleted.
- Checks before deployment: 396 companion tests pass, four expected skips;
  the scoped Forgejo API suite passes with disposable real-companion acceptance
  enabled. Three deployer tests cover dry-run, pushed-source enforcement and
  secret-preserving configuration. Touched-file diagnostics and whitespace
  checks pass.

### Rebuilt Staging Fixture: 2026-10-31

- Explicit rebuild recreated only `xen-review/xen` from the existing
  `xen-project/xen` mirror. The new repository ID is 3, and the committed
  profile pins `staging` at `8803b82ab2c95da09c837482cbcac11d7f3853e5`.
  Forgejo's fork endpoint has no default-branch option, so setup applies and
  verifies the committed branch with the repository-edit API after creation.
- Deployed Forgejo `dc5d6dd6c55e0673046acfe0803bbbeecc986c44` and companion
  `fb9d52795590c843e2b51f282e53f1a7a78710cf`. The live API reports repository
  3's default branch as `staging`; Forgejo is active and the companion timer is
  enabled/waiting. The temporary administrator token was revoked and its local
  file removed.
- Bounded bootstrap imported 3 PRs and 15 discussion effects: 9 conversations,
  2 reviews, and 4 replies. This is not complete source coverage. Other
  candidates were incomplete or failed patch preflight against the pinned base;
  one quoted reply could not be uniquely anchored and was safely deferred.
- The full companion suite passed 398 tests (four expected skips) at `8ccd48f`.
  Focused setup/API tests passed after the later repository-ID, trusted-base and
  staging-default changes. The final committed package and policy were deployed.

Updated: 2026-10-30. Design and boundaries: [PLAN.md](PLAN.md).

Statuses: `TODO`, `IN PROGRESS`, `DONE`, `BLOCKED`, `DEFERRED`. `DONE` requires
recorded verification, not just code. Each completed companion topic gets a
commit; record its subject here and use Git history for the resulting hash.
The operator authorized commits and pushes in both repositories on 2026-10-30.
Normal verified commits are pushed automatically; do not rewrite commit history.
Commit early and often: checkpoint each coherent implemented part immediately
after focused validation, before beginning the next part of a feature.
Forgejo topics use incremental stacked branches; the last contains every topic.
Forgejo changes belong to the separate parent repository. Existing operator
files, source snapshots and nested repositories are not part of these commits.

## Current Iteration

- Assigned Forgejo issue [#35](https://review-forge.be.free.or.at/xen-review/xen/issues/35)
  is pending: require the appropriate author Signed-off-by and create a blocking
  import issue when it is missing; stop PR descriptions at a real diffstat;
  remove a trailing standalone `---` delimiter. Validate the diffstat pattern
  against the example in the issue and preserve existing publication replay.
- Issue #35 source implementation is now pushed in companion `6814f37`
  (quoted new PR descriptions and diffstat/trailing-delimiter handling),
  `8feb442` (per-patch Signed-off-by blocking and Message-ID issue reporting),
  and `46a723c` (continuous backlog draining followed by ten-minute Lore polls).
  The complete companion suite passes 432 tests with four expected skips, and
  `git diff --check` is clean. The disposable fork rebuild and deployment are
  still pending, so these changes are not yet claimed as live behavior.
- The `review-companion` account is already in the `xen-review` Owners team;
  provisioning continues to verify that invariant but does not treat membership
  as the cause of the prior 403. The failed unattended capability check was
  caused by the missing explicit `review-import-grants/review-companion` grant
  and stale repository safety settings; rebuild/setup must apply and verify
  those settings before imports resume.
- Submission quote presentation: companion `bb97957` and real-Forgejo fixture
  `362c7b6` are pushed. The installed wheel is stamped
  `362c7b66ff34/test-verify-submission-quote-formatting-against-forgejo`.
  New `mail-text-v4` replies to recorded patch/cover parents fence verified
  non-code quotes as Rust and remove final standalone sender-name sign-offs;
  actual patch code and ambiguous or unmatched quotes remain untouched. Other
  new imported comment bodies also omit final sender-name sign-offs. Approval
  reconstructs the body from retained source; old effects keep their original
  format and operation identities, and previously posted comments are not edited.
- Read-only reconstruction of PR 1 source mail
  `25716378-3592-471f-a972-8446c32e29bd@citrix.com` produces the requested
  fenced quoted message and omits `~Andrew`; the operator-edited live comment
  remains untouched. Clean committed suite: 370 tests, 368 pass and two optional
  server skips. Isolated Forgejo/Git/b4 acceptance passed. The ledger remains
  at 27 complete effects and server import/notification/hook counts at 32/1/0.
  Private checkpoint `backups/20261030-presentation-362c7b6/` retains both
  preceding ledgers and the existing NNTP incident; the timer is active and
  that incident remains latched.

- Inline reply excerpt follow-up: companion matching/revalidation `3e2920b` and
  disposable Forgejo acceptance `f8a018d` are pushed. The then-installed wheel is
  stamped `f8a018d1d279/test-exercise-inline-reply-excerpts-against-forgejo`.
  Nonempty contiguous lines from the immediate depth-one quote match exactly one
  accepted parent remark after ignoring blank lines and surrounding whitespace;
  approval rechecks the accepted parent mapping and body hash. Existing exact
  evidence retains its original validation and immutable operation identities.
- Jan Beulich's retained reply
  `a3c4926f-9e69-441b-bb4e-0f8324e17f02@suse.com` produced three approved
  inline replies. PR 5 shows comments
  [41](https://review-forge.be.free.or.at/xen-review/xen/pulls/5#issuecomment-41)
  and [42](https://review-forge.be.free.or.at/xen-review/xen/pulls/5#issuecomment-42)
  under Andrew's comment 25, and
  [43](https://review-forge.be.free.or.at/xen-review/xen/pulls/5#issuecomment-43)
  under his comment 40. All display Jan's original remark, author and source.
  A message-scoped requeue created no duplicates and the reply delivery preview
  has no pending effects. Three additional matches from a different mail were
  previewed but not queued or delivered as part of this demonstration.
- The clean committed suite passed 366 tests with real b4, with two optional
  server tests skipped. The isolated Forgejo/Git/b4 import suite passed with two
  excerpt replies to different parents. The ledger now has 27 completed effects;
  server import records increased from 29 to 32, while notification/hook counts
  remained 1/0. Private checkpoint `backups/20261030-inline-replies-f8a018d/`
  retains the preceding ledger, scheduler and NNTP incident. The timer remains
  active and the pre-existing incident remains latched.

- Multi-hunk inline review follow-up: companion resolver `4e109f2` and independently
  approved segmented effects `ad21e9d` are pushed; companion acceptance fixture
  `eec1287` and Forgejo fixture `f297f8dc90` are pushed. The then-installed companion
  wheel is stamped `eec12875576d/test-exercise-segmented-inline-reviews-against-forgejo`.
  Every quoted segment must match its pinned patch uniquely, in hunk order, with
  exact quoted hunk headers; only the final contiguous segment is selected.
  Previously accepted review payloads and IDs remain unchanged.
- Andrew Cooper's retained PR 5 mail
  `35fa29d5-fafc-43da-a5a5-72df8f367e3c@citrix.com` has seven quoted remarks.
  Six were already imported; the seventh is now independently delivered at
  https://review-forge.be.free.or.at/xen-review/xen/pulls/5#issuecomment-40
  on `xen/arch/riscv/cpufeature.c` lines 723–745 in the pinned patch. The actual
  PR view shows the original remark, author and source link. Requeue leaves the
  previous six IDs unchanged, the new operation complete and no pending effects.
  Nine quote/remark pairs in other replies remain unresolved; some do not
  identify exactly one accepted inline parent. Their complete text remains in
  the conversation/ledger.
- Exact committed suite: 364 tests, 362 pass and two live-server skips. Focused
  anchor/queue checks cover ambiguity, mixed sides, wrong hunks, replay and
  evidence tampering. The disposable Forgejo/Git/b4 two-hunk acceptance workflow
  passes in 21.637s. Notification/hook counts did not increase. Private paired
  companion backup `backups/20261030-inline-review-eec1287/` preserves the prior
  ledger, scheduler state and existing NNTP incident latch.

- Presentation follow-up is deployed: Forgejo `f83fc8258a` (including audited
  correction `1adc8ba36a` and source-free inline-review binding `62a85160f3`),
  companion `ff82557` (cover/title `b3dfff9`, build/header/v3 bodies `eb8104a`).
  Workflow instruction `c379733` requires immediate verified-part commits and
  pushes. Every part was committed/pushed normally; no history was rewritten.
- PR 4 now reads `Kevin Lampis: x86/kexec: Secure Boot support [v3]`, with source
  prose only. Scoped `cover-prose-v3` correction retained original payload hash,
  author, creation/edit timestamps and refs; same-key correction and original
  publication replay pass. One presentation audit exists; no blanket rewrite.
- New reviewed submission: https://review-forge.be.free.or.at/xen-review/xen/pulls/5
  is Baptiste Le Duc's RISC-V v10, seed
  `1793192679.8631fc262581453bbf619ec5b2062170.1a1829cd6d5000f79f@vates.tech`,
  one patch, head `5b728bae014663b68af643d3f067fffd66963a1e` on the fixed base.
  Imported six real reply emails, Andrew Cooper's six exact inline remarks and
  three exact native replies. Ten quote blocks remain unresolved for inline
  placement; full conversation/raw mail retain them. No guessed anchors.
- Actual public PR 5 desktop/mobile DOM and screenshots verify submitter/revision
  title, no generated cover shortlog/diffstat/revision line, no body footnotes,
  linked `xen-devel` provenance and the actual posting build
  `ff825577a64f/fix-remove-validated-b4-cover-tails-including-base-metadata`.
  Relative time renders correctly. Requested 1440x1000 and 390x844 viewports
  (browser zoom yields 1152/312 CSS-pixel widths) have no page/footer overflow or
  title/metadata overlap. Older unknown posting builds were not invented.
- Original PR 4/5 publication replays and requeue are idempotent. Ledger has five
  complete publications and 23 complete effects, none pending/uncertain. Server
  has 28 import records, one presentation audit, zero notifications/hook tasks.
  Both repository master refs remain `e4956d5e6bdcb7e2e9abfa4474b2dc1160c51a45`.
- Validation: exact companion commit passes 361 tests, 359 pass/two explicit
  server skips; scoped Forgejo import integration passes, and disposable real
  Git/b4/server workflow passes in 20.007s after the empty-summary binding fix.
  Display cleanup regression and migration registration/order checks pass.
  Generated Swagger correction body/schema was checked with a JSON parser.
  Source archive and wheel carry identical true build stamps; clean wheel has
  no dirty suffix. New migrations and public/loopback health pass in production.
- Paired private rollback checkpoints `backups/20261030-1adc8ba36a/` and
  `backups/20261030-reviewed-pr5/` retain database/companion snapshots, old
  executable, config and reflinked repository storage; the latter includes the
  tested companion wheel. They are on-host, not off-host disaster recovery.
  The preexisting NNTP 430 incident remains latched, pending mail retained;
  timer is enabled but healthy unattended ingestion is not claimed.
- Operator edits to mail-parser typing and series formatting, plus untracked
  `prek.toml`, remain unstaged. Scoped formatter checks pass; a global format
  check reports the concurrent series-formatting edit. Latest `uv check --no-sync`
  reports 458 diagnostics in the concurrent worktree, still a failing type gate,
  not a claimed improvement over the historical 564 baseline.

- Local ingestion/presentation follow-up (committed, not installed):
  `fetch-nntp --message-id` now queues one normalized ID after an NNTP checkpoint
  exists and validates its ARTICLE response against the requested ID and group.
  The Xen `recover` command fetches an unreceived seed before thread discovery
  and refuses to bypass the worker incident latch. Batch fetching defers only
  ARTICLE 430 responses, reports them, and retains those queue rows; an
  instrumented temporary-ledger probe confirmed the live 430 is from ARTICLE,
  after group-validity checks. The worker incident latch remains set.
- New `mail-text-v6` diff quotes recognize `diff --git`, generic
  `diff a/... b/...`, and unified file headers; nested replies retain immediate
  quote blocks in Rust fences before prose. The retained PR 5 source
  `a3c4926f-9e69-441b-bb4e-0f8324e17f02@suse.com` reconstructs three v6 quote
  blocks and passes the approval payload validator in memory. Its four stored
  v3 effects remain untouched.
- PR 6 comment 45 has a pending, append-only v6 recreation in the operational
  ledger as delivery 42. Its original operation and source are bound to the new
  effect; the current public comment body was verified against the original.
  The effect requires explicit ID selection and manual digest approval, and has
  not been delivered. Existing comment 45 and its child remain unchanged.
- Verification: 387 companion tests, four expected skips; 45 NNTP tests, six
  Xen setup/recovery tests and 16 presentation tests pass. Editor diagnostics and
  `git diff --check` pass. Repository-wide formatting still reports unrelated
  existing differences. No service restart, deployment, remote comment
  recreation, or notification/hook count query was performed. T02-T05 remain
  `IN PROGRESS`; browser and deployed verification gates remain open.

- 2026-10-31 checkpoint follow-up: the operational NNTP window was explicitly
  extended from 148 to 240 hours, retaining the forward cursor and queueing 142
  additional IDs. A private paired SQLite backup at
  `~/.local/state/lore-review-companion/backups/pre-window-240-yhvh2j60/` passed
  integrity checks before the extension. The cover and nine patches of Marcus
  Granado's LZ4 v2 series were then fetched by exact Message-ID; nine replies
  were already retained. A read-only preflight reports `publication_ready`,
  but no PR or comments were created. The worker remains incident-latched, the
  timer remains active, and its existing policy still specifies 148 hours.
  Reconcile the policy and scheduling state before any worker recovery; do not
  silently clear the latch or assume that changing the checkpoint updates policy.
  Details and the next-chat handoff are in [HANDOFF.md](HANDOFF.md).

## Autonomous Import And Audited Refresh Follow-up

- The user clarified that importing a new submission includes all retained review
  replies automatically, under the companion's deterministic private policy.
  An AI/operator must not be required to complete ordinary reply import.
- The worker now recovers candidates for the selected thread before publication,
  defers only on that thread's missing source, validates reply ancestry, accepts
  parents before queueing children, and drains all effect IDs rather than
  truncating at one API batch. New PRs process their complete retained discussion
  in the initial publication round; manual pending effects remain protected.
- `refresh-import` implements a retained-source in-place rebuild with a private
  approval plan, full snapshot compare-and-swap, complete original-operation
  verification and per-comment audits. It preserves PR/ref/comment/review IDs,
  source times and reply ancestry, refuses missing/edited/uncertain evidence and
  emits no notifications. Replacement is never implicit. Forgejo commits
  `9bb6521eaf` and `8c3229016b` are pushed; deployment remains outstanding.
  Companion implementation and regression coverage are pushed as `b42bc70`.
- Companion `f47a1af` changes new checkpoint/setup defaults to an overridable
  240 hours. Explicit smaller initial values and larger backfills remain
  supported. Existing durable checkpoints and private policies are not silently
  rewritten.
- The LZ4 v2 plan was regenerated read-only and matches the earlier digest,
  base and head. Ten source messages and nine Teddy Astie replies are retained.
  This is plan review, not publication approval. Runtime deployment, policy
  reconciliation and incident recovery remain gated; delivery 42 is untouched.
  See [HANDOFF.md](HANDOFF.md) for the current evidence and operational blockers.
- The disposable real-Forgejo companion workflow also exposed and verified fixes
  for two queue defects: a conversation closure captured a later inline operation
  key, and fully anchored replies omitted their canonical complete conversation.
  Both fixes are committed and pushed as `4a4ea0e`; full-message and inline effects
  retain separate stable identities and approval states.
- Verification: 393 companion tests pass with four expected skips; the scoped
  Forgejo import API suite passes. The explicitly enabled disposable companion
  acceptance workflow passes with real Git/HTTP publication, automatic replies,
  inline review mappings, refresh/retry/original replay and protected pending
  effects. Direct CLI tests cover private plan creation, approved writes and
  wrong-digest refusal. Touched-module editor diagnostics are clean. This is not
  production deployment or evidence of complete archive coverage.

## Remark-Level Threading Follow-up

The first storage foundation is implemented but not deployed; all user-facing
threading phases remain incomplete. Detailed boundaries are in
[PLAN.md](PLAN.md#planned-remark-level-discussion-trees).

- Forgejo `e761990cb0` adds an indexed `discussion_parent_id` column, separate
  from cross-reference metadata, with a schema migration. `3d4dc710ba` rejects
  missing, cross-PR, cyclic and over-depth parent chains at creation. Focused
  SQLite model and existing-table migration tests pass. Both commits are pushed;
  neither is deployed. The migration does not yet backfill old inline replies.
- Forgejo `585e359488` adds an optional ordinary-comment parent to the existing
  PR comment API; integration tests cover nested comments, missing and cross-PR
  parents and anonymous requests. It does not accept inline parents. Forgejo
  `802ebfeba8` stores dedicated parents on new imported inline replies and
  rejects replay if a nonzero dedicated parent changes, while allowing older
  zero-valued imports. The focused review-import integration test passes.
- Forgejo `76788a443b` advertises `conversation-reply` and imports replies to
  source-owned ordinary comments and their nested replies, with parent-body
  hash checks and idempotent replay. Integration tests cover nested replies,
  conflicting replay, wrong parent hash and unowned parents. The full scoped
  import group and the isolated Forgejo/Git/b4 companion workflow pass.
- The companion now resolves exact, unique depth-one quotes against accepted
  ordinary parent prose, carries the ordinary target kind through nested
  replies, rechecks source, parent identity and capability during approval, and
  uses a distinct ordinary-reply delivery response contract. Existing inline
  effects retain their prior identities and behavior. The isolated real-server
  workflow verifies a three-level ordinary discussion, API parent IDs and
  idempotent replay. The companion suite passes 371 tests with four skips;
  neither this change nor the Forgejo threading commits are deployed yet.
- Forgejo `98f1450e35` backfills only verifiable same-source imported inline
  parents, preserving existing edges and rejecting cycles, ambiguous provenance,
  invalid chains and ancestry deeper than 100. The migration package passes.
  `0bf5805d3f`, `b8da582e31` and `cda142fe72` add ordinary and inline trees,
  per-comment web reply controls, normal API parent IDs, and explicit deleted-
  parent placeholders. Focused integration tests cover nested replies in both
  views, deletion, invalid targets, archived repositories and pending-review
  privacy; the live deletion placeholder also has a JavaScript unit test.
  Swagger parent fields were generated and validated in `c76f179a6a`.
- Companion approval now repeats the ordinary-parent uniqueness check and
  rejects changed operation types. Forty-four queue tests cover ambiguous
  parents and cover-letter-only ancestry; the isolated Forgejo/Git/b4
  workflow passes again. None of these threading changes is deployed yet.
  Remaining work includes broader permission/security review, desktop/mobile
  browser verification, and a new deployed PR showing the actual discussion.

| Phase | Status | Dependency | Completion evidence |
| --- | --- | --- | --- |
| T01: tree contract | DONE | None | Contract specifies parent identity, deletion fallback, ordering, bounds, access controls, compatibility and accessibility in PLAN.md. |
| T02: Forgejo parent model and user API | IN PROGRESS | T01 | Persist parent edges, safely backfill imported inline parents without confusing cross-references, and test normal-user permissions and invalid/cyclic targets. |
| T03: scoped import API | IN PROGRESS | T02 | Support non-inline and nested parents with capability negotiation, source provenance, parent-content checks, conflict handling and replay. |
| T04: tree presentation | IN PROGRESS | T02 | Ordinary and inline trees, controls, deletion fallback and focused integration checks pass; desktop/mobile browser and accessibility verification remain. |
| T05: remark-level companion | IN PROGRESS | T03, T04 | Ordinary and nested matching, ambiguity checks, cover ancestry and real-server replay pass; rendered deployed behavior still requires verification. |

## Earlier Rollout Notes

- 2026-10-30 follow-up: the Xen installation/provisioning guide and explicit
  on-demand import script are in [INSTALL-XEN.md](../INSTALL-XEN.md).
  `81825d6` checkpoints new presentation/API setup; `81b8d53` is the separately
  requested **formatting-only** commit, pushed without history rewrites.
  All Python sources now use `uv format`; current 48-file format check passes.
  `uv check --no-sync` is run separately: initial 565/current 564 type diagnostics
  remain, not suppressed and not claimed as a passing gate. Operator-owned
  prek configuration is left untouched.
- Current companion checks: 357 tests, 355 pass, two explicit server skips,
  15.127s. Real disposable Forgejo/Git/b4 workflow passes in 19.811s. The dedicated
  import script reports the retained ARM v13 publication `up_to_date` with no
  remote or ledger writes. Source/wheel build and guide/script inclusion pass.
- New `mail-text-v2` effects use original prose plus an escaped small named lore
  link, not a numbered footnote. New PR covers strip only recognized trailing
  diffstat/Git signatures and put revision metadata last. Existing publication
  digests choose their original body; existing effect formats remain immutable.
  Exact native inline replies tolerate blank-only mail quoting differences,
  never changed words/ambiguous remarks. New nested mail waits for an accepted
  explicit parent; conversation display omits quoted history and links the
  proven parent, while raw mail remains complete. Parent ID/source/type and
  inline body hashes are revalidated before delivery.
- The live worker subsequently failed at 2026-10-30 17:16 CET on NNTP 430 and
  retained pending work with an incident latch. This predates installation of
  these changes. The incident is not cleared by a presentation/code upgrade;
  previous P04/P05 successful rollout evidence below is historical, not a claim
  that the current source round is healthy. No failed source work is discarded.
- Final workflow/docs commit `beab2ec` is pushed and installed in the companion
  runtime. Fresh-wheel installation verifies all three console entry points and
  dependency consistency; pinned b4/liblore source fingerprint remains unchanged.
  Final full suite: 357 run, 355 pass, two explicit server skips, 15.027s;
  `uv format --check` passes for all 48 files. Existing `uv check` type debt
  remains 564 diagnostics. No operator prek configuration was staged or edited.
- Forgejo footer/naming commits `ca043ab99c`, `f9e8e31ded` and `4242acdd5a`
  are pushed; the last is built/deployed under the existing review service.
  Template tests pass. Native timeline/files author links now show mapped
  participant names even with username-oriented instance display settings.
  Live desktop/mobile DOM and screenshots show content-before-metadata, small
  footers, no repeated participant/unverified label, a separate-line PR lore
  link, no duplicated comment provenance link and no page overflow. Ordinary
  user display names and API source-quality fields are unchanged.
  Existing imported bodies are deliberately not rewritten or re-approved.
- Public/loopback health and deployed version checks pass. Private checkpoint
  `backups/20261030-ca043ab99c/` preserves the old executable and an online
  SQLite snapshot with verified integrity. The preexisting NNTP incident remains
  latched; upgrading rendering did not restart source ingestion or discard it.
  The operator's saved parent whitespace-revert diff is untouched; only the
  explicitly confirmed footer/name edits were restored after that cleanup.

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
- C05 `DONE`: native quote-to-range queue, evidence preview, approval,
  delivery and replay are verified, including imported-author per-patch effects.
  Explicit publication/queue/delivery CLI commands and exact email-to-inline
  thread replies and isolated whole-workflow acceptance pass. No runtime
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
- F06 `DONE`: web and ordinary read APIs show importer/participant identities,
  lore source, original chronology/quality, actual import time and bounded
  correction history. Desktop/mobile layout and ordinary reviews are checked.
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
- S04 `DONE`: private expiring policy plus server-admin unattended grant gates
  bounded worker rounds. Review-only settings and identity are rechecked; only
  worker-enrolled effects deliver, scheduling is durable and failures latch.
- Historical sample publications remain background evidence only; the current
  baseline is 350 tests, 348 passing with two server opt-ins skipped when real
  pinned b4 is enabled. The separate real-Forgejo workflow and hosted Python
  3.11/3.12/3.13 matrix pass. V01/V02, technical R01 and license adoption L01
  are complete; P01-P08 separately record production rollout and initial population.
- Production Forgejo is now built, backed up, migrated and healthy under its
  enabled user service. Four real Xen revision PRs are published with eleven
  complete-message conversations and two imported reviews containing three exact
  inline remarks. Original review prose has only a lore source footnote added.
  The bounded worker timer is enabled under a policy expiring 2026-11-06;
  live restart/replay, grant revocation and review-only safety checks pass.
- The operator moved these design documents under `docs/design/`, removed
  obsolete source-context files and renamed the environment prefix to
  `LORE_FORGE_`. Current license and migration guidance are in
  [COPYING](../../COPYING), [CONTRIBUTING.md](../../CONTRIBUTING.md) and
  [README.md](../../README.md). Legacy payload identities remain unchanged.

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
| B02 | DONE | B01 | Unchanged Python/requirements snapshot imported with checksums, legal context, provenance and ignores. Original files untouched; imported offline suite passed. |
| B03 | DONE | B02 | Removed inline fallback; tests prove stale/disabled/pending-review paths make zero POSTs and preserve unresolved effects. Separate conversation approval still works; 25 publisher and 31 queue tests passed. |
| B04 | DONE | B03 | Neutral adapters and installable package/CLI added with README and offline CI. Wheel installed and entry points tested outside checkout; 296 tests run, 3 skips. Legacy wire identities and token options retained; hosted CI matrix not yet run. |

## Deterministic Multiline Reviews

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| C01 | DONE | B04 | Atomic schema setup, rollback, read-only reopen and schema 18/21/22 payload preservation tested. Version-2 ranges preserve source/parser/time/comparison evidence; legacy approval digests unchanged. Future server import mappings belong to F03/F04. |
| C02 | DONE | B04 | Verified b4 0.17.dev0/liblore 0.10.1 combined source fingerprint; same-interpreter offline assembly pins revision, disables trailers/partial rerolls and checks ordered patch content/count. 12 focused tests pass including real local b4. |
| C03 | DONE | B04 | Added deterministic quoted-block extraction with depth/headers/source offsets, remarks and conservative signature/trailer boundaries. 13 mail-parser tests pass, including nested/trimmed and MIME/CRLF cases. Full message remains canonical. |
| C04 | DONE | C03 | Exact contiguous same-file/side range matching added separately from legacy Anchor. All 13 anchor tests pass; full context disambiguates repeated lines, while mixed/disjoint/headerless/renamed/ambiguous cases fail closed. |
| C05 | DONE | C01, C04, F04 | Exact assembled patch mapping, version-3 imported conversation/review/thread-reply queue and digest-approved CLI delivery are tested. Exact parent quotes bind accepted comment IDs and body hashes, including nested replies; missing/ambiguous parents remain unresolved. Source/quote/diff identity is revalidated; changed pins/targets cause zero POSTs, response loss replays the same key, and legacy effects cannot silently change authorship. Isolated real-Forgejo/b4 workflow and full 347-test regression pass. |

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
| F06 | DONE | F03, F04, F05 | Shared localized provenance on PR covers, conversation/review headers, timeline inline threads and exact files-changed views. Ordinary issue/PR/comment/review/range read APIs expose separate importer/participant names and IDs, source chronology/quality, actual import times and bounded correction history with explicit truncation. Six import and seven ordinary review API cases, models/converters, locales, schemas and desktop/mobile checks pass. |

## Publication And Automation

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| S01 | DONE | B04, C02, F04 | `sync-imported-series` uses one PR and paired immutable base/head refs per revision, pinned source/patch maps and prior-revision links. V2 leaves V1 unchanged; source-thread checks retain late replies on their recorded revision. Repeated writes reconcile the same operation/refs. Tests cover upstream movement, two revisions and approved uncertain retry; full live workflow remains V01. |
| S02 | DONE | B04, C01 | `discover-nntp --lookback-hours` exposes a bounded 1-8760-hour initial window (148 default), fixed per durable source checkpoint. 42 NNTP tests cover restart/deduplication, older-parent recursion/repair, selected retries, unavailable items retained pending, bounded thread discovery and byte/disk/group-history guards. No verified receipt-time or full thread coverage is claimed. |
| S03 | DONE | C05, S01, S02, F04 | Bounded `plan-sync` previews discovery/recovery, exact publication/preflight and all three imported delivery kinds using a private SQLite backup. Original ledger and remote refs/content stay unchanged; no write flags or blanket approval. Tests cover fixed resource bounds, snapshot isolation, retained-mail/no-NNTP state, missing/incomplete/ambiguous/unknown-base/preflight/pending-publication blockers and independent child plans. |
| S04 | DONE | S03, F05, F06 | Explicit private expiring local policy and admin-owned server unattended grant bind repo/importer/source identities, trusted base, rules and budgets. Review-only Actions/hooks/secrets/merge/update/head-edit settings are checked at grant and on unattended writes. Bounded worker rounds rotate durable work, preserve separate Git/API credentials, enroll only newly queued effects with fingerprints and latch failures. Tests cover revocation/identity drift/strict policy schema/manual-effect exclusion/replay/state isolation; sanitized user-level templates are supplied, with production activation separately recorded in P04/P05. Whole-workflow acceptance is V01. |

## Acceptance And Handoff

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| V01 | DONE | C05, F06, S04 | Real isolated Forgejo/b4 workflow covers two-patch V1/V2, distinct reviewers, late nested replies, old/new historical ranges behind head, chronology, duplicate-ID/hash conflict and accepted-response loss. Actual worker delivers only enrolled new effects and restarts without duplication; importer/participant private drafts, manual pending effects and upstream refs remain unchanged. Unavailable ancestry queues nothing. Component collision/isolation cases also pass. |
| V02 | DONE | V01 | Eight import API cases pass (26.903s), as do affected security/model/service/converter/migration packages. Prior seven ordinary review cases and desktop/mobile screenshot/overflow checks pass. Hostile provenance is escaped. Formatting, locale/usage lint, diagnostics and structured Swagger checks pass; all existing paths/models retained. Generator needs bounded memory; existing unrelated warnings remain. |
| R01 | DONE | V02 | Wheel installed in a fresh environment outside checkout; console/module entry points and pip check pass. Source/wheel builds retain legal context, dependency pins and operational inputs without runtime/operator state; packaging omission repaired by explicit manifest. Upgrade/backup/restore instructions and inert service templates documented. Hosted CI passes Python 3.11/3.12/3.13. Initial topics were pushed; later squashed history and review commits are operator-managed. No public package release or upstream PR. |
| L01 | DONE | B02 | Adopted GPL-2.0-or-later with official COPYING, SPDX source/configuration notices, contribution terms and PEP 639 package metadata. The unrelated Gitea license and obsolete provenance files were removed by the operator. Preserve third-party notices and check directional compatibility before importing external code. |

## Production Rollout

The operator requested production planning and execution on 2026-10-30. These
are separate gates, not implied by component or isolated acceptance results.
No production build/restart, migration, token provisioning, policy enablement
or deployment had occurred when these gates were added. Never log credentials,
post historical pending effects implicitly, or use a production DB for tests.

| ID | Status | Depends On | Step And Acceptance Gate |
| --- | --- | --- | --- |
| P01 | DONE | V02 | Exact process/work path/SQLite/storage/health verified without exposing secrets. Preserved the running cff8e8831a executable before stopping only its PID. Private cold config/data/repository archive restored into a mode-0700 scratch directory; SQLite integrity is ok. Rollback requires complete pre-migration snapshot plus old binary, not a binary downgrade. |
| P02 | DONE | P01 | Built sqlite/sqlite_unlock_notify candidate 09401fd3c6 with temporary cache and bounded Go memory. Started the exact work/config paths under enabled forgejo-review.service. All three review migrations are present; loopback/public health and embedded API version pass. User lingering is enabled. No shared SSH configuration was changed. |
| P03 | DONE | P02 | Password gate cleared and both selected-repository credentials verified. Effective access was stale after Owner-team removal; supported collaborator read/write updates recalculated it. Importer push=true/admin=false. Disabled merge/rebase/update/manual/head-edit methods; source-bound Xen lore unattended grant and numeric identities verified. Temporary administrative bootstrap revoked in finally, with no credential values exposed. |
| P04 | DONE | P03, R01 | Installed companion 2e12fb9 with pinned b4/liblore; exact source fingerprint and pip checks pass. Retained real NNTP mail, reviewed immutable publication/delivery digests and enabled the seven-day repository/source-bound policy. Two bounded no-refresh worker rounds and the hardened systemd source-refresh round succeed without duplicate writes. |
| P05 | DONE | P04 | Historical grant-revocation and recovery checks passed; temporary admin tokens were verified absent. The original scheduler is superseded by the continuous worker service. |
| P06 | DONE | C05 | New imported conversation/range/reply bodies use original source prose plus a lore footnote; headers contain the footnote alone. mail-text-v1 is explicit and source-bound; untagged old effects retain exact request bytes and approval/replay identity. All 39 queue tests and actual isolated API/Git/b4/worker acceptance pass, including multiline ranges, nested replies and old-payload requeue. Full suite: 350 run, 348 pass, two deliberate server skips, 14.993s. |
| P07 | DONE | P03, P04, P06 | Published the three digest-approved actual Xen revisions below, eleven original-message conversations and two reviews with three exact inline remarks. The timer also published ready kexec v3 as PR 4. Actual participant authorship and immutable verified bases are retained. Unsupported bases and ambiguous or missing inline parents stay unresolved, not force-matched. |
| P08 | DONE | P07 | Real public cover/timeline/files views and ordinary APIs show original prose, clickable lore footnotes, unverified participant/source chronology and exact historical ranges. Desktop 1440x1000 and mobile 390x844 show no page overflow. Same-publication replay returns PRs 1/2/3 unchanged; requeue leaves no old effects pending. Initial master refs and notification/hook counts are unchanged. Active scheduler, paired backup and recovery/monitoring handoff are recorded below. |

### Deployment State And Resume

- Server: tested parent tip `09401fd3c6`, original config/work path, user unit
  `forgejo-review.service`, public endpoint healthy. Stop/restart only that unit.
- Private rollback directory:
  `~/.local/state/lore-review-companion/backups/20261030-09401fd3c6/` contains
  `gitea.previous` and `instance.tar.gz`. Keep these together; the archive
  contains credentials and must never enter Git. This is local rollback
  protection, not an off-host disaster-recovery backup.
- Runtime: `~/.local/share/lore-review-companion/.venv`, installed from companion
  `2e12fb9`; protected separate credentials and enabled policy in
  `~/.config/lore-review-companion/`; independent source clone in
  `~/.local/state/lore-review-companion/xen.git`. The operational mail ledger
  and scheduling DB are `mail.sqlite3` and `worker.sqlite3` in that state directory.
  Policy digest is `ef5f5199c21a620c886432ff13e7ddf9aeec82af51fddecdbca253ad38853824`;
  expiry was `2026-11-06T15:21:05.869451+00:00`. Same-key uncertain retry was
  disabled. This historical policy is superseded by the committed current profile. Review expiry
  and disk space before renewing; do not extend expiry silently.
- The mandatory password change is complete. Importer 4 (`review-companion`)
  has selected-repository write access, not administration; repository 2 is
  `xen-review/xen`. The source-bound unattended grant and review-only settings
  remain mandatory. No shared SSH files were changed or credentials exposed.
- Trusted base is `e4956d5e6bdcb7e2e9abfa4474b2dc1160c51a45`, the verified
  source/review master tip at initial publication, not a guessed patch base.
  The 148-hour NNTP bootstrap discovered 411 IDs. At the inspected timer round,
  320 messages were retained, with no conflicts or source incidents, four complete
  publications and thirteen completed outbox effects, none pending/uncertain.
- Additional checkpoints: `backups/20261030-pre-initial-import/` contains a
  verified online Forgejo DB snapshot plus master refs. Paired companion snapshots
  in `backups/20261030-published-153351/` were taken under the worker lock and
  both pass integrity checks. These are private local recovery material, not
  an off-host backup; do not commit them or restore only one companion database.
- GPL-2.0-or-later adoption is complete; see the root COPYING and contribution
  terms. No package release was published. Review third-party source provenance
  and licensing whenever adding external material.
- Root space after backup/runtime installation is approximately 2.5 GiB free.
  Keep Go cache under /tmp, bound compiler memory, and monitor free space before
  enabling source ingestion. The initial Swagger process was killed under
  memory pressure; GOGC=25/GOMEMLIMIT=512MiB/GOMAXPROCS=2 succeeded. Existing
  unrelated generator/locale warnings remain documented limitations.

### Initial Xen Publications And Limits

| PR | Revision | Retained Imported Discussion |
| --- | --- | --- |
| [1](https://review-forge.be.free.or.at/xen-review/xen/pulls/1) | vPCI/MSI-X v1, three patches | Eight complete conversations; Andrew Cooper review, one exact inline range. |
| [2](https://review-forge.be.free.or.at/xen-review/xen/pulls/2) | RISC-V boot/MMU v9, one explicitly numbered patch | Two complete conversations, including a later retained author reply; no exact inline parent/range accepted. |
| [3](https://review-forge.be.free.or.at/xen-review/xen/pulls/3) | ARM64 suspend-to-RAM v13, fourteen patches | One complete conversation; Jan Beulich review, two exact inline ranges. |
| [4](https://review-forge.be.free.or.at/xen-review/xen/pulls/4) | x86/kexec Secure Boot v3, seven patches | First timer-published revision; no retained replies at publication. |

PR heads 1/2/3 are respectively `6a3dc50eaf4e6e9198e801fda0ea15b0ee7a8af9`,
`1eb50ff61ce3e453376652c301b935a06a48b1b0` and
`52179dad54769f4409ffcc07bf24998b66b08350`. Replaying each approved publication
returns the same PR and head. A later normal review request by the operator on
PR 3 is not an imported review and is left untouched.

Review bodies were checked against the original prose and source footnote;
timeline and files views retain historical multiline context, not a nearest
cumulative-head anchor. The first queue pass left four quoted remarks unresolved.
After one later RISC-V reply, the queue reports one unresolved remark on PR 1
and six on PR 2, including a missing exact accepted inline parent. Their complete
messages remain visible; none is converted into a guessed inline reply.

Known source limits remain explicit: unnumbered single patches currently have
unknown series size and remain incomplete; recursive recovery is bounded, not
full-history coverage. RISC-V v8 and nested-VMX v1 do not apply to the trusted
base and remain unpublished. Cross-posted non-Xen patches are not made applicable
by changing the base. Read-only Git preflight blockers for new revisions are
diagnostics, while source conflicts, membership drift, authorization failures,
retained-publication failures and uncertain writes still stop and latch.

The original-prose change is commit `dfb103b`. Real retained mail exposed CRLF
transport and standard in-body patch authors moved into headers by b4; `5b55475`
normalizes those representations while retaining exact title/effective-author/diff
membership and rejecting mutated diff content (13 adapter tests pass).
Commit `2e12fb9` reports only new read-only Git preflight failures as per-series
blockers; all 31 lifecycle/worker tests pass. The full 350-test source suite
passes with two deliberate server skips. All three commits were pushed normally,
without rewriting history, before their builds were installed.

Final live payload assertions match original bodies, participant names, parsed
source-time instants, signed-side commits, exact sole-parent comparisons and all
multiline range fields. Ordinary API timestamps can use local offsets, so compare
parsed instants, not serialized strings. `original_commit_id` is the ordinary
comment's old-ref field, not a promised patch parent; the scoped exact comparison
verifies the parent. Notification and hook-task counts remain zero after PR 4.

### Scheduler Handoff

The worker now runs continuously under `lore-review-companion.service`.
Check the service, journal, source backlog and available disk:

```sh
systemctl --user show lore-review-companion.service -p Result -p ExecMainStatus
systemctl --user is-active lore-review-companion.service
journalctl --user -u lore-review-companion.service --no-pager -n 30
df -h / /tmp
~/.local/share/lore-review-companion/.venv/bin/lore-review-companion list-nntp-queue \
  --ledger ~/.local/state/lore-review-companion/mail.sqlite3 --limit 12
```

To suspend imports, disable the service and revoke the repository/source
import grant through the administrator API:

```sh
systemctl --user disable --now lore-review-companion.service
```

Let any active round finish before taking paired
backups or changing policy. Preserve incident/publication/outbox evidence; after
inspection, clear only a known induced/resolved incident marker. A policy digest
change requires reviewed scheduling-state reset, never deletion of the mail
ledger. Review-only settings, a fixed trusted base, separate private tokens and
the expiry gate remain required after restarts. Monitor root space closely
(approximately 2.5 GiB free at handoff); long-term archival capacity and off-host
backup provisioning are operational follow-up, not claims of completed coverage.

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

## Published Topic Chain

All branches are pushed to the operator's Forgejo fork, not submitted upstream.
Each branch starts at the preceding topic; the last includes all changes:

| Branch | Commit | Topic |
| --- | --- | --- |
| lore-review/01-managed-participants | 00baaabdd3 | Non-login participant registry and account/credential isolation. |
| lore-review/02-scoped-grants | d024a9d4df | Explicit scopes, source/owner-bound grants and transfer revocation. |
| lore-review/03-canonical-imports | 8c6df383fc | Immutable PR/review/reply imports, chronology, audits and API. |
| lore-review/04-provenance-views | 32062dbac7 | Web/read-API provenance and generated schemas. |
| lore-review/05-import-acceptance | 09401fd3c6 | Security/isolation and actual companion-to-Forgejo acceptance. |

Companion acceptance implementation is `088caa8`; source archive correction is
`3200a88`. Documentation/policy authority was recorded in `2098719`. The full
source suite passed after the implementation commits: 347 run, 345 passed,
two deliberate server skips, 15.187s with real pinned b4. The separate eight-case
Forgejo gate includes the actual API/Git/b4/worker workflow. Hosted workflow
41051094904 passed all three Python versions; packaging commit CI also passed.
Wheel installation, pip dependency check and explicit source/wheel content
assertions passed. These historical checks do not replace production activation
gates or third-party notice review. Existing operator files and both source
snapshots remain untouched.

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

- Imported only legacy Python sources/tests and requirements, with historical
  source-context documentation and checksums. The operator later removed the
  unrelated Gitea license and obsolete provenance files when adopting the
  independent project's GPL-2.0-or-later license.
- The initial import recorded individual unchanged source hashes. Ignore rules
  exclude bytecode, environments, local mail and database state.
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

### 2026-10-30: F06 Readable Imported Provenance

- Added read-only per-object import mapping with importer/participant profiles
  and the latest 100 corrections, explicitly signaling truncation. A shared
  localized metadata partial fits existing PR/comment layouts and appears on
  covers, conversations, review headers, inline threads and exact patch views.
  Retained names are displayed independently of global username preference;
  generated logins remain distinct and identities are always unverified.
- Normal issue/PR/comment/review/range read APIs expose shared `review_import`
  provenance under existing read authorization. No broad import credential,
  source email, password or token is needed/exposed. Actual import/correction
  times stay separate from source estimates. API history retains actor, old/new
  quality/time and reason; web history is expandable and ordinary content is
  unchanged. Private-draft filtering remains in the existing path.
- Browser preview used only an explicitly opted-in loopback integration fixture.
  Screenshots and DOM checks at 1440x1000 and 390x844 showed all four root/range/
  reply blocks with loaded assets and no page-level horizontal overflow or
  metadata overflow. The preview shut down cleanly and the test passed. No live
  service, HOME, SSH or database was used. Browser inspection found generated
  login text under username preference; retained-name fallback fixed it and
  focused web/API tests reran.
- All six import API cases passed (8.086 seconds), all seven ordinary review
  cases passed (11.750 seconds), and models/issues plus services/convert passed.
  Exact historical old-side replies were verified against before..after, not
  an unrelated cumulative diff. Locale/usage lint, formatting, diagnostics,
  whitespace and all five generated read-schema assertions passed. Existing
  unrelated locale/Swagger warnings remain. Parent worktree is uncommitted.
- F06 is complete. Remaining work is C05/V01 whole-workflow acceptance, S04's
  explicit unattended policy/supervisor and V02/R01 security/operations handoff.
- Commit subject: `docs: record verified imported provenance views`.

### 2026-10-30: S04 Policy-Bound Unattended Rounds

- Added a mode-0600 owner-only, symlink-refusing, expiring local policy with
  exact numeric repo/importer and API/Git/lore/NNTP bindings, full trusted base,
  budgets, disabled notifications and isolated/unverified identity rule.
  Duplicate/unknown fields, expired/naive times, invalid budgets, moving bases
  and unsafe rules are refused. Every capability check rereads the policy and
  verifies identities/capabilities; worker clients explicitly flag requests.
- Forgejo admin grants can separately authorize unattended mode. Enabling it
  requires a review-only repository, and every flagged request rechecks it.
  Actions, active repo/inherited/system hooks, repo/owner secrets, merge/manual/
  rebase methods and default maintainer head edits block authorization. Grant
  changes require explicit revoke/regrant. Existing manual imports are unchanged.
- `run-import-worker` continuously processes discovery, publication and replies.
  Private scheduling SQLite plus an independent process lock avoid nested mail
  ledger locks. Durable revision rotation prevents new-series starvation. Only
  newly queued worker effects enroll; IDs are bound to payload fingerprints and
  the original ledger path. Existing manual/legacy work stays manual. Original
  publication/outbox records remain authoritative, with same-key uncertainty
  reconciliation permitted only by explicit policy or manual approval.
- Failures leave a private incident marker. Later timer invocations stop until
  operator inspection/recovery explicitly clears it. The disabled example and
  user-level service/timer templates are not installed or enabled. Docs cover
  enrollment-gap recovery, policy changes, backups and proven-orphan Git/SQL ref
  collisions. No automatic merge, mail sending, destructive recovery or upstream
  ref mutation is introduced; Git credential authority stays separate.
- Focused lifecycle/worker 31, queue/outbox 38 and API/client 22 tests pass.
  Full Python: 346 run, 345 passed, only live-server acceptance skipped in
  15.268 seconds; real b4 ran. All seven isolated import API cases passed in
  10.682 seconds, including the complete review-only safety matrix. Affected
  repo/service/migration packages, formatting, diagnostics, generated schemas
  and both worktree whitespace checks pass. No live policy/service was enabled.
- S04 is complete at its tested component boundary. C05/V01 require real
  companion-to-disposable-Forgejo workflow acceptance; V02/R01 remain open.
- Commit subject: `feat: add revocable policy-bound import worker rounds`.