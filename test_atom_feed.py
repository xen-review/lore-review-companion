import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from atom_feed import AtomFeedError, parse_atom_feed
from ledger import Ledger


FEED_URL = "https://lore.kernel.org/xen-devel/new.atom"

FEED = b'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:thr="http://purl.org/syndication/thread/1.0">
  <id>mailto:xen-devel@lists.xen.org</id>
  <updated>2026-10-28T00:00:00Z</updated>
  <entry>
    <author><name>Ada Maintainer</name><email>ada@example.test</email></author>
    <title>[PATCH v2 1/2] xen: fix a boundary</title>
    <updated>2026-10-28T00:00:00Z</updated>
    <link href="http://lore.kernel.org/xen-devel/patch@example.test/"/>
    <id>urn:uuid:entry-1</id>
    <thr:in-reply-to ref="urn:uuid:entry-cover"
      href="https://lore.kernel.org/xen-devel/cover@example.test/"/>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
      <pre>Patch body is not parsed as RFC mail.</pre>
    </div></content>
  </entry>
  <entry>
    <title>Malformed entry</title>
    <updated>2026-10-28T00:01:00Z</updated>
    <link href="https://attacker.example/collect"/>
    <id>urn:uuid:entry-2</id>
  </entry>
</feed>'''


class AtomFeedTests(unittest.TestCase):
    def test_parses_entry_metadata_and_rejects_out_of_inbox_links(self):
        feed = parse_atom_feed(FEED, FEED_URL)

        self.assertEqual(feed.feed_id, "mailto:xen-devel@lists.xen.org")
        self.assertEqual(len(feed.entries), 2)
        first, second = feed.entries
        self.assertEqual(first.entry_id, "urn:uuid:entry-1")
        self.assertEqual(first.author_email, "ada@example.test")
        self.assertEqual(first.parent_entry_id, "urn:uuid:entry-cover")
        self.assertEqual(
            first.permalink,
            "https://lore.kernel.org/xen-devel/patch@example.test/",
        )
        self.assertEqual(
            first.parent_url,
            "https://lore.kernel.org/xen-devel/cover@example.test/",
        )
        self.assertIsNone(second.permalink)
        self.assertIn("Missing or out-of-inbox Atom entry link", second.errors)

    def test_rejects_non_atom_malformed_xml_and_non_https_source(self):
        for body, url in (
            (b"<html/>", FEED_URL),
            (b"<feed>", FEED_URL),
            (FEED.replace(b"<id>mailto:xen-devel@lists.xen.org</id>", b""), FEED_URL),
            (FEED.replace(b"<updated>2026-10-28T00:00:00Z</updated>", b"", 1), FEED_URL),
            (FEED, "http://lore.kernel.org/xen-devel/new.atom"),
        ):
            with self.subTest(body=body[:12], url=url):
                with self.assertRaises(AtomFeedError):
                    parse_atom_feed(body, url)

    def test_rejects_same_host_links_that_escape_inbox_path(self):
        for href in (
            "https://lore.kernel.org/xen-devel/../outside/message/",
            "https://lore.kernel.org/xen-devel/%2e%2e/outside/message/",
        ):
            feed = parse_atom_feed(
                FEED.replace(
                    b"http://lore.kernel.org/xen-devel/patch@example.test/",
                    href.encode(),
                ),
                FEED_URL,
            )
            self.assertIsNone(feed.entries[0].permalink)

    def test_preserves_escaped_permalink_path(self):
        href = "https://lore.kernel.org/xen-devel/id%23fragment/"
        feed = parse_atom_feed(
            FEED.replace(
                b"http://lore.kernel.org/xen-devel/patch@example.test/",
                href.encode(),
            ),
            FEED_URL,
        )
        self.assertEqual(feed.entries[0].permalink, href)

    def test_feed_checkpoint_replays_overlapping_entries(self):
        feed = parse_atom_feed(FEED, FEED_URL)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "feed.sqlite3")

            first = ledger.record_atom_feed(FEED_URL, feed)
            replay = ledger.record_atom_feed(FEED_URL, feed)

            self.assertEqual((first.new_entries, first.replayed_entries), (2, 0))
            self.assertEqual((replay.new_entries, replay.replayed_entries), (0, 2))
            self.assertEqual(first.invalid_entries, 1)
            with closing(ledger._connect()) as connection:
                count = connection.execute("SELECT count(*) FROM atom_entries").fetchone()[0]
                checkpoint = connection.execute(
                    "SELECT feed_updated_at FROM feed_checkpoints WHERE feed_url = ?",
                    (FEED_URL,),
                ).fetchone()[0]
            self.assertEqual(count, 2)
            self.assertEqual(checkpoint, feed.updated_at)

    def test_unkeyed_entry_does_not_advance_checkpoint(self):
        feed_without_entry_id = parse_atom_feed(
            FEED.replace(b"<id>urn:uuid:entry-2</id>", b""), FEED_URL
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "feed.sqlite3")

            summary = ledger.record_atom_feed(FEED_URL, feed_without_entry_id)
            with closing(ledger._connect()) as connection:
                checkpoint = connection.execute(
                    "SELECT feed_updated_at FROM feed_checkpoints WHERE feed_url = ?",
                    (FEED_URL,),
                ).fetchone()
            self.assertEqual(summary.invalid_entries, 1)
            self.assertIsNone(checkpoint)

            complete_feed = parse_atom_feed(FEED, FEED_URL)
            ledger.record_atom_feed(FEED_URL, complete_feed)
            with closing(ledger._connect()) as connection:
                checkpoint = connection.execute(
                    "SELECT feed_updated_at FROM feed_checkpoints WHERE feed_url = ?",
                    (FEED_URL,),
                ).fetchone()[0]
            self.assertEqual(checkpoint, complete_feed.updated_at)


if __name__ == "__main__":
    unittest.main()