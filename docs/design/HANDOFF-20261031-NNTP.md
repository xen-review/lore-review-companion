# NNTP Activation Handoff

Read [../../CONSTITUTION.md](../../CONSTITUTION.md) before changing runtime or
source. This is an early pre-alpha disposable development fixture. The
authoritative source is the pushed companion `bff0015`; the known untracked
operator file `prek.toml` is not part of the source changes.

## Live State

- Forgejo is serving `b51da217df` and is active.
- `xen-review/xen` was explicitly rebuilt from `xen-project/xen` as repository
  ID 4. Its default branch is `staging`, pinned at
  `8803b82ab2c95da09c837482cbcac11d7f3853e5`.
- The review fork has issues and pull requests enabled. Actions, merge methods,
  rebase/update, and maintainer-edit settings are disabled.
- Unattended review-import capability is `true` for repository/importer ID 4,
  source `https://lore.kernel.org/xen-devel/`. The `review-companion` account
  is already in the `xen-review` Owners team.
- The resident service is enabled and active with:
  `run-import-worker --forge-path /home/forge/forgejo`.
- Temporary administrator tokens used for rebuild and repair were revoked and
  their private files removed. Only the scoped importer and Git token files
  remain under the private runtime configuration directory.

## Observed Bootstrap

NNTP TLS connectivity is live and the worker has created new PRs and blocking
issues from Lore. The latest snapshot observed 45 PRs and 15 issues. Blocking
issues use exact Message-ID titles, Lore source links, and concrete reasons.
Durable worker cursors were `source_cursor=0`, `publication_cursor=0`, and
`revision_cursor=60` while the 240-hour bootstrap was still draining. This is
an observed snapshot, not an archive-coverage claim.

The worker previously surfaced source-specific b4 assembly failures as top-level
errors. Companion `0dd6110` and `bff0015` now classify those failures as
idempotent Message-ID issues and continue the round. After the `bff0015` restart
at 2026-10-31 23:33:41, no new application error had appeared in the observed
log window; the process remained active and connected to NNTP.

## Validation

- Companion suite: 433 tests passed, 4 expected skips.
- b4 adapter suite: 13 tests passed, 2 expected skips.
- Xen setup suite: 8 tests passed.
- Deployment helper suite: 3 tests passed.
- Focused Forgejo review-import integration suite: passed.
- `git diff --check`: clean for all committed changes.

## Next Observation

Leave the resident service running while the bootstrap drains. Confirm a
completed round emits normal JSON, then observe that the worker waits ten
minutes before its next Lore discovery while still draining internal pages
without an arbitrary per-run import cap. Recheck fresh replies on existing PRs
after the next Lore poll. Do not claim complete archive coverage from the
current snapshot.

Weekly usage-limit percentage is not exposed by the available tooling, so the
90 percent stop condition could not be measured directly in this session.