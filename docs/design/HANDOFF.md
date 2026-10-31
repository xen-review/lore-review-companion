# Import Work Handoff

Status captured 2026-10-31. Recheck live state before any write.

## Current Implementation And Review

This section supersedes the earlier implementation limitations below.

- New imports must be autonomous, not completed by an AI or an operator queueing
  each reply. The policy worker discovers a selected submission thread, fetches
  its missing candidates within the configured budget, and defers publication
  until those candidates are retained. Unrelated NNTP backlog does not prevent
  that thread becoming ready. Before publication it validates retained reply
  ancestry; after publication it queues/delivers every retained reply in explicit
  parent-first order, draining all effects in bounded API batches. Existing PRs
  continue receiving later replies. No archive completeness claim is made.
- `refresh-import` now provides an audited in-place full-PR presentation rebuild.
  It requires a retained publication preview and a private digest-bound refresh
  plan. The server verifies the full snapshot, original operations and exact
  immutable refs, preserves IDs/ancestry/human discussion, and commits all body
  corrections atomically without notifications. Original imports remain
  replayable, and future source-verified replies accept audited parent changes
  but not unaudited edits. Missing retained replies block refresh until automatic
  import completes. No replacement is needed or automatically attempted.
- Forgejo server implementation is committed and pushed as `9bb6521eaf`; the
  post-refresh parent-hash compatibility fix is `8c3229016b`. Configurable
  240-hour defaults are committed and pushed in companion `f47a1af`; canonical
  conversation/operation-key queue fixes are pushed as `4a4ea0e`.
  Companion refresh CLI and autonomous complete-import orchestration are pushed
  as `b42bc70`.
- Verification includes 393 companion tests (four expected skips), the scoped
  Forgejo import API suite, direct refresh CLI preview/write/digest checks and
  the enabled disposable real-server companion workflow, including inline
  review refresh and original-operation replay. No production deployment is
  implied by those passing checks.
- 240 hours is only the initial default. Override `--lookback-hours` or policy
  `lookback_hours` within the supported 1-8760-hour range. Widen a durable
  checkpoint with explicit backfill; preserve forward progress and retained
  mail. Existing policy/checkpoint mismatches still require reviewed scheduling
  reconciliation, not a silent policy edit.
- The live server still lacks `pull-refresh` capability; the new server and
  companion code have not been installed or activated. The worker incident
  latch and protected manual delivery 42 remain untouched. Do not claim the
  running timer already uses the new behavior.

### LZ4 v2 Publication Plan Review

Regenerated read-only from the operational ledger and configured trusted base:

- Status: `publication_ready`; title: `Marcus Granado: tools: optional LZ4
  compression of the libxenguest migration stream [v2]`.
- Ten retained submission messages form the cover and nine-patch revision.
  All nine patch commits form one exact parent chain from base
  `e4956d5e6bdcb7e2e9abfa4474b2dc1160c51a45` to head
  `ed6697e3f49e55c586cd2995158b67c13a3da350`.
- Paired refs are `lore/dc668b15bc307a5f99617d85ed7856a5dfd532382b1d89154301b9e02e733f90/v2-base`
  and the same prefix's `/v2`. Both are planned, not already created. Matching
  immutable refs can be reused; mismatches must still stop publication.
- The digest is unchanged:
  `eeedd12a8e00ef39e691ac5ec3666da1bf545549ae49846848ae563dbd2381d6`.
  This review is not approval to publish.
- Nine retained replies from Teddy Astie each explicitly name their corresponding
  patch as immediate parent. They must be imported automatically after confirmed
  publication. Actual conversation/review IDs and exact server delivery plans
  are unavailable until a PR exists; no inline placement is asserted here.
- Publication is not an atomic transaction with discussion delivery. The worker
  retains effects for recovery and stops on uncertain writes. Do not confuse a
  confirmed PR with completed discussion if a round fails mid-import.
- Operational blockers remain: installed runtime is older, policy says 148
  hours while the durable checkpoint is 240, worker is incident-latched, and
  manual PR 6 delivery 42 must not be auto-adopted. Review paired state/backups,
  policy digest and enrolled effects before deployment and recovery. No remote
  PR/comment/ref write occurred during this review.

## Requested Work

- Implement an explicit CLI to refresh an existing imported open PR in place,
  rebuilding its description and comments from retained mail. If that cannot be
  supported, offer an explicitly approved close-and-recreate fallback. Preserve
  provenance and do not silently remove unrelated or locally edited discussion.
- Reuse existing immutable revision refs when their commits match; never
  force-update mismatched refs. A real-Git test already verifies matching paired
  refs are reused and divergent refs are rejected.
- Import Marcus Granado's LZ4 v2 series with its replies once the publication and
  delivery plans have been reviewed. The 240-hour source expansion is done.

## Historical Limitations At Initial Handoff

The full PR refresh/recreation CLI is **not implemented**. `recreate-import`
creates one additional conversation effect; it does not remove old comments or
recreate a PR. The server's existing presentation correction endpoint changes
only PR title/body. Deleting imported comments would strand import-operation
records and break replay; the existing source-message binding also prevents
simply creating a second PR for the same imported source. The design and safety
requirements are in [PLAN.md](PLAN.md#explicit-pr-refresh-planned).

The new extension is committed as `569ba81`; it has not been installed in the
scheduled runtime. The operational checkpoint has been widened to 240 hours,
but the private worker policy still specifies 148 hours. The user timer is
active and the service is failed behind `worker.sqlite3.incident`, originally
caused by an NNTP ARTICLE 430. Do not clear that latch, restart the worker,
silently alter policy or delete scheduling/mail history. First inspect durable
publication and outbox states, reconcile the changed policy digest and worker
enrollment/cursors with the documented recovery procedure, and deploy/test a
reviewed runtime. The only pending outbox effect at capture is manual PR 6
comment recreation delivery 42; do not let the worker auto-deliver it.

The companion worktree had a pre-existing untracked `prek.toml`; leave it
untouched. The outer Forgejo worktree also has pre-existing untracked files and
the companion repository itself is untracked there. Do not stage/revert them.

## Operational Evidence

- Ledger: `~/.local/state/lore-review-companion/mail.sqlite3`; scheduler state:
  `~/.local/state/lore-review-companion/worker.sqlite3`. Before extension,
  paired private SQLite backups passed integrity checks in
  `~/.local/state/lore-review-companion/backups/pre-window-240-yhvh2j60/`.
- Checkpoint: bootstrap `2026-10-30T15:08:02+00:00`, cutoff
  `2026-10-20T15:08:02+00:00`, forward `checked_through`
  `2026-10-30T16:16:05+00:00`, window 240 hours. The scan found 619 IDs and
  queued 142 new IDs; after fetching LZ4, 271 IDs were pending. Index time is
  not verified receipt time and coverage is not guaranteed complete.
- Ledger status after fetching LZ4: 432 retained messages, zero conflicts or
  recorded NNTP incidents, six complete publications, 41 complete effects and
  one pending effect. No service restart, remote PR/comment write or latch
  removal occurred during the checkpoint work.
- LZ4 v2 cover Message-ID:
  `20261020200238.1815766-1-marcus.granado@citrix.com`. All ten messages
  (cover and nine patches) are retained, with nine related replies. A read-only
  preview against configured base `e4956d5e6bdcb7e2e9abfa4474b2dc1160c51a45`
  returned `publication_ready`, head `ed6697e3f49e55c586cd2995158b67c13a3da350`,
  and digest `eeedd12a8e00ef39e691ac5ec3666da1bf545549ae49846848ae563dbd2381d6`.
  Regenerate and inspect the plan before seeking approval; the digest is not a
  standing authorization. Replies require a confirmed PR before queue/delivery.
- `python -m unittest discover -s tests -p 'test_*.py'`: 390 tests passed,
  four skipped. Scoped formatter checks and editor diagnostics passed.

## Historical Suggested Next Steps

1. Recheck status, incident logs, policy and operation mappings. Keep the latch
   in place until the documented recovery gate is satisfied.
2. Design and implement the auditable server-side refresh/replacement contract,
   then the CLI and regression/integration tests; handle human comments,
   changed bodies, ancestry, old operations, retries and notification suppression.
3. Review LZ4's current publication plan, obtain exact approval, publish only
   through the authorized gate, then queue and review its conversation, review
   and reply effects. Verify the resulting PR and provide its link.
4. Update the policy and scheduling state only under an explicitly reviewed
   recovery plan, install the tested companion runtime, then verify bounded
   time-based worker rounds and reply coverage.