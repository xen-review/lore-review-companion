# Source Provenance

## Initial Snapshot

Imported on 2026-10-29 from the operator-provided `contrib/lore-gitea-mirror`
directory in the Forgejo development workspace. The earlier implementation
was developed for Gitea; this repository continues that work for Forgejo.

The snapshot includes only Python implementation/test files and
[requirements.txt](requirements.txt). Runtime files now live under
`lore_review_companion/` and tests under `tests/`. It does not include downloaded email,
operational databases, credentials, caches, private incident/setup documents,
installed service definitions, or either parent project's Git metadata.

No file content was changed for the snapshot import. `SOURCE.sha256` records
the original individual file hashes at the import commit. It describes that
historical snapshot, not subsequent renamed or edited versions of the files.

The source directory was untracked in the available Forgejo checkout. The
available earlier Gitea checkout did not contain it or commits for it. This is
a source-snapshot import, not a claim of preserved earlier companion history.
The baselines inspected were:

- Forgejo: `cff8e8831a0695913dd0f383426a487baf899cd3`.
- Gitea: `178d33ed4dce734384caa61a04290b30ab955b21`.

The complete offline suite passed before and after import: 295 tests run,
292 passed, 3 optional integration tests skipped. See [LEDGER.md](LEDGER.md)
for command scope and subsequent changes.

## Licensing And Dependencies

[LICENSE.GITEA](LICENSE.GITEA) retains the MIT license from the earlier Gitea
checkout for source-context and attribution preservation. The imported Python
snapshot has no per-file copyright/license declarations; a final distribution
license and per-file provenance review remain part of R01. Workspace placement
alone is not evidence that independent Python files were relicensed under
Forgejo's license. Do not remove required legal references during renaming.

No Forgejo or b4 implementation is vendored here. b4 remains an external
dependency behind a versioned subprocess adapter; private runtime imports are
not copied source. Its own license and runtime installation must be respected.

## Compatibility

The snapshot used legacy Gitea-named modules, environment variables, markers,
and test fixtures. The API/publisher modules now use neutral `forge_*` names;
environment and wire compatibility are separate concerns. Durable source and
delivery identities must survive renaming, and changes to the preflight
committer identity must be versioned because they change generated commit IDs.

The historical draft-reading code is present for traceability but is not part
of the incoming-only worker. Never publish b4 review/tracking refs or invoke
draft delivery implicitly.