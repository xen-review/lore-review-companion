# SPDX-License-Identifier: GPL-2.0-or-later

"""Read bounded message snapshots from a local public-inbox Git epoch."""

from __future__ import annotations

import gzip
import io
import json
import mailbox
import os
import re
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from email import policy
from email.errors import MessageError
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from lore_review_companion.mail_parser import _message_id

MAX_MANIFEST_COMPRESSED = 2 * 1024 * 1024
MAX_MANIFEST_UNCOMPRESSED = 8 * 1024 * 1024
MAX_ARCHIVE_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_THREAD_COMPRESSED = 8 * 1024 * 1024
MAX_THREAD_UNCOMPRESSED = 32 * 1024 * 1024


class PublicInboxError(RuntimeError):
    """A public-inbox Git snapshot could not be read completely."""


class ArchiveDeletionError(PublicInboxError):
    def __init__(self, commit_id: str):
        self.commit_id = commit_id
        super().__init__(f"Archive deletion record at {commit_id}; stop publication and review removal policy")


class ArchiveCheckpointUnavailable(PublicInboxError):
    def __init__(self, checkpoint: str, head: str):
        self.checkpoint = checkpoint
        self.head = head
        super().__init__(
            f"Archive checkpoint {checkpoint} is not an available ancestor of {head}; "
            "fetch missing history or inspect for a rewritten archive"
        )


@dataclass(frozen=True)
class ArchiveMessage:
    commit_id: str
    raw_message: bytes


@dataclass(frozen=True)
class ArchiveDeletionInspection:
    commit_id: str
    blob_id: str
    message_id: str
    size: int


@dataclass(frozen=True)
class ArchiveBatch:
    head: str
    messages: tuple[ArchiveMessage, ...]
    history_incomplete: bool
    has_more: bool = False


@dataclass(frozen=True)
class ArchiveBackfillBatch:
    head: str
    messages: tuple[ArchiveMessage, ...]
    has_more: bool
    needs_deepen: bool
    complete: bool


@dataclass(frozen=True)
class ArchiveEpoch:
    number: int
    git_url: str


@dataclass(frozen=True)
class ArchiveEpochCatalog:
    source: str
    epochs: tuple[ArchiveEpoch, ...]
    missing: tuple[int, ...]


@dataclass(frozen=True)
class PublicInboxThread:
    message_ids: tuple[str, ...]
    compressed_bytes: int
    uncompressed_bytes: int
    empty_or_unavailable: bool = False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _validated_inbox(inbox_url: str) -> tuple[str, str]:
    parsed = urlsplit(inbox_url)
    path = parsed.path.rstrip("/")
    segments = path.split("/")[1:]
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.query or parsed.fragment
        or any(ord(character) < 32 or ord(character) == 127 for character in inbox_url)
        or not path.startswith("/") or not segments
        or any(not re.fullmatch(r"[A-Za-z0-9._~-]+", part) or part in (".", "..") for part in segments)
    ):
        raise ValueError("Inbox URL must be a credential-free HTTPS URL with a safe path")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("Inbox URL has an invalid port") from error
    return inbox_url.rstrip("/"), path


def public_inbox_epochs(inbox_url: str, numbers: tuple[int, ...], *, source: str = "operator") -> ArchiveEpochCatalog:
    base, _ = _validated_inbox(inbox_url)
    if (
        not numbers or len(numbers) > 1000
        or any(not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= 10000 for number in numbers)
        or len(set(numbers)) != len(numbers)
    ):
        raise ValueError("Epoch numbers must be unique integers between 0 and 10000")
    ordered = tuple(sorted(numbers))
    listed = set(ordered)
    return ArchiveEpochCatalog(
        source, tuple(ArchiveEpoch(number, f"{base}/{number}") for number in ordered),
        tuple(number for number in range(ordered[-1] + 1) if number not in listed),
    )


def parse_public_inbox_manifest(inbox_url: str, compressed: bytes) -> ArchiveEpochCatalog:
    _, path = _validated_inbox(inbox_url)
    if len(compressed) > MAX_MANIFEST_COMPRESSED:
        raise PublicInboxError("Compressed public-inbox manifest exceeds the size limit")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as manifest:
            data = manifest.read(MAX_MANIFEST_UNCOMPRESSED + 1)
        if len(data) > MAX_MANIFEST_UNCOMPRESSED:
            raise PublicInboxError("Uncompressed public-inbox manifest exceeds the size limit")

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate manifest key")
                result[key] = value
            return result

        entries = json.loads(data, object_pairs_hook=unique_object)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PublicInboxError("Invalid compressed public-inbox manifest") from error
    if not isinstance(entries, dict):
        raise PublicInboxError("Public-inbox manifest must be a JSON object")
    numbers = []
    prefix = f"{path}/git/"
    for key, metadata in entries.items():
        if not key.startswith(prefix):
            continue
        match = re.fullmatch(r"(0|[1-9][0-9]{0,4})\.git", key[len(prefix):])
        if not match or not isinstance(metadata, dict):
            raise PublicInboxError("Invalid public-inbox epoch entry")
        numbers.append(int(match.group(1)))
    try:
        return public_inbox_epochs(inbox_url, tuple(numbers), source="manifest")
    except ValueError as error:
        raise PublicInboxError("Manifest has no valid bounded epoch list") from error


def _validate_thread_limits(compressed_limit: int, uncompressed_limit: int) -> None:
    for value, maximum in ((compressed_limit, MAX_THREAD_COMPRESSED), (uncompressed_limit, MAX_THREAD_UNCOMPRESSED)):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError("Thread byte limits must be positive and within their configured maxima")


def parse_public_inbox_thread(
    compressed: bytes, *, compressed_limit: int = MAX_THREAD_COMPRESSED,
    uncompressed_limit: int = MAX_THREAD_UNCOMPRESSED, temporary_directory: Path | None = None,
) -> PublicInboxThread:
    _validate_thread_limits(compressed_limit, uncompressed_limit)
    if len(compressed) > compressed_limit:
        raise PublicInboxError("Compressed public-inbox thread exceeds the byte limit")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as thread:
            mbox_data = thread.read(uncompressed_limit + 1)
    except (OSError, EOFError, zlib.error) as error:
        raise PublicInboxError("Invalid compressed public-inbox thread") from error
    if len(mbox_data) > uncompressed_limit:
        raise PublicInboxError("Uncompressed public-inbox thread exceeds the byte limit")
    with tempfile.TemporaryDirectory(prefix="lore-thread-", dir=temporary_directory) as directory:
        path = Path(directory) / "thread.mbox"
        path.write_bytes(mbox_data)
        box = mailbox.mbox(path, create=False)
        try:
            identifiers = []
            for key in box.iterkeys():
                headers = BytesHeaderParser(policy=policy.default).parsebytes(box.get_bytes(key))
                values = headers.get_all("Message-ID", [])
                identifier = _message_id(values[0]) if len(values) == 1 else None
                if (
                    identifier is None or not identifier.isascii()
                    or any(ord(character) < 33 or ord(character) > 126 for character in identifier)
                ):
                    raise PublicInboxError("Public-inbox thread contains a missing or invalid Message-ID")
                identifiers.append(identifier)
        finally:
            box.close()
    if not identifiers:
        raise PublicInboxError("Public-inbox thread did not contain mbox records")
    if len(set(identifiers)) != len(identifiers):
        raise PublicInboxError("Public-inbox thread contains duplicate Message-IDs")
    return PublicInboxThread(tuple(identifiers), len(compressed), len(mbox_data))


def fetch_public_inbox_thread(
    inbox_url: str, message_id: str, *, compressed_limit: int = MAX_THREAD_COMPRESSED,
    uncompressed_limit: int = MAX_THREAD_UNCOMPRESSED, temporary_directory: Path | None = None,
    from_index_time: int | None = None, through_index_time: int | None = None,
) -> PublicInboxThread:
    _validate_thread_limits(compressed_limit, uncompressed_limit)
    for value in (from_index_time, through_index_time):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 9007199254740991):
            raise ValueError("Indexed-time bounds must be non-negative exactly representable integer seconds")
    if from_index_time is not None and through_index_time is not None and from_index_time > through_index_time:
        raise ValueError("Indexed-time bounds are reversed")
    base, _ = _validated_inbox(inbox_url)
    identifier = _message_id(message_id) if isinstance(message_id, str) else None
    if (
        identifier is None or not identifier.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in identifier)
    ):
        raise ValueError("Thread discovery requires a valid printable ASCII Message-ID")
    ranged = from_index_time is not None or through_index_time is not None
    thread_url = f"{base}/{quote(identifier, safe='@')}/"
    if ranged:
        first = f"{from_index_time:05d}" if from_index_time is not None else ""
        last = f"{through_index_time:05d}" if through_index_time is not None else ""
        url = thread_url + "?" + urlencode({"q": f"rt:{first}..{last}", "x": "m"})
    else:
        url = thread_url + "t.mbox.gz"
    try:
        response = build_opener(_NoRedirect()).open(
            Request(url, headers={
                "Accept": "application/gzip", "Accept-Encoding": "identity", "User-Agent": "lore-gitea-mirror/0.1",
            }, data=b"" if ranged else None), timeout=20,
        )
        try:
            compressed = response.read(compressed_limit + 1)
        finally:
            response.close()
    except HTTPError as error:
        try:
            try:
                no_results = ranged and error.code == 404 and error.read(257).strip() == b"No results found"
            except (TimeoutError, OSError) as read_error:
                raise PublicInboxError(f"Thread export request failed: {type(read_error).__name__}") from read_error
        finally:
            error.close()
        if no_results:
            return PublicInboxThread((), 0, 0, empty_or_unavailable=True)
        raise PublicInboxError(f"Thread export request returned HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise PublicInboxError(f"Thread export request failed: {type(error).__name__}") from error
    thread = parse_public_inbox_thread(
        compressed, compressed_limit=compressed_limit, uncompressed_limit=uncompressed_limit,
        temporary_directory=temporary_directory,
    )
    if not ranged and identifier not in thread.message_ids:
        raise PublicInboxError("Thread export does not contain its requested Message-ID")
    return thread


def fetch_public_inbox_manifest(inbox_url: str) -> bytes:
    base, _ = _validated_inbox(inbox_url)
    url = f"{base}/manifest.js.gz"
    try:
        response = build_opener(_NoRedirect()).open(Request(url, headers={"Accept": "application/gzip"}), timeout=20)
        try:
            compressed = response.read(MAX_MANIFEST_COMPRESSED + 1)
        finally:
            response.close()
    except HTTPError as error:
        error.close()
        raise PublicInboxError(f"Manifest request to {url} returned HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise PublicInboxError(f"Manifest request to {url} failed: {error}") from error
    if len(compressed) > MAX_MANIFEST_COMPRESSED:
        raise PublicInboxError("Compressed public-inbox manifest exceeds the size limit")
    return compressed


def _read_archive_message(git: Callable[..., bytes], commit_id: str) -> ArchiveMessage:
    entries = []
    for entry in git("ls-tree", "-z", commit_id).split(b"\0"):
        if not entry:
            continue
        metadata, separator, name = entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise PublicInboxError(f"Invalid archive tree at {commit_id}")
        entries.append((name, fields[1]))
    if any(name == b"d" for name, _ in entries):
        raise ArchiveDeletionError(commit_id)
    if entries != [(b"m", b"blob")]:
        raise PublicInboxError(f"Archive tree at {commit_id} must contain exactly one mail blob")
    try:
        size = int(git("cat-file", "-s", f"{commit_id}:m"))
    except ValueError as error:
        raise PublicInboxError(f"Invalid archive mail size at {commit_id}") from error
    if size > MAX_ARCHIVE_MESSAGE_BYTES:
        raise PublicInboxError(f"Archive mail at {commit_id} exceeds the 16 MiB limit")
    raw_message = git("cat-file", "blob", f"{commit_id}:m")
    return ArchiveMessage(commit_id=commit_id, raw_message=raw_message)


def inspect_public_inbox_deletion(repo_path: Path, commit_id: str) -> ArchiveDeletionInspection:
    if not isinstance(commit_id, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_id) is None:
        raise ValueError("Deletion inspection requires a full Git commit ID")
    repo_path = repo_path.resolve(strict=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"})

    def git(*args: str) -> bytes:
        try:
            return subprocess.run(
                ["git", "--no-replace-objects", "-C", str(repo_path), *args],
                check=True, capture_output=True, timeout=30, env=environment,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise PublicInboxError("Deletion record could not be inspected locally") from error

    if git("cat-file", "-t", commit_id).strip() != b"commit":
        raise PublicInboxError("Deletion record is not a commit")
    try:
        tree_size = int(git("cat-file", "-s", f"{commit_id}^{{tree}}"))
    except ValueError as error:
        raise PublicInboxError("Deletion record has an invalid tree size") from error
    if not 0 <= tree_size <= 4096:
        raise PublicInboxError("Deletion record has an unexpected tree")
    entry = re.fullmatch(rb"100644 blob ([0-9a-f]{40}|[0-9a-f]{64})\td\x00", git("ls-tree", "-z", commit_id))
    if not entry:
        raise PublicInboxError("Deletion record does not contain exactly one d blob")
    blob_id = entry.group(1).decode("ascii")
    try:
        size = int(git("cat-file", "-s", blob_id))
    except ValueError as error:
        raise PublicInboxError("Deletion record has an invalid blob size") from error
    if not 0 <= size <= MAX_ARCHIVE_MESSAGE_BYTES:
        raise PublicInboxError("Deletion record exceeds the 16 MiB limit")
    raw_message = git("cat-file", "blob", blob_id)
    if len(raw_message) != size:
        raise PublicInboxError("Deletion record blob size changed during inspection")
    try:
        headers = BytesHeaderParser(policy=policy.default).parsebytes(raw_message)
        values = headers.get_all("Message-ID", [])
        message_id = _message_id(values[0]) if len(values) == 1 and not headers.defects and not values[0].defects else None
    except (MessageError, ValueError) as error:
        raise PublicInboxError("Deletion record could not be parsed safely") from error
    if message_id is None or not message_id.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in message_id):
        raise PublicInboxError("Deletion record does not identify exactly one valid Message-ID")
    return ArchiveDeletionInspection(commit_id, blob_id, message_id, size)


def fetch_public_inbox(repo_path: Path, *, deepen: int | None = None) -> str:
    """Fetch the configured origin without changing a checkout or requesting tags."""
    if deepen is not None and (not isinstance(deepen, int) or isinstance(deepen, bool) or not 1 <= deepen <= 1000):
        raise ValueError("deepen must be between 1 and 1000 commits")
    repo_path = repo_path.resolve(strict=True)
    command = [
        "git", "--no-replace-objects", "-C", str(repo_path), "fetch",
        "--no-tags", "--no-write-fetch-head", "--no-auto-maintenance",
        "--no-recurse-submodules",
    ]
    if deepen is not None:
        command.append(f"--deepen={deepen}")
    command.append("origin")
    try:
        subprocess.run(
            command, check=True, capture_output=True, timeout=120,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        mirror = subprocess.run(
            ["git", "-C", str(repo_path), "config", "--bool", "--get", "remote.origin.mirror"],
            capture_output=True, timeout=30,
        )
        if mirror.returncode not in (0, 1):
            raise PublicInboxError("Could not inspect the public-inbox origin configuration")
        return "refs/heads/master" if mirror.stdout.strip() == b"true" else "refs/remotes/origin/master"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise PublicInboxError("Failed to fetch public-inbox origin") from error


def verify_archive_continuity(repo_path: Path, checkpoint: str, observed_head: str, ref: str = "HEAD") -> str:
    repo_path = repo_path.resolve(strict=True)
    if any(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None for value in (checkpoint, observed_head)):
        raise ValueError("Archive continuity requires full Git object IDs")
    if ref != "HEAD" and not re.fullmatch(r"refs/(?:heads|remotes)/[A-Za-z0-9._/-]+", ref):
        raise ValueError("Archive continuity requires HEAD or a named branch ref")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"})

    def git(*args: str) -> bytes:
        try:
            return subprocess.run(
                ["git", "--no-replace-objects", "-C", str(repo_path), *args],
                check=True, capture_output=True, timeout=30, env=environment,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise PublicInboxError("Archive continuity cannot be proven from local history") from error

    grafts = git("rev-parse", "--git-path", "info/grafts").decode("utf-8").strip()
    if (repo_path / grafts).exists():
        raise PublicInboxError("Archive continuity cannot be proven with Git grafts")
    head = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode("ascii").strip()
    git("merge-base", "--is-ancestor", checkpoint, observed_head)
    git("merge-base", "--is-ancestor", observed_head, head)
    return head


def read_public_inbox(
    repo_path: Path, *, limit: int = 128, after: str | None = None, ref: str = "HEAD"
) -> ArchiveBatch:
    """Read the newest messages in oldest-first order without updating refs."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    repo_path = repo_path.resolve(strict=True)

    def git(*args: str) -> bytes:
        command = ["git", "--no-replace-objects", "-C", str(repo_path), *args]
        try:
            return subprocess.run(command, check=True, capture_output=True, timeout=30).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise PublicInboxError(f"Failed to read public-inbox Git data: {args!r}") from error

    head = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode("ascii").strip()
    shallow = git("rev-parse", "--is-shallow-repository").strip() == b"true"
    if after is None:
        commits = git("rev-list", "--reverse", f"--max-count={limit + 1}", head).decode("ascii").splitlines()
        history_incomplete = shallow or len(commits) > limit
        commits = commits[-limit:]
        has_more = False
    else:
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", after) is None:
            raise ValueError("checkpoint must be a full Git object ID")
        try:
            git("merge-base", "--is-ancestor", after, head)
        except PublicInboxError as error:
            raise ArchiveCheckpointUnavailable(after, head) from error
        commit_range = f"{after}..{head}"
        count = int(git("rev-list", "--count", commit_range))
        lines = git(
            "rev-list", "--reverse", "--parents", f"--skip={max(count - limit, 0)}",
            f"--max-count={limit}", commit_range,
        ).decode("ascii").splitlines()
        commits = []
        previous = after
        for line in lines:
            fields = line.split()
            if len(fields) != 2 or fields[1] != previous:
                raise PublicInboxError("Archive history is not linear after the checkpoint")
            commits.append(fields[0])
            previous = fields[0]
        history_incomplete = shallow
        has_more = count > len(commits)
    messages = tuple(_read_archive_message(git, commit_id) for commit_id in commits)
    return ArchiveBatch(
        head=head, messages=messages, history_incomplete=history_incomplete, has_more=has_more
    )


def read_public_inbox_backfill(
    repo_path: Path, *, limit: int = 128, before: str | None = None, ref: str = "HEAD",
) -> ArchiveBackfillBatch:
    """Read older commits newest-first, detecting shallow boundaries and true roots."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    repo_path = repo_path.resolve(strict=True)

    def git(*args: str) -> bytes:
        command = ["git", "--no-replace-objects", "-C", str(repo_path), *args]
        try:
            return subprocess.run(command, check=True, capture_output=True, timeout=30).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise PublicInboxError(f"Failed to read public-inbox Git data: {args!r}") from error

    def boundary(commit_id: str) -> tuple[bool, bool]:
        headers = git("cat-file", "commit", commit_id).split(b"\n\n", 1)[0].splitlines()
        parents = [line[7:] for line in headers if line.startswith(b"parent ")]
        if len(parents) > 1:
            raise PublicInboxError("Archive history is not linear")
        if not parents:
            return False, True
        if git("rev-parse", "--is-shallow-repository").strip() != b"true":
            raise PublicInboxError("Archive history has an unavailable parent")
        return True, False

    head = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode("ascii").strip()
    start = head
    if before is not None:
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", before) is None:
            raise ValueError("backfill checkpoint must be a full Git object ID")
        try:
            git("merge-base", "--is-ancestor", before, head)
        except PublicInboxError as error:
            raise ArchiveCheckpointUnavailable(before, head) from error
        fields = git("rev-list", "--parents", "--max-count=1", before).decode("ascii").split()
        if len(fields) > 2:
            raise PublicInboxError("Archive history is not linear")
        if len(fields) == 1:
            needs_deepen, complete = boundary(before)
            return ArchiveBackfillBatch(head, (), False, needs_deepen, complete)
        start = fields[1]

    lines = git("rev-list", "--parents", f"--max-count={limit}", start).decode("ascii").splitlines()
    commits = []
    expected = start
    parent = None
    for line in lines:
        fields = line.split()
        if len(fields) not in (1, 2) or fields[0] != expected:
            raise PublicInboxError("Archive history is not linear")
        commits.append(fields[0])
        parent = fields[1] if len(fields) == 2 else None
        expected = parent
    if not commits:
        raise PublicInboxError("Archive backfill returned no commits")
    needs_deepen, complete = boundary(commits[-1]) if parent is None else (False, False)
    messages = tuple(_read_archive_message(git, commit_id) for commit_id in commits)
    return ArchiveBackfillBatch(head, messages, parent is not None, needs_deepen, complete)