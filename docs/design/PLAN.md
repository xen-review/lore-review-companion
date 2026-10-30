# Lore Review Companion: Implementation Plan

This is living documentation. [LEDGER.md](LEDGER.md) records task status,
dependencies, acceptance criteria, checks, and implementation commits. Update it
when a step is verified; do not turn historical test reports into current results.

## Goal

Represent mailing-list patch series and review discussions as usable Forgejo
pull requests in a dedicated review-only repository. Start with recent
`xen-devel` threads from <https://lore.kernel.org/xen-devel/>. Reuse the existing
Python bridge rather than replace its ingestion, threading, and reconciliation.

The companion is an independent repository. Forgejo receives only the extensions
needed for scoped imported authorship, provenance, and source timestamps. The
running service is deterministic software, not an LLM agent.

## Agreed Behavior

- Create one immutable PR per revision of a series. Retain its base, patch
  commits, and head; cross-link revisions without force-pushing or silently
  retargeting older reviews. Do not automatically close or merge old PRs.
- Import existing and new replies into their original revision. Use Message-ID,
  References/In-Reply-To, and explicit associations. Subject similarity and an
  unverified change-id alone do not establish series identity.
- Preserve raw mail and hashes. Quarantine conflicting content with the same
  Message-ID. Distinguish source identity from patch equivalence and Git identity.
- Convert remarks below quoted patch lines into inline comments only when the
  quoted context maps uniquely to the correct file, side, revision, and range.
  Preserve the complete reply even when an excerpt cannot be anchored.
- Use exact deterministic matching, not nearest-line guesses or runtime LLM
  identity/anchor decisions. A multiline quote is context, not a range inferred
  from the length of the reviewer's remark.
- Import reviews as non-voting `COMMENT` reviews, never approvals or requested
  changes inferred from prose or trailers.
- Start with a configurable recent window, preserving the existing 148-hour
  default, and retrieve older dependencies or selected threads on demand.
  Resume durable cursors rather than repeatedly bootstrapping.
- Support incoming email to web reviews first. Draft export, reverse sync, and
  outbound review email are separate future work.
- Show actual imported participant profiles for submitters and reviewers,
  retaining claimed-email identity in provenance without repeating unverified
  labels above each message. Never adopt an unrelated
  existing account merely because its name or email matches a source header.
- Record the authenticated companion separately from the displayed participant.
  Show an attribution such as "Imported by @review-companion from <lore link>".
- Preserve source time and its evidence separately from actual import time.
  Only a restricted import API may initialize or correct original content times.
- Limit importing to explicitly enabled repositories where the companion
  currently has the required write permission. Do not give it administrator
  credentials or a general impersonation permission.

## Verified Starting Points

The first research pass inspected the previous bridge, Forgejo review/auth code,
and the local b4 source. It did not execute tests or verify earlier commit history.
A fresh baseline is task B01.

- Forgejo's `Sudo` header/query exists, but requires a site administrator and
  changes the effective authenticated actor. It is unsuitable for this service.
- Forgejo already supports `old_position`, `new_position`, and
  `extra_lines_count`, including storage, validation, and rendering. Reuse them.
  The configured default limit is 50 selected lines.
- The review service already accepts before/after commits. The normal REST
  handlers supply the PR merge base as before; an exact patch review may need
  the patch's own parent instead.
- Pending reviews are selected by reviewer and PR. Imported events must not
  consume unrelated drafts or combine different source participants.
- The previous companion already implements durable ingestion, explicit
  threading, immutable publication refs, dry-run plans, conflict checks, an
  outbox, provenance markers, and uncertain-write reconciliation.
- Its current inline publisher can silently fall back to a conversation write
  when inline delivery becomes unsafe. Fix that before extending delivery.
- Its current quote representation preserves one quoted line, not a whole
  multiline block. Adding a JSON range field alone is insufficient.
- b4's received-review extractor is private and alpha. Its stateful review
  integration is not an append-only archive or a Forgejo PR synchronization API.
- `ProhibitLogin` alone is insufficient for permanently imported profiles;
  recovery, OAuth, credential creation, and administrative updates also need
  invariant checks.

## Architecture

```text
source transport -> raw-mail ledger -> series/revision/thread index
                 -> isolated offline b4/Git preflight -> immutable refs
                 -> Forgejo import API -> effect reconciliation
```

The companion owns ingestion, parsing, scheduling, and local SQLite state.
Forgejo owns authorization, participant profiles, canonical content provenance,
and web rendering. Use normal REST endpoints for reads, capabilities, settings,
and ordinary metadata; use custom routes only for actual import requirements.
Push explicit allowlisted revision refs. Never mirror all refs or publish
`b4/review/*` branches, which may contain private drafts.

### b4 Boundary

Use one pinned runtime for assembly and the extraction adapter. Verify both the
runtime version and actual module/entrypoint locations; a development version
string alone is not a reproducible pin. Keep b4 external and avoid its private
SQLite/state formats.

Assemble one complete explicitly selected revision from local mail with global
`--offline-mode`, `am -m`, explicit revision selection, no added trailers, and no
partial-reroll reconstruction. Verify output membership and count. Offline mode
alone does not guarantee those properties. Apply only in an isolated checkout
with a trusted explicit base and no untrusted hooks or Actions.

Preserve the existing exact matcher. A b4 compatibility extractor may help
segment remarks, but its nearest-position heuristics are not publication proof.
Implement missing quote-block preservation locally without copying private b4
source into this project.

### Scoped Forgejo Import API

Proposed route group: `/api/v1/repos/{owner}/{repo}/review-imports`.

- Accept typed PR, conversation, `COMMENT` review, and review-reply operations.
  Include capability inspection and lookup by immutable operation key.
- Require the intersection of an explicit repository/importer grant, an
  import-only token scope and resource restriction, and the companion's current
  token-reduced write permission. Recheck every request and referenced object.
- Keep the companion as authorizer. Do not use `SetDoer(participant)` or expose
  arbitrary author-ID overrides on normal APIs. Reject `Sudo` on import routes.
- Resolve authors only through a repository/source participant registry. Create
  collision-safe managed users atomically with their registry records. Use
  server-generated `.invalid` account addresses, not source email addresses.
- Managed profiles are inactive, restricted, passwordless, non-admin,
  non-notifying, and permanently non-login. They get no repository/team access,
  keys, tokens, recovery, linking, or automatic promotion path. Guard shared
  account/authentication update paths, not just the importer endpoint.
- Store the server-derived importer, participant, destination IDs, source
  namespace, Message-ID, raw hash, validated lore URL, claimed author, source
  time/basis, comparison SHAs, actual import time, and operation key.
- Make replay return the existing objects. Reusing a key with different input
  is a conflict. Commit content/provenance atomically where possible and emit
  notifications only after commit and deduplication under an explicit policy.
- Create explicit imported review IDs with `Official=false`, independent of
  shared live drafts. Reuse existing range validation and review services.
- Add a validated optional before-commit input only if focused parent-versus-
  merge-base tests demonstrate the existing API gap. Do not add range columns.
- Restrict timestamp correction to registered imports and preserve an audit
  reason, actual edit time, and original source evidence. Unknown source time
  remains unknown; an estimate is never labeled verified lore receipt time.
- Revoke grants on repository transfer by default. Scope global user creation
  volume because participant profiles are visible global records even when
  their import authority is repository-local.

### Quote Matching And Delivery

Retain quoted blocks, quote depth, source offsets, file/hunk evidence, and the
following remark. Validate against the original patch's pinned comparison, not
an unrelated latest PR snapshot. Publish only unique contiguous same-file,
same-side ranges within configured limits. If quoted context spans hunks,
validate every segment and hunk header uniquely in patch order, then select only
the final contiguous quoted segment immediately above the remark. Mixed-side,
cross-file, repeated, or headerless material remains unresolved unless exact
retained context proves it; never fill gaps or infer a nearby line.

The full source reply is a canonical conversation effect. Inline excerpts have
separate stable keys. Losing an inline anchor does not authorize a different
write; conversation fallback must be a separately planned effect. Reply to an
existing inline conversation only when source ancestry and mappings prove it.

For new imported effects in this dedicated review-only repository, display the
reviewer's original message or exact remark text without a generated source-link
suffix. Link the source mail from the compact provenance footer, alongside the
importer's recorded build and relative import time. Do not add sender/time/tool narration or surround
the prose with a generated code fence. Server provenance remains separate and
appears below content. New nested replies omit repeated quoted mail once an
accepted explicit parent is proven, using native code-review replies where
exact parent remarks match, otherwise a concise conversation and parent link.
Raw mail remains complete in the ledger; old approved effect bytes are unchanged.
Rendering is explicitly tagged; retained old effects keep their original bytes
and operation identities rather than being silently rewritten on replay.
New `mail-text-v4` effects render a direct reply's source-verified non-code
submission quotes in Rust fenced blocks, preserving quote markers and using a
fence long enough for embedded backticks. Quotes in actual patch code, ambiguous
or unmatched material, and replies to other review messages are not converted.
The last standalone sender-name sign-off is omitted from new imported comment
bodies without removing the only remaining content. Approval reconstructs the
presentation from the retained source and recorded immediate parent; new inline
replies match the displayed parent rather than an omitted sign-off.
For newly resolved inline replies, match nonempty contiguous quoted lines from
the immediately preceding depth-one block to exactly one accepted inline remark
from the explicit parent mail, ignoring blank lines and surrounding whitespace.
Recheck uniqueness, parent identity and body hash at approval. Keep the legacy
exact-match parser for earlier effects and leave ambiguous excerpts unresolved.
Newly resolved multi-hunk remarks receive their own deterministic, independently
approved review effect, whose source identity and pinned patch are rechecked
before delivery.
Build identity is transport metadata, never part of the canonical import hash;
replay preserves the originally recorded build. PR titles are `Sender: Subject
[vN]`; covers omit the generated revision line and validated shortlog/diffstat
tail. Existing PR presentation changes require an explicit scoped, audited
compare-and-swap rather than silently changing the original import request.

### Planned Remark-Level Discussion Trees

The current imported reply endpoint accepts only code-review parents. Its
timeline and diff templates render each inline conversation as a flat list;
ordinary PR comments have no equivalent per-comment reply control. Import code
stores an inline parent in `RefCommentID`, a field also used by cross-reference
events. Do not treat that field alone as a general discussion-parent relation.

The parent relation is immutable for a posted remark and points to a single
comment in the same PR. A root has parent ID zero. New chains are bounded to
100 ancestors and reject missing targets, cycles and non-comment events. A
deleted parent leaves its ID intact: its descendants remain visible beneath an
explicit missing-parent placeholder, without inventing a different relationship.
Editing a parent preserves the edge but an import planned against the old body
must fail its content-hash check. Siblings render in source chronology with a
stable comment-ID tie-breaker; imported provenance continues to distinguish
source time from actual import time. User replies require the same permissions,
lock and archive restrictions as ordinary PR comments; imported replies also
require the source-scoped grant and parent ownership. Ordinary and inline
threads must support branching, per-comment controls, stable permalinks and
keyboard navigation, with bounded indentation on narrow screens. Existing flat
comments and links remain valid.

1. Specify a PR comment tree covering ordinary comments, inline comments and
  replies to replies. Establish stable parent identity, source chronology,
  deletion/edit behavior, bounded depth, cycle prevention, permissions and
  accessibility. Preserve existing permalinks and the current flat display
  for comments that have no parent.
2. Implement and test a durable parent relation and normal-user reply API. Plan
  an explicit, verified backfill for already imported inline reply edges;
  distinguish them from cross-reference events and preserve existing data.
  Reject cross-PR targets and unauthorized, archived or invalid writes.
3. Add a capability-advertised, scoped import operation for a reply to an
  accepted non-inline or nested parent, retaining raw source identity, exact
  parent body hash, immutable operation key and idempotent replay. Keep normal
  user permissions separate from importer grants.
4. Render actual parent/child trees and per-comment reply controls in the PR
  timeline and inline discussion views. Preserve source and import timestamps,
  stable ordering, keyboard access, mobile usability and comment permalinks.
5. Extend the companion to match each remark's immediate quoted parent against
  accepted inline and non-inline remark effects in the explicit mail ancestry.
  Only unique exact matches may create separate stable reply effects; retain
  unresolved text in the complete source conversation and never silently
  rewrite existing approved effects. Add real-server and browser coverage for
  branching, nested replies, ambiguity, replay and changed/deleted parents.

## Working Method

1. Commit this plan and ledger before importing implementation sources.
2. Establish a fresh offline baseline and trace source provenance. Import only
   reviewed code, synthetic tests, and sanitized documentation, leaving the old
   tree intact. Preserve legal notices and compatibility identifiers.
3. Make one small behavioral change at a time. Immediately run the narrowest
   check that can falsify it, then repair the same slice or proceed.
4. Update the ledger with actual checks, results, limitations, and commit
   subjects. Commit verified companion steps in small topic-based commits.
5. Keep Forgejo changes in the parent fork and reference their tests/files from
   this ledger. Do not claim the companion commit contains parent-fork changes.
6. Under the operator's 2026-10-30 authorization, commit and push both
  repositories in tested topics. Forgejo uses incremental stacked branches,
  with every topic in the final branch. Do not rewrite history or commit
  unrelated changes, private state, or credentials.

## Safety And Completion

Keep existing conflict stops, worker locking, immutable revision membership,
uncertain-write reconciliation, and explicit association policies. Renaming must
not rewrite delivered effect IDs, markers, or old commit-generation identities.

Production build, backup, restart/migration, scoped credential provisioning and
review-only deployment are now explicitly authorized and tracked as P01-P08,
including a bounded initial population of actual Xen threads and live review.
Do not silently adopt historical pending writes or change shared SSH files.
Back up the exact instance before migration and stop on failed safety gates.
Disposable fixtures need independent
HOME, database, repository and SSH roots, outbound mail disabled/captured,
`DISABLE_SSH=true`, and `SSH_CREATE_AUTHORIZED_KEYS_FILE=false`.

Do not claim full thread coverage from a recent NNTP window. Unavailable mail,
incomplete patch sets, ambiguous quotes, and unknown bases are visible supported
states, not reasons to invent content. Deletion or unverifiable archive history
halts publication without guessing a destructive retraction.

Deferred: full historical mirrors, reverse drafts/email sending, dual-product
Gitea certification, staging-arrival inference/notifications, automatic PR
closing/merging, destructive retractions, exceptional series-link corrections,
and repair of the earlier instance's SSH incident.

Complete the deliverable when all in-scope ledger items meet their acceptance
gates, including safe isolated end-to-end tests and installation documentation.
Record genuine blockers rather than claiming completion. Development is for a
local Forgejo fork; Forgejo's policy excludes AI-authored upstream contributions.