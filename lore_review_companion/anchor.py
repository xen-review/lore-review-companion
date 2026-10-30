# SPDX-License-Identifier: GPL-2.0-or-later

"""Conservative mapping of quoted review context to Forgejo diff positions."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from lore_review_companion.mail_parser import QuotedRemark


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class Anchor:
    path: str
    old_position: int
    new_position: int


@dataclass(frozen=True)
class RangeAnchor(Anchor):
    extra_lines_count: int


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
        self._positions: dict[tuple[str, str], list[tuple[Anchor, str]]] = defaultdict(
            list
        )
        self._ranges: dict[tuple[str, str, int], list[tuple[str, int]]] = defaultdict(
            list
        )
        old_path = ""
        new_path = ""
        old_line = 0
        new_line = 0
        in_hunk = False
        hunk_index = 0

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
                hunk_index += 1
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
                self._ranges[(path, "new", hunk_index)].append((line, new_line))
                self._positions[(path, line[1:])].append(
                    (Anchor(path, old_position=0, new_position=new_line), "+")
                )
                new_line += 1
            elif line.startswith("-"):
                self._ranges[(path, "old", hunk_index)].append((line, old_line))
                self._positions[(path, line[1:])].append(
                    (Anchor(path, old_position=old_line, new_position=0), "-")
                )
                old_line += 1
            elif line.startswith(" "):
                self._ranges[(path, "old", hunk_index)].append((line, old_line))
                self._ranges[(path, "new", hunk_index)].append((line, new_line))
                self._positions[(path, line[1:])].append(
                    (Anchor(path, old_position=0, new_position=new_line), " ")
                )
                old_line += 1
                new_line += 1

    def resolve_quote(
        self, remark: QuotedRemark, *, max_lines: int = 50
    ) -> RangeAnchor | None:
        """Resolve a complete exact quote on one side without line-hint guesses."""
        if (
            not isinstance(max_lines, int)
            or isinstance(max_lines, bool)
            or max_lines < 1
        ):
            raise ValueError("The maximum selected line count must be positive")
        paths = set()
        for header in (*remark.headers, *remark.quote):
            if header.startswith(("--- ", "+++ ")):
                path = _header_path(header, header[:4])
                if path:
                    paths.add(path)
        if len(paths) != 1:
            return None
        selected_path = next(iter(paths))
        quote = list(remark.quote)
        while quote and quote[-1] == "":
            quote.pop()
        tokens = []
        for line in quote:
            if line.startswith(
                ("diff --git ", "index ", "--- ", "+++ ")
            ) or _HUNK_RE.match(line):
                if tokens:
                    return None
                continue
            if line.startswith("\\ No newline"):
                continue
            if not line or line[0] not in ("+", "-", " "):
                return None
            tokens.append(line)
        if not tokens or len(tokens) > max_lines:
            return None
        markers = {line[0] for line in tokens}
        if "+" in markers and "-" in markers:
            return None
        side = "old" if "-" in markers else "new"
        candidates = set()
        for (path, candidate_side, _), lines in self._ranges.items():
            if path != selected_path or candidate_side != side:
                continue
            for offset in range(len(lines) - len(tokens) + 1):
                window = lines[offset : offset + len(tokens)]
                if [content for content, _ in window] != tokens:
                    continue
                start = window[0][1]
                if [number for _, number in window] != list(
                    range(start, start + len(tokens))
                ):
                    continue
                candidates.add(
                    RangeAnchor(
                        path,
                        start if side == "old" else 0,
                        start if side == "new" else 0,
                        len(tokens) - 1,
                    )
                )
        return next(iter(candidates)) if len(candidates) == 1 else None

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
            anchor
            for candidate_path in paths
            for anchor, candidate_side in self._positions.get(
                (candidate_path, line), []
            )
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


def resolve_comments(
    diff_text: str, comments: Iterable[dict[str, object]]
) -> list[Anchor | None]:
    """Resolve extracted b4 comments in input order; unresolved comments stay None."""
    index = DiffIndex(diff_text)
    return [index.resolve(comment) for comment in comments]
