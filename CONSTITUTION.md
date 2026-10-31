# Development Constitution

## Read First: Early Pre-Alpha

This is an early pre-alpha development experiment. There is no production
deployment, no production users, and no user-data preservation requirement.
The running Forgejo instance and `xen-review/xen` fork are development fixtures.
Do not infer production status from a public URL, running service, timer,
database, credential file, or historical use of the word "production".

This document overrides contrary project guidance in older plans, handoffs,
installation notes, and agent memories. A future production phase requires an
explicit committed amendment; agents must not invent that transition.

## Source Is Authoritative

- Only source counts. Commit each coherent, tested topic and push immediately
  after every commit. Do not leave completed work only in a running instance.
- Commit all instructions, operational policy, deployment configuration and
  bootstrap/rebuild procedures. Runtime policy must use the committed version,
  not a private or independently maintained policy copy.
- Credentials are not policy. Passwords, tokens, private keys and other secrets
  must remain outside Git and must never appear in logs, plans or documentation.
- Record actual verification results and known gaps. Do not call code tested
  merely because it compiles, nor call a development service production.
- Preserve unrelated operator source edits. Disposable runtime state does not
  authorize reverting someone else's uncommitted code.

## Disposable Development Runtime

- Do not create or enforce persistent incident latches. A failed round reports
  its error, retains useful diagnostic/retry state, and allows the next round to
  try again. Old incident-marker files have no authority.
- Retain deterministic identity, provenance, authorization and uncertain-write
  checks: removing a latch is not permission to leak credentials, target an
  unrelated repository, force mismatched refs or duplicate an uncertain write.
- Scheduling state is a cache, not a separate policy authority. Reconcile it
  automatically with committed policy while retaining useful effect enrollment.
- The `xen-review/xen` fork, associated import/scheduler state, old comments and
  conflicting experimental data may be deleted and rebuilt from `xen-project/xen`
  when useful for testing bootstrap or current source. Use a named, explicit
  rebuild operation scoped to that development fork; never delete the upstream
  `xen-project/xen` mirror or unrelated repositories. Do not impose production
  backup/incident-recovery ceremonies on this experiment.
- A rebuild must exercise committed bootstrap code and document its outcome.
  Fix bootstrap defects in source, test, commit and push before depending on the
  repaired runtime. Optional diagnostic backups must not become recovery gates.

## Import Contract

The companion, not an AI agent, autonomously imports complete submissions and
their retained review replies. Accept parents before their children, retain
complete source conversations, and never guess inline anchors. A confirmed PR
alone is not proof of complete discussion delivery.

- Never defer or silently omit a source comment because inline placement is
  unavailable or ambiguous. Post an unanchored remark as a standalone PR
  comment. When quoted text matches multiple accepted comments, select the
  match with the latest source timestamp strictly before the reply's own
  timestamp. When disambiguation is needed, unknown times, equal-time ties or
  no earlier match require a standalone comment, not a guessed anchor.
- If comment creation fails, the last resort is a PR comment authored by the
  companion itself, not by the source sender. Include the original source
  reply link, the attempted API method/path and sanitized request details, and
  the failure returned. Retain durable, idempotent retry state for both the
  original effect and the diagnostic comment. API unavailability is not
  successful delivery: report it as a blocker and retry without duplicating
  uncertain writes. Never include credentials in diagnostic comments.

## Reporting Contract

- Establish the exact entity before using shorthand such as "candidate",
  "reply", "incomplete" or "blocked". Distinguish submission revision/PR
  creation from source-email import, quoted-remark placement and API delivery.
- Report each specific incomplete or blocked instance, with its title,
  revision, Message-ID/source link, failed operation and concrete cause. Counts
  or unnamed aggregate warnings are not an adequate report. Distinguish
  observed snapshots from current or exhaustive coverage.
- End every user-facing turn with the remaining blockers and affected
  instances, or explicitly state that there are no known blockers. Do not
  claim unfinished, untested, uncommitted or undeployed work is complete.

Use 240 hours as the initial source-lookback default, not a fixed rule. Keep it
overridable in committed policy and CLI options. Preserve truthful distinctions
between retained source, archive discovery and verified coverage.

## Deployment

Deploy committed, tested source to the development instance and keep its
operational configuration synchronized with the repository. Restarting services,
reconciling scheduling state and clearing obsolete incident artifacts are normal
authorized development work. Report deployed revisions and verification, without
claiming that source-only changes are already running.