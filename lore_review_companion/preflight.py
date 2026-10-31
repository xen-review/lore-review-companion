# SPDX-License-Identifier: GPL-2.0-or-later

"""Validate assembled patch mailboxes without modifying the source repository."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping


class GitPreflightError(RuntimeError):
    """The selected base or patch mailbox could not be validated."""


@dataclass(frozen=True)
class PreflightResult:
    base_commit: str
    head_commit: str
    patch_count: int
    rejected_hunks: int = 0
    requested_base_commit: str | None = None


@dataclass(frozen=True)
class PatchCommit:
    message_id: str
    before_commit: str
    after_commit: str


def _run_git(
    environment: Mapping[str, str],
    timeout: float,
    stage: str,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> str:
    command = [
        "git",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Lore Gitea preflight",
        "-c",
        "user.email=lore-gitea-preflight@localhost",
        *arguments,
    ]
    completed = subprocess.run(
        command,
        input=input_bytes,
        capture_output=True,
        env=environment,
        timeout=timeout,
        check=False,
    )
    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode:
        detail = "\n".join(part for part in (stdout, stderr) if part)
        raise GitPreflightError(f"{stage} failed ({completed.returncode}): {detail}")
    return stdout


@dataclass(frozen=True)
class PreparedMailbox:
    repository: Path
    result: PreflightResult
    environment: Mapping[str, str] = field(repr=False)
    timeout: float = 120

    def patch_commits(self, message_ids: tuple[str, ...]) -> tuple[PatchCommit, ...]:
        if (
            not message_ids
            or len(message_ids) != self.result.patch_count
            or any(not message_id for message_id in message_ids)
            or len(set(message_ids)) != len(message_ids)
        ):
            raise GitPreflightError(
                "One unique source Message-ID is required per assembled patch"
            )
        output = self.run_git(
            "Inspect assembled patch chain",
            "--no-replace-objects",
            "rev-list",
            "--reverse",
            "--parents",
            "--no-abbrev-commit",
            f"{self.result.base_commit}..{self.result.head_commit}",
        )
        rows = [line.split() for line in output.splitlines()]
        if len(rows) != len(message_ids):
            raise GitPreflightError(
                "Assembled commit count differs from source patch membership"
            )
        comparisons = []
        parent = self.result.base_commit
        for message_id, row in zip(message_ids, rows, strict=True):
            if len(row) != 2 or row[1] != parent:
                raise GitPreflightError(
                    "Assembled patches must form one exact single-parent chain"
                )
            comparisons.append(PatchCommit(message_id, parent, row[0]))
            parent = row[0]
        if parent != self.result.head_commit:
            raise GitPreflightError(
                "Assembled patch chain does not reach the pinned head"
            )
        return tuple(comparisons)

    def run_git(
        self,
        stage: str,
        *arguments: str,
        input_bytes: bytes | None = None,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> str:
        environment = dict(self.environment)
        environment.update(environment_overrides or {})
        return _run_git(
            environment,
            self.timeout,
            stage,
            "-C",
            str(self.repository),
            *arguments,
            input_bytes=input_bytes,
        )


def _reject_files(repository: Path) -> dict[Path, bytes]:
    found = {}
    for directory, directories, files in os.walk(repository, followlinks=False):
        directories[:] = [name for name in directories if name != ".git"]
        for name in files:
            if not name.endswith(".rej"):
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise GitPreflightError("Reject file must not be a symbolic link")
            found[path] = path.read_bytes()
    return found


def _apply_mailbox_with_rejects(prepared: PreparedMailbox, mailbox: bytes) -> int:
    rejected_hunks = 0
    arguments = ("am", "--reject", "--no-gpg-sign", "--no-verify", "--committer-date-is-author-date")
    input_bytes = mailbox
    while True:
        previous = _reject_files(prepared.repository)
        for path in previous:
            os.utime(path, ns=(0, 0), follow_symlinks=False)
        try:
            prepared.run_git("Apply mailbox with rejects", *arguments, input_bytes=input_bytes)
            return rejected_hunks
        except GitPreflightError as error:
            if re.search(r"(?m)^error: (?!while searching for:|patch failed:)", str(error)):
                raise
            current = _reject_files(prepared.repository)
            changed = {
                path: content for path, content in current.items()
                if previous.get(path) != content or path.stat().st_mtime_ns != 0
            }
            count = sum(
                len(re.findall(rb"(?m)^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", content))
                for content in changed.values()
            )
            if not count:
                raise
            for path, content in changed.items():
                if path in previous:
                    path.write_bytes(previous[path].rstrip(b"\n") + b"\n" + content)
            rejected_hunks += count
            prepared.run_git("Stage partially applied patch and rejects", "add", "-A", "-f", "--", ".")
            arguments = ("am", "--continue")
            input_bytes = None


def _format_patch_parent(prepared: PreparedMailbox, mailbox: bytes) -> str | None:
    envelope = re.match(rb"From ([0-9a-f]{40}|[0-9a-f]{64}) ", mailbox)
    if envelope is None:
        return None
    commit = envelope.group(1).decode("ascii")
    try:
        ancestry = prepared.run_git(
            "Inspect submitted commit", "--no-replace-objects", "rev-list",
            "--parents", "-n", "1", commit,
        ).split()
        if len(ancestry) != 2 or ancestry[0] != commit:
            return None
        submitted = prepared.run_git(
            "Identify submitted patch", "patch-id", "--stable", input_bytes=mailbox
        ).splitlines()[0].split()[0]
        diff = prepared.run_git(
            "Inspect submitted commit diff", "--no-replace-objects", "show",
            "--format=", "--binary", "--no-ext-diff", commit,
        ).encode("utf-8")
        committed = prepared.run_git(
            "Identify submitted commit diff", "patch-id", "--stable", input_bytes=diff
        ).splitlines()[0].split()[0]
    except (GitPreflightError, IndexError):
        return None
    return ancestry[1] if submitted == committed else None


@contextmanager
def prepare_patch_mailbox(
    repository: Path,
    base_ref: str,
    patch_mailbox: bytes,
    *,
    timeout: float = 120,
    allow_rejects: bool = False,
    alternative_bases: tuple[str, ...] = (),
    infer_format_patch_parent: bool = False,
) -> Iterator[PreparedMailbox]:
    """Apply a mailbox in a disposable clone available only inside the context."""
    repository = repository.resolve(strict=True)
    if not base_ref:
        raise ValueError("A base ref is required")
    if not patch_mailbox:
        raise ValueError("A patch mailbox is required")
    if len(alternative_bases) > 8 or any(
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base) is None
        for base in alternative_bases
    ):
        raise ValueError("Alternative bases must be bounded full commit hashes")

    with tempfile.TemporaryDirectory(prefix="lore-gitea-preflight-") as temp_dir:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": temp_dir,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }

        base_commit = _run_git(
            environment,
            timeout,
            "Resolve base",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{base_ref}^{{commit}}",
        )
        requested_base_commit = base_commit
        clone = Path(temp_dir) / "repository"
        _run_git(
            environment,
            timeout,
            "Clone repository",
            "clone",
            "--shared",
            "--no-checkout",
            "--no-tags",
            "--",
            str(repository),
            str(clone),
        )
        prepared = PreparedMailbox(
            clone, PreflightResult(base_commit, "", 0), environment, timeout
        )
        prepared.run_git("Checkout base", "checkout", "--detach", base_commit)
        rejected_hunks = 0
        try:
            prepared.run_git(
                "Apply mailbox",
                "am",
                "--no-gpg-sign",
                "--no-verify",
                "--committer-date-is-author-date",
                input_bytes=patch_mailbox,
            )
        except GitPreflightError:
            if not allow_rejects and not alternative_bases and not infer_format_patch_parent:
                raise
            prepared.run_git("Abort failed mailbox application", "am", "--abort")
            applied = False
            inferred = _format_patch_parent(prepared, patch_mailbox) if infer_format_patch_parent else None
            candidates = (*alternative_bases, *((inferred,) if inferred else ()))
            for alternative in dict.fromkeys(candidates):
                if alternative == requested_base_commit:
                    continue
                try:
                    resolved = prepared.run_git(
                        "Resolve source base", "--no-replace-objects", "rev-parse",
                        "--verify", "--end-of-options", f"{alternative}^{{commit}}",
                    )
                except GitPreflightError:
                    continue
                prepared.run_git("Checkout source base", "checkout", "--detach", resolved)
                try:
                    prepared.run_git(
                        "Apply mailbox on source base", "am", "--no-gpg-sign",
                        "--no-verify", "--committer-date-is-author-date", input_bytes=patch_mailbox,
                    )
                    base_commit = resolved
                    applied = True
                    break
                except GitPreflightError:
                    prepared.run_git("Abort failed source-base application", "am", "--abort")
            if not applied:
                prepared.run_git("Restore requested base", "checkout", "--detach", requested_base_commit)
                if not allow_rejects:
                    raise GitPreflightError("Mailbox did not apply to any available base")
                rejected_hunks = _apply_mailbox_with_rejects(prepared, patch_mailbox)
        head_commit = prepared.run_git("Inspect result", "rev-parse", "HEAD")
        patch_count = int(
            prepared.run_git(
                "Inspect result",
                "rev-list",
                "--count",
                f"{base_commit}..{head_commit}",
            )
        )
        yield PreparedMailbox(
            clone,
            PreflightResult(base_commit, head_commit, patch_count, rejected_hunks, requested_base_commit),
            environment,
            timeout,
        )


def preflight_patch_mailbox(
    repository: Path,
    base_ref: str,
    patch_mailbox: bytes,
    *,
    timeout: float = 120,
) -> PreflightResult:
    """Apply a complete mailbox in a disposable clone and return its result."""
    with prepare_patch_mailbox(
        repository, base_ref, patch_mailbox, timeout=timeout
    ) as prepared:
        return prepared.result
