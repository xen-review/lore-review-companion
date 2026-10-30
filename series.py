"""Discover patch revisions from explicit RFC 5322 thread relationships."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from mail_parser import ParsedMessage

MAX_SERIES_PATCHES = 500
_CHANGE_ID = re.compile(r"^change-id:[ \t]+([A-Za-z0-9][A-Za-z0-9._+-]{0,199})[ \t]*$", re.IGNORECASE | re.MULTILINE)
_CHANGE_ID_LINE = re.compile(r"^change-id:[^\n]*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class SeriesRevision:
    series_key: str
    root_message_id: str
    revision: int
    title: str
    expected_patch_count: int | None
    cover_message_id: str | None
    messages: tuple[ParsedMessage, ...]
    complete: bool
    conflicts: tuple[str, ...]


def _change_id_bodies(revision: SeriesRevision) -> Iterable[str]:
    return (
        message.body_text.replace("\r\n", "\n").replace("\r", "\n")
        for message in revision.messages if message.kind == "cover" or message.patch_index == 1
    )


def change_id_candidates(revision: SeriesRevision) -> tuple[str, ...]:
    values = {
        match.group(1)
        for body in _change_id_bodies(revision)
        for match in _CHANGE_ID.finditer(body)
    }
    return tuple(sorted(values))


def has_invalid_change_id_claims(revision: SeriesRevision) -> bool:
    return any(
        _CHANGE_ID.fullmatch(match.group()) is None
        for body in _change_id_bodies(revision)
        for match in _CHANGE_ID_LINE.finditer(body)
    )


def discover_series(messages: Iterable[ParsedMessage]) -> tuple[SeriesRevision, ...]:
    candidates = [
        message
        for message in messages
        if message.message_id and message.kind in ("cover", "patch")
    ]
    by_id = {message.message_id: message for message in candidates}
    id_counts: dict[str, int] = defaultdict(int)
    for message in candidates:
        id_counts[message.message_id] += 1
    root_cache: dict[str, tuple[str, tuple[str, ...]]] = {}

    def root_for(message: ParsedMessage) -> tuple[str, tuple[str, ...]]:
        stack = [(message.message_id, False)]
        active: set[str] = set()
        while stack:
            message_id, expanded = stack.pop()
            if not expanded and message_id in root_cache:
                continue
            current = by_id[message_id]
            references = tuple(dict.fromkeys((*current.references, *current.in_reply_to)))
            if not expanded:
                active.add(message_id)
                stack.append((message_id, True))
                for parent_id in reversed(references):
                    if parent_id in by_id and parent_id not in active and parent_id not in root_cache:
                        stack.append((parent_id, False))
                continue
            errors = []
            if message_id in references:
                errors.append("thread ancestry contains a cycle")
            roots = []
            for parent_id in references:
                if parent_id != message_id and parent_id in by_id:
                    if parent_id in active:
                        root_id, parent_errors = parent_id, ("thread ancestry contains a cycle",)
                    else:
                        root_id, parent_errors = root_cache[parent_id]
                    roots.append(root_id)
                    errors.extend(parent_errors)
            if len(set(roots)) > 1:
                errors.append("thread ancestry identifies multiple roots")
            root_id = roots[0] if roots else references[0] if references else message_id
            root_cache[message_id] = root_id, tuple(dict.fromkeys(errors))
            active.remove(message_id)
        return root_cache[message.message_id]

    grouped: dict[tuple[str, int], list[ParsedMessage]] = defaultdict(list)
    ancestry_errors: dict[tuple[str, int], set[str]] = defaultdict(set)
    for message in candidates:
        revision = message.patch_version or 1
        root_id, errors = root_for(message)
        key = root_id, revision
        grouped[key].append(message)
        ancestry_errors[key].update(errors)
        if id_counts[message.message_id] > 1:
            ancestry_errors[key].add(f"duplicate Message-ID: {message.message_id}")

    revisions: list[SeriesRevision] = []
    for (root_id, revision), members in sorted(grouped.items()):
        members.sort(key=lambda item: (item.patch_index is None, item.patch_index or 0, item.message_id or ""))
        covers = [message for message in members if message.kind == "cover"]
        patches = [message for message in members if message.kind == "patch"]
        conflicts: list[str] = sorted(ancestry_errors[(root_id, revision)])
        expected_counts = {message.series_size for message in members if message.series_size is not None}
        if len(expected_counts) > 1:
            conflicts.append("messages disagree on the series patch count")
        expected_count = next(iter(expected_counts), None)
        if len(covers) > 1:
            conflicts.append("multiple cover messages identify the same revision")

        indexed: dict[int, list[ParsedMessage]] = defaultdict(list)
        for patch in patches:
            if patch.patch_index is None:
                conflicts.append(f"patch has no series index: {patch.message_id}")
            else:
                indexed[patch.patch_index].append(patch)
        for patch_index, duplicates in indexed.items():
            if len(duplicates) > 1:
                conflicts.append(f"duplicate patch index {patch_index}")
        if expected_count is None:
            conflicts.append("series patch count is unknown")
        elif not 1 <= expected_count <= MAX_SERIES_PATCHES:
            conflicts.append(f"series patch count is outside 1..{MAX_SERIES_PATCHES}")
        else:
            missing = sorted(set(range(1, expected_count + 1)) - indexed.keys())
            if missing:
                conflicts.append(
                    "missing patch indices: " + ", ".join(str(index) for index in missing)
                )
            out_of_range = sorted(index for index in indexed if index < 1 or index > expected_count)
            if out_of_range:
                conflicts.append(
                    "patch indices outside declared range: "
                    + ", ".join(str(index) for index in out_of_range)
                )

        title = next((cover.subject_title for cover in covers if cover.subject_title), "")
        if not title:
            title = next((patch.subject_title for patch in patches if patch.subject_title), "")
        revisions.append(
            SeriesRevision(
                series_key=f"lore:{root_id}",
                root_message_id=root_id,
                revision=revision,
                title=title,
                expected_patch_count=expected_count,
                cover_message_id=covers[0].message_id if len(covers) == 1 else None,
                messages=tuple(members),
                complete=not conflicts,
                conflicts=tuple(conflicts),
            )
        )
    return tuple(revisions)