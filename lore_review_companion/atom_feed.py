# SPDX-License-Identifier: GPL-2.0-or-later

"""Parse public-inbox Atom feed metadata without treating it as raw mail."""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from posixpath import normpath
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


ATOM_NS = "http://www.w3.org/2005/Atom"
THREAD_NS = "http://purl.org/syndication/thread/1.0"


@dataclass(frozen=True)
class AtomEntry:
    entry_id: str | None
    title: str
    updated_at: str
    permalink: str | None
    author_name: str | None
    author_email: str | None
    parent_entry_id: str | None
    parent_url: str | None
    errors: tuple[str, ...]


@dataclass(frozen=True)
class AtomFeed:
    feed_id: str | None
    updated_at: str
    entries: tuple[AtomEntry, ...]


class AtomFeedError(ValueError):
    """The source bytes are not an Atom feed for the configured inbox."""


def parse_atom_feed(feed_bytes: bytes, feed_url: str) -> AtomFeed:
    try:
        root = ElementTree.fromstring(feed_bytes)
    except ElementTree.ParseError as error:
        raise AtomFeedError("Invalid Atom XML") from error
    if root.tag != f"{{{ATOM_NS}}}feed":
        raise AtomFeedError("Expected an Atom feed document")

    parsed_feed_url = urlsplit(feed_url)
    if parsed_feed_url.scheme != "https" or not parsed_feed_url.hostname:
        raise AtomFeedError("Feed URL must be an absolute HTTPS URL")
    try:
        feed_port = parsed_feed_url.port
    except ValueError as error:
        raise AtomFeedError("Feed URL has an invalid port") from error
    inbox_path = normpath(parsed_feed_url.path.rsplit("/", 1)[0]).rstrip("/") + "/"

    def inbox_url(href: str | None) -> str | None:
        if not href:
            return None
        try:
            resolved = urlsplit(urljoin(feed_url, href))
            resolved_port = resolved.port
        except ValueError:
            return None
        path = unquote(resolved.path)
        if (
            resolved.scheme not in ("http", "https")
            or resolved.hostname != parsed_feed_url.hostname
            or resolved_port != feed_port
            or resolved.username is not None
            or resolved.password is not None
            or "\\" in path
            or not normpath(path).startswith(inbox_path)
            or resolved.query
        ):
            return None
        return urlunsplit(("https", resolved.netloc, resolved.path, "", ""))

    feed_id = _text(root, f"{{{ATOM_NS}}}id")
    updated_at = _text(root, f"{{{ATOM_NS}}}updated")
    if not feed_id or not updated_at:
        raise AtomFeedError("Atom feed is missing its ID or updated timestamp")
    entries: list[AtomEntry] = []
    for element in root.findall(f"{{{ATOM_NS}}}entry"):
        entry_id = _text(element, f"{{{ATOM_NS}}}id")
        title = _text(element, f"{{{ATOM_NS}}}title") or ""
        entry_updated = _text(element, f"{{{ATOM_NS}}}updated") or ""
        errors: list[str] = []
        if not entry_id:
            errors.append("Missing Atom entry ID")
        if not entry_updated:
            errors.append("Missing Atom entry updated timestamp")

        permalink = None
        for link in element.findall(f"{{{ATOM_NS}}}link"):
            if link.get("rel", "alternate") != "alternate":
                continue
            permalink = inbox_url(link.get("href"))
            break
        if permalink is None:
            errors.append("Missing or out-of-inbox Atom entry link")

        author = element.find(f"{{{ATOM_NS}}}author")
        author_name = _text(author, f"{{{ATOM_NS}}}name") if author is not None else None
        author_email = _text(author, f"{{{ATOM_NS}}}email") if author is not None else None

        parent = element.find(f"{{{THREAD_NS}}}in-reply-to")
        parent_entry_id = parent.get("ref") if parent is not None else None
        parent_url = inbox_url(parent.get("href")) if parent is not None else None
        if parent is not None and parent.get("href") and parent_url is None:
            errors.append("Ignored out-of-inbox thread parent link")

        entries.append(
            AtomEntry(
                entry_id=entry_id,
                title=title,
                updated_at=entry_updated,
                permalink=permalink,
                author_name=author_name,
                author_email=author_email,
                parent_entry_id=parent_entry_id,
                parent_url=parent_url,
                errors=tuple(errors),
            )
        )

    return AtomFeed(feed_id=feed_id, updated_at=updated_at, entries=tuple(entries))


def _text(element: ElementTree.Element | None, tag: str) -> str | None:
    if element is None:
        return None
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None