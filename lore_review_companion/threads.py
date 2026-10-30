"""Match replies to explicitly referenced series members without subject guessing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from lore_review_companion.mail_parser import ParsedMessage
from lore_review_companion.series import SeriesRevision

@dataclass(frozen=True)
class ThreadMatch:
    message: ParsedMessage
    series_key: str
    revision: int
    patch_message_id: str | None


@dataclass(frozen=True)
class ThreadMatches:
    matched: tuple[ThreadMatch, ...]
    unmatched: tuple[ParsedMessage, ...]


def match_thread_replies(
    messages: Iterable[ParsedMessage], revisions: Iterable[SeriesRevision],
) -> ThreadMatches:
    by_id: dict[str, ParsedMessage] = {}
    conflicts: set[str] = set()
    for message in messages:
        if message.message_id is None:
            continue
        previous = by_id.get(message.message_id)
        if previous and previous.sha256 != message.sha256:
            conflicts.add(message.message_id)
        else:
            by_id[message.message_id] = message

    members: dict[str, set[tuple[str, int, str, str]]] = {}
    for revision in revisions:
        for message in revision.messages:
            if message.message_id:
                members.setdefault(message.message_id, set()).add(
                    (revision.series_key, revision.revision, message.message_id, message.kind)
                )

    resolved: dict[str, set[tuple[str, int, str, str]]] = {}

    def resolve(message_id: str) -> set[tuple[str, int, str, str]]:
        stack = [(message_id, False)]
        active: set[str] = set()
        dependencies: dict[str, tuple[str, ...]] = {}
        while stack:
            current, expanded = stack.pop()
            if not expanded and current in resolved:
                continue
            if not expanded:
                if current in conflicts:
                    resolved[current] = set()
                    continue
                if current in members:
                    resolved[current] = members[current]
                    continue
                message = by_id.get(current)
                if message is None or message.kind != "reply":
                    resolved[current] = set()
                    continue
                known_parents = tuple(parent for parent in message.in_reply_to if parent in by_id or parent in members)
                if known_parents and len(known_parents) != len(message.in_reply_to):
                    resolved[current] = set()
                    continue
                if not known_parents:
                    known_parents = next(
                        ((parent,) for parent in reversed(message.references) if parent in by_id or parent in members),
                        (),
                    )
                dependencies[current] = known_parents
                active.add(current)
                stack.append((current, True))
                for parent in reversed(known_parents):
                    if parent not in active and parent not in resolved:
                        stack.append((parent, False))
                continue
            targets = [set() if parent in active else resolved[parent] for parent in dependencies[current]]
            resolved[current] = set().union(*targets) if targets and all(targets) else set()
            active.remove(current)
        return resolved[message_id]

    matched: list[ThreadMatch] = []
    unmatched: list[ParsedMessage] = []
    replies = sorted(
        (message for message in by_id.values() if message.kind == "reply"),
        key=lambda message: message.message_id or "",
    )
    for reply in replies:
        targets = resolve(reply.message_id or "")
        series_revisions = {(series_key, revision) for series_key, revision, _, _ in targets}
        if len(series_revisions) != 1:
            unmatched.append(reply)
            continue
        series_key, revision = next(iter(series_revisions))
        patches = {message_id for _, _, message_id, kind in targets if kind == "patch"}
        patch_id = next(iter(patches)) if len(targets) == 1 and len(patches) == 1 else None
        matched.append(ThreadMatch(reply, series_key, revision, patch_id))
    return ThreadMatches(tuple(matched), tuple(unmatched))