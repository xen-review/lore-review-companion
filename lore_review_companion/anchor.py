"""Conservative mapping of quoted review lines to Gitea diff positions."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class Anchor:
    path: str
    old_position: int
    new_position: int


def _header_path(value: str, prefix: str) -> str:
    value = value[len(prefix) :].split("\t", 1)[0]
    if value == "/dev/null":
        return ""
    if value.startswith('"'):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return ""
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def _quoted_line(content: str) -> tuple[str, str | None]:
    content = content.rstrip("\r\n")
    while content.startswith(">"):
        content = content[1:]
        if content.startswith(" "):
            content = content[1:]
    if content[:1] in ("+", "-", " "):
        return content[1:], content[0]
    return content, None


class DiffIndex:
    """Index changed and context lines from a unified Git diff."""

    def __init__(self, diff_text: str):
        self._positions: dict[tuple[str, str], list[tuple[Anchor, str]]] = defaultdict(list)
        old_path = ""
        new_path = ""
        old_line = 0
        new_line = 0
        in_hunk = False

        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                in_hunk = False
                old_path = ""
                new_path = ""
                continue

            hunk = _HUNK_RE.match(line)
            if hunk:
                old_line = int(hunk.group(1))
                new_line = int(hunk.group(2))
                in_hunk = True
                continue

            if not in_hunk:
                if line.startswith("--- "):
                    old_path = _header_path(line, "--- ")
                elif line.startswith("+++ "):
                    new_path = _header_path(line, "+++ ")
                continue

            if line.startswith("\\"):
                continue
            if old_path and new_path and old_path != new_path:
                continue
            path = new_path or old_path
            if line.startswith("+"):
                self._positions[(path, line[1:])].append(
                    (Anchor(path, old_position=0, new_position=new_line), "+")
                )
                new_line += 1
            elif line.startswith("-"):
                self._positions[(path, line[1:])].append(
                    (Anchor(path, old_position=old_line, new_position=0), "-")
                )
                old_line += 1
            elif line.startswith(" "):
                self._positions[(path, line[1:])].append(
                    (Anchor(path, old_position=0, new_position=new_line), " ")
                )
                old_line += 1
                new_line += 1

    def resolve(self, comment: dict[str, object]) -> Anchor | None:
        """Return a unique exact match, using b4's line hint only to disambiguate."""
        path = comment.get("path")
        content = comment.get("content")
        if not isinstance(path, str) or not path or not isinstance(content, str):
            return None

        line, side = _quoted_line(content)
        paths = {path}
        if path.startswith(("a/", "b/")):
            paths.add(path[2:])
        candidates = [
            anchor for candidate_path in paths
            for anchor, candidate_side in self._positions.get((candidate_path, line), [])
            if side is None or candidate_side == side
        ]
        if len({candidate.path for candidate in candidates}) > 1:
            return None
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) < 2:
            return None

        line_hint = comment.get("line")
        if not isinstance(line_hint, int) or isinstance(line_hint, bool):
            return None
        hinted = [
            anchor
            for anchor in candidates
            if anchor.old_position == line_hint or anchor.new_position == line_hint
        ]
        return hinted[0] if len(hinted) == 1 else None


def resolve_comments(diff_text: str, comments: Iterable[dict[str, object]]) -> list[Anchor | None]:
    """Resolve extracted b4 comments in input order; unresolved comments stay None."""
    index = DiffIndex(diff_text)
    return [index.resolve(comment) for comment in comments]