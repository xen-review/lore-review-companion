---
name: lore-review-development
description: 'Use when developing, testing, deploying, debugging, or handing over the Lore Review Companion and Forgejo review-import integration.'
---

# Lore Review Development

## Authority and scope

Read the companion root `CONSTITUTION.md` first. For Forgejo changes also read
the parent checkout's `AGENTS.md` and `DEVELOPMENT-CONSTITUTION.md`. These
supersede older handoffs and production or incident-latch assumptions. The
running instance and `xen-review/xen` are disposable early pre-alpha
development fixtures, not production. Do not assume a dated handoff describes
the current service, policy, repository, or credentials.

The companion and Forgejo are separate Git repositories and remotes. Before
editing, inspect each relevant worktree, branch, upstream and nearby tests.
Preserve user changes and untracked files; stage only task-owned changes. Once
each coherent topic has focused tests, commit it in its own repository and push
immediately before starting the next topic. Do not amend or rewrite published
history. Report any failed push or uncommitted work explicitly.

## Find current facts

1. Start with `docs/design/HANDOFF-20261107.md`, the current committed policy
   `lore_review_companion/policies/xen-development.json`, and the source/tests
   for the task. Historical design notes contain useful evidence but may have
   obsolete IDs, timer states, or deployment claims.
2. For operations, inspect `systemctl --user status --no-pager` and
   `journalctl --user --no-pager` for `forgejo-review.service` and
   `lore-review-companion.service`, current process arguments, the policy, and
   actual capabilities. Confirm the repository ID, importer and source before
   any writes. Never print credentials; token paths are not token values.
3. A continuously running worker polls rather than subscribing to NNTP. It
   drains backlog, reconciles stored revisions and replies, and sleeps after
   idle or failed rounds. Read `docs/worker.rst` and the actual worker source
   for current timing and behavior. A running service does not prove imports
   are succeeding.
4. Before a separate round or bootstrap using the same worker-state file, stop
   the resident worker and confirm the worker lock is available. Restart only
   after the exclusive process has exited. Preserve idempotency, provenance,
   authorization and uncertain-write checks; never add a persistent incident
   latch or invent an operational approval gate.

## Test and deploy

Use the repository's prepared Python environment and the commands in `README.md`
and `docs/DEVELOPMENT.md`. Typical focused and full checks from this repository:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_series_sync.py'
env -u LORE_FORGE_ACCEPTANCE -u LORE_FORGE_ACCEPTANCE_DELETIONS \
  -u LORE_FORGE_ACCEPTANCE_B4_PYTHON -u LORE_FORGEJO_IMPORT_FIXTURE \
  .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m sphinx -n -W -b html docs /tmp/lore-review-docs
```

Sphinx requires `docs/requirements-docs.txt`; optional integration tests have
their own prerequisites and opt-ins. Run focused checks for the actual change;
report skips and unavailable checks rather than assuming they pass. Review the
project's current test instructions before running tests against a live system.

Deploy only committed, pushed source using `contrib/deploy-development.py` and
`docs/DEVELOPMENT.md`; verify actual installed revisions and service behavior.
The script restarts both services and requires clean tracked worktrees and
matching upstream heads. Never report a source commit as deployed without
verification. Do not alter repository safety settings, grants or tokens merely
to bypass a 403; inspect current state and review the impact with the user if
it may undo an intentional configuration change.

An explicitly scoped rebuild of `xen-review/xen` and its development state is
permitted when useful under the constitution. Use the committed setup tool,
check identities, and never delete `xen-project/xen` or unrelated repositories.
Update the public policy and commit/push it when identities change. Credentials
are private and must not enter source, documentation, shell arguments or logs.

## Handover

Update a dated committed handover when operational facts materially change;
include exact dates, verified revisions, tests, affected instances and concrete
blockers, and label snapshots as such. Link to existing evidence instead of
copying large historical reports. Do not claim full archive coverage or successful
imports from service status alone.