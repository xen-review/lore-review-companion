# Development Runtime

Read [../CONSTITUTION.md](../CONSTITUTION.md) first. This is a disposable early
pre-alpha experiment with no production users. Do not reintroduce incident
latches or a private policy copy.

## Public Policy And Secret Credentials

The authoritative runtime policy is
[../lore_review_companion/policies/xen-development.json](../lore_review_companion/policies/xen-development.json).
It is version controlled and included in the installed package. The worker and
on-demand tools use that profile by default. Another deployment may select a
different committed profile with `--policy-file`; do not maintain a home-directory
policy fork. Policy is public JSON, normally mode 0644. Runtime locks/databases
may remain owner-only implementation files, not hidden policy authorities.

The Xen profile uses an overridable 240-hour window and no time expiry
(`expires_at: null`). It enables idempotent same-key retries for uncertain
operations. It never permits guessing content, disabling provenance checks or
posting to a different repository. Identity mismatches still report errors.
Commit and push profile changes, then reinstall the package. The scheduler
automatically reconciles its policy digest/cursors and preserves enrolled
effects. No manual incident-recovery approval is required.

Tokens/passwords remain in `~/.config/lore-review-companion/`, mode 0600.
They are credentials, not policy, and must not be committed or printed.
Forgejo's credential-bearing configuration also stays outside Git. The deployment
procedure, service units, resource limits and operational decisions are public.

## Deploy Committed Source

Run unit and isolated integration tests, commit each coherent topic and push
immediately. From the companion checkout:

```sh
.venv/bin/python contrib/deploy-development.py \
  --forgejo-root "$HOME/forgejo" \
  --forgejo-config /path/to/current/app.ini
```

The default previews the exact source revisions and profile without writes.
Add `--apply` to deploy. Both tracked worktrees must be clean and their heads
must match their upstream refs. Unrelated untracked operator files are ignored.
The script stops companion scheduling, builds matching Forgejo backend/frontend
assets, installs a revision-specific server release and stamped companion
package, verifies installed policy bytes and b4, updates the checked-in service
units, removes obsolete incident/policy artifacts, restarts Forgejo, checks its
version endpoint, runs a worker round, and enables the timer after success.
No administrator credential or privilege escalation is required for an existing
development installation. Failures are reported and can be retried; no incident
marker is created.

Inspect service logs with `journalctl --user -u lore-review-companion.service`
and check actual PRs, provenance, reply counts and deployed build stamps. A
successful build alone is not evidence of a successful import.

## Disposable Fork Rebuild

The user authorizes explicit deletion and recreation of `xen-review/xen` from
`xen-project/xen` when useful to remove conflicting experimental data and test
bootstrap. It is not mandatory on every deployment. Do not delete the upstream
mirror or any unrelated repository. Stop the companion first; reset the matching
import and scheduling state together rather than retaining stale remote IDs.
Recreate the fork through the committed setup tool and re-provision its scoped
tokens/grant. Fresh numeric repository IDs must be updated in the committed
profile, tested, committed and pushed before resuming autonomous imports.
Optional diagnostic snapshots must not turn this into a production recovery
ceremony. Record what was rebuilt and which bootstrap paths were actually tested.