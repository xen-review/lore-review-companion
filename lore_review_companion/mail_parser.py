# SPDX-License-Identifier: GPL-2.0-or-later

"""Parse source mail headers and patch-series metadata."""

from __future__ import annotations

import hashlib
import mailbox
import re
from dataclasses import dataclass, replace
from datetime import timezone
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Literal


MessageKind = Literal["patch", "cover", "reply", "other"]

MAX_MBOX_BYTES = 64 * 1024 * 1024
MAX_MBOX_MESSAGES = 10000
MAX_THREAD_HEADER_BYTES = 64 * 1024
MAX_THREAD_REFERENCES = 1000

_MESSAGE_ID_RE = re.compile(r"<([^<>\s]+@[^<>\s]+)>")
_BARE_MESSAGE_ID_RE = re.compile(r"[^<>\s]+@[^<>\s]+")
_PATCH_TOKEN_RE = re.compile(r"(?:^|\s)PATCH(?:$|\s)", re.IGNORECASE)
_PATCH_VERSION_RE = re.compile(r"(?<!\w)v(\d+)(?!\w)", re.IGNORECASE)
_PATCH_INDEX_RE = re.compile(r"(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)")
_SUBJECT_TAG_RE = re.compile(r"\[([^\]]+)\]")
_REPLY_PREFIX_RE = re.compile(r"^(?:(?:re|fwd?):\s*)+", re.IGNORECASE)
_TRAILER_RE = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9-]*):[ \t]*(?P<value>.*)")
_NNTP_TRANSPORT_HEADERS = frozenset(("path", "newsgroups", "xref"))


def nntp_matches_source(article: bytes, source: bytes) -> bool:
    def components(raw: bytes, *, nntp: bool):
        header, separator, body = raw.replace(b"\r\n", b"\n").partition(b"\n\n")
        if not separator or b"\r" in header:
            return None
        message = BytesHeaderParser(policy=policy.default).parsebytes(header + b"\n\n")
        if message.defects:
            return None
        fields = list(message.raw_items())
        transport = [
            name.lower()
            for name, _ in fields
            if name.lower() in _NNTP_TRANSPORT_HEADERS
        ]
        if (nntp and sorted(transport) != sorted(_NNTP_TRANSPORT_HEADERS)) or (
            not nntp and transport
        ):
            return None
        headers = tuple(
            (name, re.sub(r"\n(?=[ \t])", "", value))
            for name, value in fields
            if name.lower() not in _NNTP_TRANSPORT_HEADERS
        )
        return headers, body

    source_parts = components(source, nntp=False)
    return source_parts is not None and source_parts == components(article, nntp=True)


def _bounded_header_number(value: str) -> int:
    return int(value) if len(value) <= 9 else 1_000_000_000


@dataclass(frozen=True)
class Trailer:
    name: str
    value: str


@dataclass(frozen=True)
class MessageTimeEstimate:
    timestamp: str
    source: Literal["received", "date"]
    verified_lore_receipt: bool = False


@dataclass(frozen=True)
class ParsedMessage:
    raw_message: bytes
    sha256: str
    message_id: str | None
    from_name: str | None
    from_address: str | None
    subject: str
    references: tuple[str, ...]
    in_reply_to: tuple[str, ...]
    kind: MessageKind
    patch_version: int | None
    patch_index: int | None
    series_size: int | None
    subject_title: str
    trailers: tuple[Trailer, ...]
    body_text: str
    errors: tuple[str, ...]
    time_estimate: MessageTimeEstimate | None


@dataclass(frozen=True)
class QuotedRemark:
    quote: tuple[str, ...]
    headers: tuple[str, ...]
    text: str
    quote_depth: int
    quote_start: int
    remark_start: int
    remark_end: int


def extract_quoted_remarks(body: str) -> tuple[QuotedRemark, ...]:
    """Retain quoted context and following remarks without choosing code anchors."""
    if not isinstance(body, str):
        raise ValueError("A decoded text body is required")
    results = []
    quoted = []
    remarks = []
    headers_by_depth: dict[int, list[str]] = {}
    depth = quote_start = 0

    def flush() -> None:
        nonempty = [(number, text) for number, text in remarks if text.strip()]
        if quoted and nonempty:
            results.append(
                QuotedRemark(
                    tuple(quoted),
                    tuple(headers_by_depth.get(depth, ())),
                    "\n".join(text for _, text in remarks).strip(),
                    depth,
                    quote_start,
                    nonempty[0][0],
                    nonempty[-1][0],
                )
            )
        remarks.clear()

    for number, line in enumerate(body.splitlines(), start=1):
        if not line.startswith(">"):
            trailer = _TRAILER_RE.fullmatch(line)
            if line == "-- " or (
                trailer
                and trailer.group("name").lower()
                in {
                    "reviewed-by",
                    "acked-by",
                    "tested-by",
                    "signed-off-by",
                    "reported-by",
                    "suggested-by",
                    "co-developed-by",
                }
            ):
                flush()
                break
            if quoted:
                remarks.append((number, line))
            continue

        prefix_end = current_depth = 0
        while prefix_end < len(line) and line[prefix_end] == ">":
            current_depth += 1
            prefix_end += 1
            if prefix_end < len(line) and line[prefix_end] == " ":
                prefix_end += 1
        if any(text.strip() for _, text in remarks) or current_depth != depth:
            flush()
            quoted.clear()
        remarks.clear()
        depth = current_depth
        if not quoted:
            quote_start = number
        raw = line[prefix_end:]
        headers = headers_by_depth.setdefault(depth, [])
        if raw.startswith("diff --git "):
            headers[:] = [raw]
        elif raw.startswith(("--- ", "+++ ")):
            header_kind = raw[:4]
            headers[:] = [
                header for header in headers if not header.startswith(header_kind)
            ]
            headers.append(raw)
        quoted.append(raw)
    else:
        flush()
    return tuple(results)


def _message_id(header: object | None) -> str | None:
    if header is None:
        return None
    value = str(header).strip()
    if len(value) > 998:
        return None
    bracketed = _MESSAGE_ID_RE.fullmatch(value)
    if bracketed:
        return bracketed.group(1)
    if _BARE_MESSAGE_ID_RE.fullmatch(value):
        return value
    return None


def _message_ids(header: object | None) -> tuple[str, ...]:
    if header is None:
        return ()
    value = str(header)
    if len(value) > MAX_THREAD_HEADER_BYTES:
        raise ValueError("Thread reference header exceeds the 64 KiB limit")
    ids = []
    for match in _MESSAGE_ID_RE.finditer(value):
        if len(ids) >= MAX_THREAD_REFERENCES:
            raise ValueError(
                "Thread reference header exceeds the 1000 Message-ID limit"
            )
        ids.append(match.group(1))
    if ids:
        return tuple(ids)
    bare = _BARE_MESSAGE_ID_RE.fullmatch(value.strip())
    return (bare.group(0),) if bare else ()


def _message_time_estimate(message: Message) -> MessageTimeEstimate | None:
    for name, source in (("Received", "received"), ("Date", "date")):
        for header in message.get_all(name, []):
            value = str(header)
            if len(value) > MAX_THREAD_HEADER_BYTES:
                continue
            if source == "received":
                _, separator, value = value.rpartition(";")
                if not separator:
                    continue
            try:
                parsed = parsedate_to_datetime(value.strip())
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    return MessageTimeEstimate(
                        parsed.astimezone(timezone.utc).isoformat(), source
                    )
            except (TypeError, ValueError, OverflowError, IndexError):
                continue
    return None


def _subject_metadata(
    subject: str, has_thread_parent: bool
) -> tuple[MessageKind, int | None, int | None, int | None, str]:
    if _REPLY_PREFIX_RE.match(subject):
        return "reply", None, None, None, _REPLY_PREFIX_RE.sub("", subject).strip()

    patch_tag = None
    for match in _SUBJECT_TAG_RE.finditer(subject):
        if _PATCH_TOKEN_RE.search(match.group(1)):
            patch_tag = match.group(1)
            break

    if patch_tag is not None:
        version_match = _PATCH_VERSION_RE.search(patch_tag)
        index_match = _PATCH_INDEX_RE.search(patch_tag)
        patch_version = (
            _bounded_header_number(version_match.group(1)) if version_match else None
        )
        patch_index = (
            _bounded_header_number(index_match.group(1)) if index_match else 1
        )
        series_size = (
            _bounded_header_number(index_match.group(2)) if index_match else 1
        )
        kind: MessageKind = "cover" if patch_index == 0 else "patch"
        title = _SUBJECT_TAG_RE.sub("", subject)
        title = _REPLY_PREFIX_RE.sub("", title).strip(" \t:-")
        return kind, patch_version, patch_index, series_size, title

    if has_thread_parent:
        return "reply", None, None, None, _REPLY_PREFIX_RE.sub("", subject).strip()
    return "other", None, None, None, subject.strip()


def _body_text(message: Message) -> tuple[str, str | None]:
    part = (
        message.get_body(preferencelist=("plain",))
        if message.is_multipart()
        else message
    )
    if part is None or part.get_content_type() != "text/plain":
        return "", "No text/plain body was available"
    try:
        content = part.get_content()
    except (LookupError, UnicodeError) as error:
        return "", f"Could not decode message body: {error}"
    if isinstance(content, bytes):
        content = content.decode(
            part.get_content_charset() or "utf-8", errors="replace"
        )
    return str(content), None


def _trailers(body: str, is_patch: bool) -> tuple[Trailer, ...]:
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    signature = next((index for index, line in enumerate(lines) if line == "-- "), None)
    if signature is not None:
        lines = lines[:signature]
    if is_patch:
        diff_separator = next(
            (index for index, line in enumerate(lines) if line == "---"), None
        )
        if diff_separator is not None:
            lines = lines[:diff_separator]

    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    found_trailer = False
    while start:
        line = lines[start - 1]
        if _TRAILER_RE.fullmatch(line):
            found_trailer = True
            start -= 1
        elif found_trailer and line[:1] in (" ", "\t"):
            start -= 1
        else:
            break
    if not found_trailer:
        return ()

    result: list[Trailer] = []
    for line in lines[start:end]:
        match = _TRAILER_RE.fullmatch(line)
        if match:
            result.append(Trailer(match.group("name"), match.group("value")))
        elif result:
            previous = result[-1]
            result[-1] = Trailer(previous.name, f"{previous.value} {line.strip()}")
    return tuple(result)


def parse_message(raw_message: bytes) -> ParsedMessage:
    raw_message = bytes(raw_message)
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    errors = [type(defect).__name__ for defect in message.defects]

    message_id = _message_id(message.get("Message-ID"))
    if message_id is None:
        errors.append("Missing or invalid Message-ID")

    sender_headers = [str(value) for value in message.get_all("From", [])]
    senders = getaddresses(sender_headers)
    from_name, from_address = next(
        ((name, address) for name, address in senders if address), (None, None)
    )

    subject = str(message.get("Subject", ""))
    references = _message_ids(message.get("References"))
    in_reply_to = _message_ids(message.get("In-Reply-To"))
    kind, patch_version, patch_index, series_size, series_title = _subject_metadata(
        subject, bool(references or in_reply_to)
    )
    body_text, body_error = _body_text(message)
    if body_error:
        errors.append(body_error)

    return ParsedMessage(
        raw_message=raw_message,
        sha256=hashlib.sha256(raw_message).hexdigest(),
        message_id=message_id,
        from_name=from_name or None,
        from_address=from_address or None,
        subject=subject,
        references=references,
        in_reply_to=in_reply_to,
        kind=kind,
        patch_version=patch_version,
        patch_index=patch_index,
        series_size=series_size,
        subject_title=series_title,
        trailers=_trailers(body_text, kind in ("patch", "cover")),
        body_text=body_text,
        errors=tuple(errors),
        time_estimate=_message_time_estimate(message),
    )


def parse_mbox(path: Path) -> tuple[ParsedMessage, ...]:
    """Parse all messages in an mbox, preserving each extracted RFC message."""
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Expected an mbox file: {path}")
    if path.stat().st_size > MAX_MBOX_BYTES:
        raise ValueError(f"Mbox exceeds the {MAX_MBOX_BYTES}-byte limit")

    box = mailbox.mbox(path, create=False)
    try:
        messages = []
        for key in box.iterkeys():
            if len(messages) >= MAX_MBOX_MESSAGES:
                raise ValueError(f"Mbox exceeds the {MAX_MBOX_MESSAGES}-message limit")
            messages.append(parse_message(box.get_bytes(key)))
    finally:
        box.close()
    if messages:
        return tuple(messages)

    raw_input = path.read_bytes()
    if not raw_input:
        return ()
    result = parse_message(raw_input)
    return (
        replace(
            result,
            errors=("No mbox records found; retained the raw input", *result.errors),
        ),
    )
