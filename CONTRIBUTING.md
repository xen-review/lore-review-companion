<!-- SPDX-License-Identifier: GPL-2.0-or-later -->

# Contributing

All contributions to this repository must be licensed under the GNU General
Public License, version 2 or (at your option) any later version
(`GPL-2.0-or-later`). Submit only material you have the right to contribute
under these terms. Copyright remains with the respective rights holders;
contributing does not assign copyright.

## License Notices

Use a short SPDX identifier at the beginning of each source file, using its
language's comment syntax. For Python, use:

```python
# SPDX-License-Identifier: GPL-2.0-or-later
```

Keep a required shebang or encoding declaration before the SPDX line. Use
`#` for TOML, YAML, requirements, manifests and systemd units, and an HTML
comment for Markdown. Do not add comments to JSON or alter historical checksum
manifests; the repository-wide grant covers these files too. The full license
text in [COPYING](COPYING) is reproduced verbatim and is not given an SPDX
header for the program.

The project-wide standard license notice is:

> Lore Review Companion mirrors mailing-list patches and reviews into Forgejo.
>
> This program is free software; you can redistribute it and/or modify it under
> the terms of the GNU General Public License as published by the Free Software
> Foundation; either version 2 of the License, or (at your option) any later
> version.
>
> This program is distributed in the hope that it will be useful, but WITHOUT
> ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
> FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
>
> You should have received a copy of the GNU General Public License along with
> this program; if not, see <https://www.gnu.org/licenses/>.

This notice applies to the repository's original code, tests, documentation and
configuration. Individual SPDX lines identify the same terms without repeating
the full notice in every file. Existing copyright and third-party notices must
be preserved; do not invent attribution or replace upstream license notices.

## Reusing Code

Check the actual license of each file, not just its project's name. Code from
this repository can be used under GPLv2 in GPLv2-only projects, or under GPLv3
in compatible GPLv3 projects, subject to the destination's requirements.

The reverse direction is not automatic: GPLv2-only code does not acquire an
"or later" permission when copied here, and GPLv3-only code cannot be offered
under GPLv2. Do not introduce either restriction under an unchanged
`GPL-2.0-or-later` header. Such imports require a separate maintainer review of
the affected files, notices and the licensing of the combined work. Retain
provenance and all required notices for any approved third-party material.

License compatibility does not waive another project's review, authorship or
contribution policies. In particular, Forgejo currently does not accept
AI-authored contributions; a compatible license alone does not change that.

## Development

Commit early and commit often: as soon as one coherent part of a feature is
implemented and its focused checks pass, commit and push that part before
starting the next. Do not wait for the whole feature to be complete. Keep
formatting-only commits separate, never include unrelated operator changes,
and do not rewrite published history.

Run `uv check --no-sync`, `uv format --check` and the focused/full unit tests
for each Python change. `uv check` runs type checking; its initial baseline is
currently failing and must not be confused with passing unit tests. Use
`uv format` to fix formatting and keep repository-wide mechanical changes in
a separate commit. Do not synchronize away the externally pinned b4 runtime.

Follow the [implementation plan](docs/design/PLAN.md) and
[task ledger](docs/design/LEDGER.md). Reuse the existing tests and run focused
validation after each substantive change. The full test and
isolated Forgejo acceptance commands are in [README.md](README.md). Never
include private mail, operational databases, credentials or pending live effects
in tests or commits.