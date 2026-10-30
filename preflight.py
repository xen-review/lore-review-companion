"""Validate assembled patch mailboxes without modifying the source repository."""

from __future__ import annotations

import os
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


def _run_git(
    environment: Mapping[str, str],
    timeout: float,
    stage: str,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> str:
    command = [
        "git",
        "-c", "core.hooksPath=" + os.devnull,
        "-c", "commit.gpgsign=false",
        "-c", "user.name=Lore Gitea preflight",
        "-c", "user.email=lore-gitea-preflight@localhost",
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

    def run_git(
        self, stage: str, *arguments: str, input_bytes: bytes | None = None,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> str:
        environment = dict(self.environment)
        environment.update(environment_overrides or {})
        return _run_git(
            environment, self.timeout, stage, "-C", str(self.repository),
            *arguments, input_bytes=input_bytes,
        )


@contextmanager
def prepare_patch_mailbox(
    repository: Path,
    base_ref: str,
    patch_mailbox: bytes,
    *,
    timeout: float = 120,
) -> Iterator[PreparedMailbox]:
    """Apply a mailbox in a disposable clone available only inside the context."""
    repository = repository.resolve(strict=True)
    if not base_ref:
        raise ValueError("A base ref is required")
    if not patch_mailbox:
        raise ValueError("A patch mailbox is required")

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
            environment, timeout,
            "Resolve base", "-C", str(repository), "rev-parse", "--verify",
            "--end-of-options", f"{base_ref}^{{commit}}",
        )
        clone = Path(temp_dir) / "repository"
        _run_git(
            environment, timeout,
            "Clone repository", "clone", "--shared", "--no-checkout", "--no-tags",
            "--", str(repository), str(clone),
        )
        prepared = PreparedMailbox(clone, PreflightResult(base_commit, "", 0), environment, timeout)
        prepared.run_git("Checkout base", "checkout", "--detach", base_commit)
        prepared.run_git(
            "Apply mailbox", "am", "--no-gpg-sign", "--no-verify",
            "--committer-date-is-author-date",
            input_bytes=patch_mailbox,
        )
        head_commit = prepared.run_git("Inspect result", "rev-parse", "HEAD")
        patch_count = int(prepared.run_git(
            "Inspect result", "rev-list", "--count",
            f"{base_commit}..{head_commit}",
        ))
        yield PreparedMailbox(
            clone, PreflightResult(base_commit, head_commit, patch_count), environment, timeout,
        )


def preflight_patch_mailbox(
    repository: Path,
    base_ref: str,
    patch_mailbox: bytes,
    *,
    timeout: float = 120,
) -> PreflightResult:
    """Apply a complete mailbox in a disposable clone and return its result."""
    with prepare_patch_mailbox(repository, base_ref, patch_mailbox, timeout=timeout) as prepared:
        return prepared.result