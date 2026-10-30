# Lore Review Companion

Mirror mailing-list patch series and review discussions into a dedicated
Forgejo repository, starting with `xen-devel` on lore.kernel.org. The companion
is deterministic Python software; it does not require a runtime LLM.

**Status:** an independently packaged prototype, based on the previous bridge.
Scoped Forgejo participant/import APIs and multiline quote matching are still
planned, not implemented. Read [PLAN.md](PLAN.md) and [LEDGER.md](LEDGER.md)
for scope, current results, and next steps.

## Install

Python 3.11 or later and Git are required. From this repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/lore-review-companion --help
```

The package installs the NNTP compatibility dependency on Python 3.13 or later.
b4 is an external optional runtime for patch assembly and the current quoted-
reply adapter; it is not automatically installed here. Pinning and validating
one supported b4 installation is tracked under C02. Installing an arbitrary b4
version is not sufficient for its private alpha review interfaces.

The CLI also supports module execution:

```sh
.venv/bin/python -m lore_review_companion --help
```

## Safe Starting Point

List explicitly configured public-inbox epochs without fetching anything:

```sh
.venv/bin/lore-review-companion discover-epochs --epoch 0
```

Use command-specific `--help` before opening a ledger or configuring an API
destination. Individual commands can write local ingestion state; do not use
an operational database as a test fixture. Remote PR/comment publication is
separately gated by write flags and reviewed plan digests. Do not enable the
old live-test opt-ins against a real instance.

This repository is not ready for unattended deployment. It does not yet provide
the proposed scoped participant-import endpoints in stock Forgejo. Existing
publication paths still use an integration account; no administrator/Sudo
credential is required or recommended for the companion.

## Develop And Test

With the package installed in the virtual environment:

```sh
env -u LORE_GITEA_ACCEPTANCE -u LORE_GITEA_ACCEPTANCE_DELETIONS \
  -u LORE_GITEA_ACCEPTANCE_B4_PYTHON \
  .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

For the inline-only delivery slice, use `-p test_forge_comments.py` instead.
The default suite uses synthetic mail and fake API transports. Three optional
tests require explicit opt-in: one disposable server and two real-b4 checks.
Do not copy private mail or operational ledgers into this repository.

Runtime modules live in `lore_review_companion/`; tests live in `tests/`.
Update the ledger with actual checks and limitations, then commit each verified
topic separately. The operator handles pushing; no automatic push is part of
the workflow.

## Compatibility And Provenance

Legacy `LORE_GITEA_*` environment variables, body markers and commit identities
remain where changing them could break existing reviewed plans or replay.
They do not imply general Gitea product certification. Compatibility changes
must be explicit and tested, not global search-and-replace of persisted data.

[ORIGIN.md](ORIGIN.md) records the source snapshot and license context, including
the outstanding distribution-license review. No b4 or Forgejo implementation
is vendored. Forgejo extensions are developed in a separate local fork, not
submitted as AI-authored upstream contributions.