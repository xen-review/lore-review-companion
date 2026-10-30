<!-- SPDX-License-Identifier: GPL-2.0-or-later -->

# Install And Bootstrap Xen Reviews

This example creates a read-only upstream mirror and a separate writable review
fork, then imports recent `xen-devel` submissions and discussion from lore. Each
complete revision becomes an immutable PR authored by its mapped submitting
participant. Reviewers have their own participant profiles. Neither these
profiles nor their source addresses gain login credentials or permissions.

## Prerequisites

- Linux, Python 3.11 or newer, Git, `uv` and outbound HTTPS/TLS NNTP (port 563).
- An already installed **review-import-enabled Forgejo fork**. Stock Forgejo
  lacks the required scoped import API. Use the operator's
  [lore-review/05-import-acceptance branch](https://codeberg.org/bkaindl/forgejo/src/branch/lore-review/05-import-acceptance),
  or a later reviewed descendant. Build with `make TAGS="sqlite sqlite_unlock_notify" build`
  for SQLite. Configure/start Forgejo normally, including its first site admin,
  HTTPS, repository storage and backups, before running the companion scripts.
- Enough disk for the Forgejo mirror, its fork, an independent bare assembly
  clone, retained raw mail and recovery snapshots. Budget multiple GiB and
  monitor growth; this is not a full-history archive deployment.
- No active global/inherited/system hooks or owner/repository secrets on the
  review destination. The script disables its Actions and merge/update/head-edit
  settings. The server refuses unattended grants if other hazards remain; the
  script does not silently delete hooks or secrets.

Install from the companion checkout:

```sh
git clone https://github.com/xen-review/lore-review-companion.git
cd lore-review-companion
uv venv --python python3 .venv
uv pip install --python .venv/bin/python -e . -r requirements-b4.txt
.venv/bin/lore-review-companion check-b4-runtime
```

The runtime check verifies pinned b4/liblore versions **and source fingerprints**.
Do not substitute an arbitrary installed b4. Assembly is offline and disables
added trailers/partial rerolls; lore discovery is a separate bounded operation.
For manual testing, invoke the installed scripts with `.venv/bin/python`, not an
unrelated Python on PATH. `uv run --no-sync` also uses this prepared environment;
plain `uv sync` can remove b4 because it is an externally pinned assembly runtime.

## Administrator Provisioning

Create a temporary site-admin API token through Forgejo. Required categories are
`write:admin`, `write:organization`, `write:repository` and `read:user`. Store it
directly in a private file without putting its value in shell arguments/history:

```sh
install -d -m 700 "$HOME/.config/lore-review-companion"
umask 077
read -rsp 'Temporary Forgejo administrator token: ' ADMIN_TOKEN
printf '\n'
printf '%s\n' "$ADMIN_TOKEN" > "$HOME/.config/lore-review-companion/bootstrap-admin-token"
unset ADMIN_TOKEN
```

Do not share this value with an assistant, commit it or use it as the worker's
credential. Replace the example URL and use an operator-controlled integration
email. The script's default is a plan with **no API calls or local writes**:

```sh
.venv/bin/python contrib/setup-xen.py \
  --api-url https://forge.example/api/v1/ \
  --admin-token-file "$HOME/.config/lore-review-companion/bootstrap-admin-token" \
  --email review-companion@example.org
```

Add `--apply` to provision. The script performs these API operations:

1. Creates `review-companion` with a strong generated initial password stored
   only in the private `initial-password` file. It is an integration user, not a
   site administrator. Existing accounts are not password-reset or adopted if
   their identity differs. Account security gates are never cleared on rerun.
2. Creates the public `xen-project` organization and migrates
   `https://github.com/xen-project/xen.git` to `xen-project/xen` with `mirror:true`.
   It does **not** send `mirror_interval`, prune, branch or other optional
   migration settings, leaving Forgejo's configured defaults in effect.
3. Creates `xen-review`, adds `review-companion` to its `Owners` team as requested,
   and forks `xen-project/xen` as writable `xen-review/xen`.
4. Disables unsafe review-fork settings and creates a source-bound unattended
   import grant for `https://lore.kernel.org/xen-devel/`.
5. Creates **separate repository-selected** API-import and Git tokens, writes
   them mode 0600, checks their effective permissions, clones a bare assembly
   repository and records the current full mirror commit as the trusted base.
6. Writes a private seven-day policy with budgets of 32 messages and two
   revisions per round, a 148-hour initial lookback and uncertain retry disabled.

Owners membership grants the integration account organization-wide authority.
The generated runtime tokens are still restricted to `xen-review/xen`; do not
replace them with an unrestricted token. Imported submitter/reviewer profiles
are never added to Owners. A lower-privilege alternative is an ordinary write
collaboration, but that is not the organization topology this example creates.

Provisioning is resumable, not transactional across HTTP and Git. It validates
existing mirrors/forks and preserves existing credentials/policies. A timeout
may mean the server accepted the request; inspect existing objects and rerun,
not delete them. If a token was created but its response was lost, inspect/revoke
that named token in Forgejo before regenerating it. No history is force-pushed.

## Bootstrap Recent Submissions

On a **fresh deployment**, authorize the expiring policy and run a finite number
of rounds in the same provisioning invocation:

```sh
.venv/bin/python contrib/setup-xen.py \
  --api-url https://forge.example/api/v1/ \
  --admin-token-file "$HOME/.config/lore-review-companion/bootstrap-admin-token" \
  --email review-companion@example.org \
  --apply --enable-policy --bootstrap-rounds 8
```

The console equivalent is `lore-review-setup-xen`. Migration/clone may take time.
Each round prints counts and blockers, without credentials. Recent IDs and older
parents are fetched into a durable queue; complete applicable revisions become
PRs. Later rounds revisit discussion after parent effects are accepted. Eight
rounds are a bounded start, **not a promise of full thread/window coverage**.

Existing policies are deliberately preserved on rerun, including disabled state,
expiry and trusted base. If you first provisioned without `--enable-policy`,
review and explicitly edit the private policy to enable it before running the
worker. Do not silently renew an expired policy. A changed policy digest requires
reviewed scheduling-state reset; never erase the authoritative mail ledger.

Run additional bounded rounds using only the generated runtime tokens:

```sh
.venv/bin/lore-review-companion run-import-worker \
  --policy-file "$HOME/.config/lore-review-companion/import-policy.json" \
  --ledger "$HOME/.local/state/lore-review-companion/mail.sqlite3" \
  --repository "$HOME/.local/state/lore-review-companion/xen.git" \
  --state "$HOME/.local/state/lore-review-companion/worker.sqlite3" \
  --token-file "$HOME/.config/lore-review-companion/import-token" \
  --git-token-file "$HOME/.config/lore-review-companion/git-token"
```

Inspect PR authors, original text and lore links, exact ranges and unresolved
diagnostics. The fixed base may reject already-applied patches or dependent
series; choose a newly reviewed base policy, never guess or force application.
Unknown-size unnumbered submissions remain incomplete. Ambiguous/missing inline
parents never become guessed code comments. Raw mail remains in the ledger.

Revoke the temporary administrator token in Forgejo once provisioning is done.
Delete its local file only after revocation. API cleanup for token-authenticated
administrators is `DELETE /api/v1/admin/users/{username}/tokens/{id}`; the ordinary
`/users/.../tokens` route requires Basic authentication. No admin token is needed
for subsequent imports. Keep the initial password private or rotate it through
normal account administration. Do not publish it with setup output.

## Import One Thread On Demand

`contrib/import-xen-thread.py` (console equivalent `lore-review-import-xen`)
replaces ad hoc operator Python snippets. It uses the private deployment config
and separate runtime tokens, but **manual writes are authorized individually by
digest**, not by enabling a timer. Use this for bootstrap inspection and testing
only in the destination you intend to modify. Automated tests use fake APIs or
the disposable Forgejo acceptance fixture, never production data.

Choose a retained cover/patch Message-ID and its explicit revision:

```sh
SEED='cover.1793129971.git.mykola_kvach@epam.com'
REVISION=13
.venv/bin/python contrib/import-xen-thread.py --seed "$SEED" --revision "$REVISION" plan
```

If dependencies are missing, initialize/fetch the recent NNTP queue as described
in the README. `recover` can then discover/fetch a bounded thread whose anchor
was already received from that group. Recovery writes local ingestion state,
not PRs; upstream HTTPS unavailability remains an explicit blocker.

The plan runs against a private ledger snapshot and prints the full publication
request and its `approval_digest`. Inspect it, then supply that exact digest:

```sh
.venv/bin/python contrib/import-xen-thread.py --seed "$SEED" --revision "$REVISION" \
  --approve REVIEWED_PUBLICATION_DIGEST publish
.venv/bin/python contrib/import-xen-thread.py --seed "$SEED" --revision "$REVISION" queue
.venv/bin/python contrib/import-xen-thread.py --seed "$SEED" --revision "$REVISION" \
  --kind conversation plan-delivery
.venv/bin/python contrib/import-xen-thread.py --seed "$SEED" --revision "$REVISION" \
  --kind conversation --approve REVIEWED_CONVERSATION_DIGEST deliver
```

Inspect and approve `--kind review` separately. Run `queue` again after accepted
parents, then inspect and approve `--kind reply`. Repeat bounded queue/delivery
passes until up to date; unaccepted parents are deferred, not guessed. Use the
same `--limit` when planning and delivering. Changed evidence/digests stop writes.
Existing immutable publications replay their original bytes, even if their old
presentation has fences or footnotes. This script never rewrites old imports.

## Scheduling And Recovery

No script automatically installs/enables systemd. After a successful inspected
round, adapt the [service](../contrib/lore-review-companion.service) and
[timer](../contrib/lore-review-companion.timer) to your actual interpreter/config/
state paths. Review filesystem protections, then install them in your user unit
directory, verify with `systemd-analyze --user verify`, reload the user manager,
and enable the timer explicitly. User lingering may require administrator setup.

Stop scheduling with `systemctl --user disable --now lore-review-companion.timer`
and revoke the source grant to stop future writes. A worker incident latch is
intentional: inspect uncertain publication/outbox state before clearing it. Back
up `mail.sqlite3` and `worker.sqlite3` together with the worker idle/locked; use
SQLite's backup API, not a copy of a live database file. Also back up Forgejo's
configuration, database and repository storage. Keep off-host backups, monitor
disk space and policy expiry, and retain raw-mail/conflict evidence.

## Developer Checks

From the checkout, after installing the runtime above:

```sh
uv check --no-sync
uv format --check
uv format
env -u LORE_FORGE_ACCEPTANCE -u LORE_FORGEJO_IMPORT_FIXTURE \
  LORE_FORGE_ACCEPTANCE_B4_PYTHON="$PWD/.venv/bin/python" \
  uv run --no-sync python -m unittest discover -s tests -p 'test_*.py'
```

`uv check` invokes `ty` type checking, not unit tests or formatting. The initial
repository-wide type-check baseline reported 565 diagnostics in existing dynamic
code/test fixtures; it is **not yet a passing gate**. Do not suppress these to
claim success. After the new-script guards the current count is 564.
`uv format --check` and unit tests are required checks. Keep
repository-wide formatter changes in a dedicated commit. Prek hook/environment
configuration is being supplied separately by the operator.