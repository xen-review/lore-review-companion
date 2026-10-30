"""Run b4's local series assembler without network or checkout side effects."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lore_review_companion.mail_parser import parse_mbox
from lore_review_companion.series import discover_series

PINNED_B4_SOURCE = "3b5a059e8544da3166d1a40fc68986110a55defe9c25ac128ac106ef4021679e"
PINNED_B4_VERSIONS = {"b4": "0.17.dev0", "liblore": "0.10.1"}

_RUNTIME_PROBE = """
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path

digest = hashlib.sha256()
versions = {}
paths = {}
for name in ('b4', 'liblore'):
    root = Path(importlib.util.find_spec(name).origin).parent
    versions[name] = importlib.metadata.version(name)
    paths[name] = str(root)
    for source in sorted(root.rglob('*.py')):
        digest.update((name + '/' + source.relative_to(root).as_posix() + '\\0').encode())
        digest.update(hashlib.sha256(source.read_bytes()).digest())
print(json.dumps({'versions': versions, 'source_sha256': digest.hexdigest(), 'module_paths': paths}))
"""


def inspect_b4_runtime(python_executable: str, *, timeout: float = 30) -> dict:
    """Reject private-runtime drift before assembly or legacy review parsing."""
    try:
        result = subprocess.run(
            [python_executable, "-I", "-c", _RUNTIME_PROBE], check=True,
            capture_output=True, text=True, timeout=timeout,
            env={"PATH": os.environ.get("PATH", os.defpath)}, stdin=subprocess.DEVNULL,
        )
        runtime = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise B4AssemblyError("Cannot inspect the configured b4 interpreter; install the pinned runtime") from error
    if not isinstance(runtime, dict) or runtime.get("versions") != PINNED_B4_VERSIONS or runtime.get("source_sha256") != PINNED_B4_SOURCE:
        raise B4AssemblyError("Unsupported b4/liblore runtime fingerprint; install the reviewed source pins")
    return runtime


class B4AssemblyError(RuntimeError):
    """The local b4 invocation did not produce exactly one patch mailbox."""


class B4ReviewError(RuntimeError):
    """The installed b4 review parser is unavailable or incompatible."""


def _check_review_runtime(python_executable: str, timeout: float) -> None:
    try:
        inspect_b4_runtime(python_executable, timeout=timeout)
    except B4AssemblyError as error:
        raise B4ReviewError(str(error)) from error


@dataclass(frozen=True)
class AssemblyResult:
    patch_mailbox: bytes
    stdout: str
    stderr: str


@dataclass(frozen=True)
class QuotedComment:
    path: str
    line: int
    text: str
    content: str | None


@dataclass(frozen=True)
class LocalDraft:
    patch_index: int
    message_id: str | None
    reply: str
    trailers: tuple[str, ...]
    comments: tuple[QuotedComment, ...]


@dataclass(frozen=True)
class DraftSnapshot:
    branch: str
    tracking_commit: str
    reviewer_email: str
    revision: int
    change_id: str | None
    drafts: tuple[LocalDraft, ...]


_REVIEW_PARSER = """
import importlib.metadata
import json
import re
import sys

version = importlib.metadata.version('b4')
if not re.fullmatch(r'0\\.17(?:[.+-].*)?', version):
    raise SystemExit('Unsupported b4 review version: ' + version)
from b4.review._review import _extract_comments_from_quoted_reply
text = json.load(sys.stdin)
json.dump(_extract_comments_from_quoted_reply(text, capture_preamble=True), sys.stdout)
"""

_DRAFT_READER = """
import importlib.metadata
import json
import re
import sys

version = importlib.metadata.version('b4')
if not re.fullmatch(r'0\\.17(?:[.+-].*)?', version):
    raise SystemExit('Unsupported b4 review version: ' + version)
from b4.review._review import load_tracking
_, tracking = load_tracking(sys.argv[1], sys.argv[2])
reviewer = sys.argv[3]
series = tracking['series']
patches = tracking['patches']
if not isinstance(series, dict) or not isinstance(patches, list):
    raise SystemExit('Invalid b4 review tracking data')
items = []
for index, target in enumerate([series, *patches]):
    reviews = target.get('reviews', {})
    review = reviews.get(reviewer, {})
    if not isinstance(review, dict):
        raise SystemExit('Invalid b4 reviewer data')
    if review.get('reply') or review.get('trailers') or review.get('comments'):
        header = target.get('header-info', {})
        items.append({'patch_index': index, 'message_id': header.get('msgid'),
                      'reply': review.get('reply', ''), 'trailers': review.get('trailers', []),
                      'comments': review.get('comments', [])})
json.dump({'revision': series.get('revision'), 'change_id': series.get('change-id'), 'drafts': items}, sys.stdout)
"""


def read_local_review_drafts(
    repository: Path, branch: str, reviewer_email: str, *, python_executable: str, timeout: float = 30,
) -> DraftSnapshot:
    """Read one reviewer's b4 drafts without changing the branch, checkout, or b4 database."""
    repository = repository.resolve(strict=True)
    if not repository.is_dir() or not branch.startswith("b4/review/"):
        raise ValueError("An existing repository and b4/review/ branch are required")
    if not reviewer_email or any(ord(character) < 33 or ord(character) == 127 for character in reviewer_email):
        raise ValueError("An explicit reviewer email is required")
    if not python_executable:
        raise ValueError("The b4 Python executable is required")
    _check_review_runtime(python_executable, timeout)
    with tempfile.TemporaryDirectory(prefix="lore-gitea-drafts-") as temp_dir:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": temp_dir,
            "XDG_CONFIG_HOME": temp_dir, "XDG_CACHE_HOME": temp_dir,
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "PYTHONNOUSERSITE": "1",
        }

        def git(*args: str) -> str:
            try:
                result = subprocess.run(
                    ["git", "--no-replace-objects", "-C", str(repository), *args],
                    capture_output=True, text=True, timeout=timeout, env=environment, check=True,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise B4ReviewError("Could not read the b4 review branch") from error
            return result.stdout.strip()

        git("check-ref-format", "--branch", branch)
        commit = git("rev-parse", "--verify", "--end-of-options", f"refs/heads/{branch}^{{commit}}")
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
            raise B4ReviewError("b4 review branch did not resolve to a full commit ID")
        try:
            size = int(git("cat-file", "-s", commit))
        except ValueError as error:
            raise B4ReviewError("Could not determine b4 tracking commit size") from error
        if size > 8 * 1024 * 1024:
            raise B4ReviewError("b4 tracking commit exceeds the 8 MiB limit")
        try:
            result = subprocess.run(
                [python_executable, "-c", _DRAFT_READER, str(repository), commit, reviewer_email],
                capture_output=True, text=True, timeout=timeout, env=environment, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise B4ReviewError(f"Could not run b4 draft reader: {error}") from error
        if result.returncode:
            raise B4ReviewError(f"b4 draft reader failed ({result.returncode}): {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise B4ReviewError("b4 draft reader returned invalid JSON") from error
    if (
        not isinstance(data, dict) or not isinstance(data.get("revision"), int)
        or isinstance(data.get("revision"), bool) or data["revision"] < 1
        or (data.get("change_id") is not None and not isinstance(data["change_id"], str))
        or not isinstance(data.get("drafts"), list)
    ):
        raise B4ReviewError("b4 draft reader returned invalid tracking data")
    drafts = []
    for item in data["drafts"]:
        if (
            not isinstance(item, dict) or not isinstance(item.get("patch_index"), int)
            or isinstance(item.get("patch_index"), bool) or item["patch_index"] < 0
            or (item.get("message_id") is not None and not isinstance(item["message_id"], str))
            or not isinstance(item.get("reply"), str) or not isinstance(item.get("trailers"), list)
            or any(not isinstance(trailer, str) for trailer in item["trailers"])
            or not isinstance(item.get("comments"), list)
        ):
            raise B4ReviewError("b4 draft reader returned an invalid draft")
        comments = []
        for comment in item["comments"]:
            if (
                not isinstance(comment, dict) or not isinstance(comment.get("path"), str)
                or not isinstance(comment.get("line"), int) or isinstance(comment.get("line"), bool)
                or not isinstance(comment.get("text"), str)
                or (comment.get("content") is not None and not isinstance(comment["content"], str))
            ):
                raise B4ReviewError("b4 draft reader returned an invalid comment")
            comments.append(QuotedComment(comment["path"], comment["line"], comment["text"], comment.get("content")))
        drafts.append(LocalDraft(item["patch_index"], item.get("message_id"), item["reply"], tuple(item["trailers"]), tuple(comments)))
    return DraftSnapshot(branch, commit, reviewer_email, data["revision"], data.get("change_id"), tuple(drafts))


def extract_quoted_comments(
    body: str, *, python_executable: str, timeout: float = 30,
) -> tuple[QuotedComment, ...]:
    """Run b4's version-checked quoted-reply parser in an isolated interpreter."""
    if not isinstance(body, str) or not python_executable:
        raise ValueError("A reply body and b4 Python executable are required")
    _check_review_runtime(python_executable, timeout)
    with tempfile.TemporaryDirectory(prefix="lore-gitea-review-") as temp_dir:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": temp_dir,
            "XDG_CONFIG_HOME": temp_dir,
            "XDG_CACHE_HOME": temp_dir,
            "PYTHONNOUSERSITE": "1",
        }
        try:
            completed = subprocess.run(
                [python_executable, "-c", _REVIEW_PARSER], input=json.dumps(body),
                capture_output=True, text=True, timeout=timeout, env=environment, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise B4ReviewError(f"Could not run b4 review parser: {error}") from error
        if completed.returncode:
            raise B4ReviewError(f"b4 review parser failed ({completed.returncode}): {completed.stderr.strip()}")
        try:
            parsed = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise B4ReviewError("b4 review parser returned invalid JSON") from error
        if not isinstance(parsed, list):
            raise B4ReviewError("b4 review parser returned an invalid comment list")
        comments = []
        for item in parsed:
            if (
                not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not isinstance(item.get("line"), int) or isinstance(item.get("line"), bool)
                or not isinstance(item.get("text"), str)
                or (item.get("content") is not None and not isinstance(item["content"], str))
            ):
                raise B4ReviewError("b4 review parser returned an invalid comment")
            comments.append(QuotedComment(item["path"], item["line"], item["text"], item.get("content")))
        return tuple(comments)


def assemble_local_series(
    mailbox_path: Path,
    *,
    executable: str | None = None,
    python_executable: str | None = None,
    revision: int | None = None,
    timeout: float = 120,
) -> AssemblyResult:
    """Assemble one local mbox series; never apply it to a Git worktree."""
    mailbox_path = mailbox_path.resolve(strict=True)
    if not mailbox_path.is_file():
        raise ValueError(f"Expected an mbox file: {mailbox_path}")
    messages = parse_mbox(mailbox_path)
    candidates = discover_series(messages)
    if len(candidates) != 1 or not candidates[0].complete or candidates[0].conflicts:
        raise B4AssemblyError("Assembly requires exactly one complete, unambiguous source revision")
    selected = candidates[0]
    if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool) or revision != selected.revision):
        raise B4AssemblyError("Requested revision does not match the complete source mailbox")
    interpreter = python_executable or sys.executable
    if executable is not None:
        located = shutil.which(executable)
        if located is None:
            raise B4AssemblyError("Configured b4 executable does not exist")
        with Path(located).open("r", encoding="utf-8") as entrypoint:
            shebang = entrypoint.readline(4096).strip()
        if not shebang.startswith("#!/") or len(shebang[2:].split()) != 1:
            raise B4AssemblyError("Configured b4 executable must use an explicit Python interpreter")
        interpreter = shebang[2:]
        if python_executable is not None and os.path.abspath(interpreter) != os.path.abspath(python_executable):
            raise B4AssemblyError("b4 executable and configured interpreter differ")
    inspect_b4_runtime(interpreter, timeout=timeout)

    with tempfile.TemporaryDirectory(prefix="lore-gitea-b4-") as output_dir:
        command = [
            interpreter,
            "-I",
            "-c",
            "from b4.command import cmd; cmd()",
            "--offline-mode",
            "--no-stdin",
            "am",
            "--use-local-mbox",
            str(mailbox_path),
            "--use-version", str(selected.revision),
            "--no-add-trailers",
            "--no-partial-reroll",
            "--no-cover",
            "--outdir",
            output_dir,
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=output_dir,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": output_dir,
                "XDG_CONFIG_HOME": output_dir,
                "XDG_CACHE_HOME": output_dir,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "PYTHONNOUSERSITE": "1",
            },
            stdin=subprocess.DEVNULL,
        )
        if completed.returncode:
            raise B4AssemblyError(
                f"b4 am failed ({completed.returncode}): {completed.stderr.strip()}"
            )

        outputs = list(Path(output_dir).glob("*.mbx"))
        if len(outputs) != 1:
            raise B4AssemblyError(
                f"Expected one assembled patch mailbox, found {len(outputs)}"
            )
        source_patches = tuple(sorted(
            (message for message in messages if message.kind == "patch"), key=lambda message: message.patch_index or 1,
        ))
        output_patches = parse_mbox(outputs[0])

        def patch_identity(message) -> tuple[str, str | None, str]:
            _, separator, diff = message.body_text.partition("diff --git ")
            diff = diff.split("\n-- \n", 1)[0].rstrip()
            return message.subject_title, message.from_address, separator + diff

        if len(output_patches) != selected.expected_patch_count or tuple(
            patch_identity(message) for message in output_patches
        ) != tuple(patch_identity(message) for message in source_patches):
            raise B4AssemblyError("Assembled mailbox does not match the selected patch membership")
        return AssemblyResult(
            patch_mailbox=outputs[0].read_bytes(),
            stdout=completed.stdout,
            stderr=completed.stderr,
        )