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

To run one bounded import round immediately, then inspect its result:

```sh
systemctl --user start lore-review-companion.service
journalctl --user -u lore-review-companion.service -n 20 --no-pager
```

The service uses the committed policy's message and revision limits and the
persisted scheduling cursors. One successful round does not imply that every
retained submission was considered; later service runs or timer ticks continue
through the queue.

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

Use the setup tool's explicit rebuild mode, which defaults to preview:

```sh
.venv/bin/lore-review-setup-xen \
  --api-url https://review-forge.be.free.or.at/api/v1/ \
  --admin-token-file /path/to/temporary-admin-token \
  --email mail@example.com --rebuild-development-fork
```

Add `--apply` to execute. The tool validates the committed repository ID and
upstream mirror before writes, stops scheduling, deletes only the development
fork, revokes its named setup tokens, retires the entire old state directory
under a unique sibling name and recreates the fork. It prints the new repository
ID without modifying policy. Update that ID in the committed public profile,
test, commit and push. Run the same setup command with `--apply` but without
`--rebuild-development-fork` to provision fresh selected-repository tokens,
grant and assembly clone, then deploy normally. A rebuild never resumes the
timer itself. The temporary administrator token is a secret and should be
revoked after setup.

## 2026-10-31 Submission And Comment Findings

The earlier phrase "incomplete or blocked candidates" referred to **submission
revisions whose PR creation was blocked**, not failed source-reply imports.
The following are the specific instances observed in the worker journal and
rechecked against retained mail. This is a bounded investigation, not a claim
that every retained submission has been verified.

### Previously Reported Incomplete Submission Revisions

| Submission | Source | Verified cause and current result |
| --- | --- | --- |
| xen/arm: psci: Do not keep a rejected PSCI version in psci_ver, v1 | [Ayan Kumar Halder](https://lore.kernel.org/xen-devel/20261021132640.599126-1-ayan.kumar.halder@amd.com/) | Singleton discovery repaired. [PR #16](https://review-forge.be.free.or.at/xen-review/xen/pulls/16) contains one rejected hunk in xen/arch/arm/psci.c.rej. |
| x86/nestedsvm: Record NextRIP with the deferred #VMEXIT, v1 | [Stephen Cheng](https://lore.kernel.org/xen-devel/20261022090658.151682-1-stephen.cheng@citrix.com/) | Singleton discovery repaired. [PR #20](https://review-forge.be.free.or.at/xen-review/xen/pulls/20) applies cleanly to staging. |
| tools/ocaml: bindings needed to build ARM domains, v1 | [source thread](https://lore.kernel.org/xen-devel/1792679039.8631fc262581453bbf619ec5b2062170.1a163ff5306000f79f@vates.tech/) | Two different cover messages share the same subject, while both patches name an absent parent. The correct cover cannot be established; see the [message-by-message investigation](design/ocaml-arm-cover-investigation.md). |
| x86/vpt: Validate timer object with generation number, v1 | [source](https://lore.kernel.org/xen-devel/1792717952.8631fc262581453bbf619ec5b2062170.1a166511467000f79f@vates.tech/) | Singleton discovery repaired. [PR #17](https://review-forge.be.free.or.at/xen-review/xen/pulls/17) contains five rejected hunks across two .rej files. |
| common: dom0less-bindings: introduce XSM labels, v5 | [Sergiy Kibrik](https://lore.kernel.org/xen-devel/20261023110933.2325318-1-Sergiy_Kibrik@epam.com/) | Singleton discovery repaired. [PR #21](https://review-forge.be.free.or.at/xen-review/xen/pulls/21) applies cleanly to staging. |

### Previously Reported Patch-Application Blockers

Rechecked against committed base `8803b82ab2c95da09c837482cbcac11d7f3853e5`.
No source patches were rewritten and no alternative base was guessed.

| Submission | Source | Concrete blocker |
| --- | --- | --- |
| tools/ocaml: implement stub_xc_domain_create() for ARM, v2 | [source](https://lore.kernel.org/xen-devel/1792658248.8631fc262581453bbf619ec5b2062170.1a162c2110b000f79f@vates.tech/) | Strict application failed at tools/ocaml/libs/xc/xenctrl_stubs.c:257. [PR #19](https://review-forge.be.free.or.at/xen-review/xen/pulls/19) contains one rejected hunk in xenctrl_stubs.c.rej. |
| x86/nestedvmx: Fix nested VMX bugs, v1 | [source](https://lore.kernel.org/xen-devel/cover.1792997742.git.chunjie.zhu@citrix.com/) | Strict application failed at patch 4, xen/arch/x86/mm/p2m.c:1675. [PR #18](https://review-forge.be.free.or.at/xen-review/xen/pulls/18) retains all eight patch commits and three rejected hunks in two .rej files. |
| xen/riscv: fix boot on missing extensions and MMU setup bugs, v8 | [source](https://lore.kernel.org/xen-devel/1793027589.8631fc262581453bbf619ec5b2062170.1a178c5c430000f79f@vates.tech/) | Strict application to staging failed. [PR #8](https://review-forge.be.free.or.at/xen-review/xen/pulls/8) applies cleanly to source base 46678752be2d97f61317d793408a59927a429847 and warns that it does not apply to staging. |

The published PSCI, VPT and nested VMX branches were independently reassembled
from retained mail; each reconstructed head exactly matched the live PR head.

### Remaining Retained-Source Gaps

These are observations from the scheduled revisions, not a complete audit of
all mail. Fetching more source messages may change the results.

- OCaml ARM bindings v1: two different cover bodies share a subject, and both
  patches name an absent parent. The [detailed investigation](design/ocaml-arm-cover-investigation.md)
  links all four messages and identifies the missing Message-ID.
- [libxenstat v2 cover](https://lore.kernel.org/xen-devel/1792831909.8631fc262581453bbf619ec5b2062170.1a16d1bed9a000f79f@vates.tech/)
  declares three patches, but the retained revision contains only its cover.
  Recover patch indices 1, 2 and 3 before publishing.
- The rule 11.1 series retains [patch 4/4](https://lore.kernel.org/xen-devel/b30f9a03-ebbc-4ff6-8c3d-c9becbca6406@suse.com/)
  and a [reply with subject `Ping: [PATCH 4/4]`](https://lore.kernel.org/xen-devel/a8fac9fb-5dd3-4b37-9f5e-f1a4ddd65d79@suse.com/).
  The parser currently treats the ping as a second patch 4; patches 1 through 3
  are also absent from the retained revision. Fix ping recognition and recover
  the missing messages before publishing.
- [The xenheap singleton](https://lore.kernel.org/xen-devel/ca7cc14c-e860-4626-9d20-820796a91024@suse.com/)
  has a [reply with subject `Ping: [PATCH]`](https://lore.kernel.org/xen-devel/e802a84b-4528-42ed-9f16-0e503273a5cc@suse.com/).
  The parser currently treats both as patch 1/1; fix ping recognition before
  publishing, rather than selecting one by guesswork.
- The [OCaml ARM bindings v2 cover](https://lore.kernel.org/xen-devel/1793369135.8631fc262581453bbf619ec5b2062170.1a18d215a7f000cae1@vates.tech/)
  declares v2 and four patches. The retained [patch 1](https://lore.kernel.org/xen-devel/1793369332.8631fc262581453bbf619ec5b2062170.1a18d245cab000cae1@vates.tech/),
  [patch 2](https://lore.kernel.org/xen-devel/1793369333.8631fc262581453bbf619ec5b2062170.1a18d245f17000cae1@vates.tech/),
  [patch 3](https://lore.kernel.org/xen-devel/1793369334.8631fc262581453bbf619ec5b2062170.1a18d24619c000cae1@vates.tech/), and
  [patch 4](https://lore.kernel.org/xen-devel/1793369334.8631fc262581453bbf619ec5b2062170.1a18d24641a000cae1@vates.tech/)
  each reply to that cover but omit `v2` from their subjects. The importer
  blocks the inferred v1 grouping and the incomplete v2 grouping until their
  version can be reconciled without guessing.

### Quoted Remarks Without A Parent Match

The retired fixture's PR #6 retained complete source-conversation imports for
both of these Jan Beulich messages but no corresponding `IMPORT_REPLY` effect:

- [2026-10-26 16:59:37 UTC](https://lore.kernel.org/xen-devel/462a149d-9a4a-49c7-8db4-96e99463a47b@suse.com/):
  "Following Andrew's clarification, I don't think this is relevant anymore";
  the depth-two quote has no match in the accepted parent's authored remarks.
- [2026-10-26 15:44:30 UTC](https://lore.kernel.org/xen-devel/e34fc3c1-a92f-486a-8265-682ef6491345@suse.com/):
  "I don't follow why you say this when ..." and "... right afterwards you
  explain why it isn't needed." Neither excerpt matches the retained accepted
  parent presentation.

The aggregate historical diagnostic did not preserve its source Message-ID,
so it cannot honestly be attributed to exactly one of these messages. Both are
verified examples of the defect. Old PR numbers belong to the retired fixture.

The repaired queue retains standalone imported comments for unanchored remarks,
uses the unique latest earlier timestamp when disambiguating accepted quote
targets, and validates each effect from raw source. Import failures retain a
retryable tool-authored diagnostic containing the source link, attempted API
call and returned failure. Real-server tests verify standalone import provenance,
diagnostic authorship and uncertain-write replay.

The separate worker failure, "Retained publication does not match a supported
presentation", was traced to historical `master` target metadata after policy
changed to `staging`. Exact-digest compatibility repairs replay without changing
accepted request bytes, immutable refs or current policy. Read-only replay of
all three original live PRs passed after the fix. Deployment at `b0a8155`
completed a successful worker round: one new PR (#4), five delivered effects,
no round diagnostics. This was the status before the later reject-based imports.

Final deployment verification used companion `d495033` and Forgejo `b51da217df`.
The service exited successfully and its timer is active. The live outbox has
14 completed conversations, four completed replies and two completed reviews,
with no pending effects; three conversations are standalone quoted remarks.
The next bounded worker round reported two additional PR-creation blockers:

- [xen/vcpu: ensure no write when runstate handler is NULL, v1](https://lore.kernel.org/xen-devel/20261023133316.64204-1-roger@xenproject.org/):
  patch 1 fails at xen/arch/x86/x86_64/domain.c:58 and xen/common/domain.c:2330.
- [The same submission, v2](https://lore.kernel.org/xen-devel/20261023161742.67329-1-roger@xenproject.org/):
  patch 1 fails at those files and xen/include/public/vcpu.h:112.

Verification: 402 companion unit tests passed (four expected opt-in skips).
The Forgejo `TestAPIReviewImport` integration suite passed with the real-server
companion workflow enabled, including source-attributed standalone comments,
tool-authored failure comments and uncertain-write retry reconciliation. The
fixture's selected-repository token includes `write:issue`, matching existing
development setup; no live permission expansion was required.