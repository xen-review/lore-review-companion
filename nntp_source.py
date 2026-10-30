"""TLS NNTP discovery with durable Message-ID queueing."""

from __future__ import annotations

import importlib
import io
import re
import shutil
import ssl
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesHeaderParser
from typing import Callable

from ledger import ArchiveSafetyBlocked, Ledger, NNTPCheckpoint, NNTPNumberCursor
from mail_parser import MessageTimeEstimate, parse_message
from public_inbox import MAX_THREAD_COMPRESSED, MAX_THREAD_UNCOMPRESSED, fetch_public_inbox_thread

DEFAULT_HOST = "nntp.lore.kernel.org"
DEFAULT_GROUP = "org.xenproject.lists.xen-devel"
DEFAULT_INBOX = "https://lore.kernel.org/xen-devel/"
MAX_DISCOVERY_BYTES = 8 * 1024 * 1024
MAX_ARTICLE_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024
MIN_FREE_BYTES = 256 * 1024 * 1024


class NNTPSourceError(RuntimeError):
    """An NNTP operation failed without safely acknowledging its contents."""


class _BoundedBuffer(io.BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.limit:
            raise NNTPSourceError("NNTP response exceeds its byte budget; current item not acknowledged")
        return super().write(data)


@dataclass(frozen=True)
class NNTPDiscovery:
    checkpoint: NNTPCheckpoint
    discovered: int
    newly_queued: int
    queried_since: str
    response_bytes: int
    numeric_cursor: NNTPNumberCursor
    receipt_time_coverage_verified: bool = False
    timestamp_basis: str = "server index; may fall back to sender Date"


@dataclass(frozen=True)
class NNTPFetch:
    source: str
    ingested: int
    replayed: int
    received_bytes: int
    dependencies_queued: int
    has_more: bool
    receipt_times_verified: bool = False
    timeout_retries: int = 0


@dataclass(frozen=True)
class NNTPNumberDiscovery:
    cursor: NNTPNumberCursor
    group_low: int
    group_high: int
    scanned: int
    newly_queued: int
    response_bytes: int
    has_more: bool


@dataclass(frozen=True)
class NNTPThreadDiscovery:
    source: str
    discovered: int
    newly_queued: int
    compressed_bytes: int
    uncompressed_bytes: int
    thread_coverage_verified: bool = False
    from_index_time: int | None = None
    through_index_time: int | None = None
    empty_or_unavailable: bool = False


@dataclass(frozen=True)
class NNTPWindowInspection:
    source: str
    message_id: str
    cutoff: str
    bootstrap_at: str
    time_estimate: MessageTimeEstimate | None
    estimated_window_position: str
    receipt_time_coverage_verified: bool = False


@contextmanager
def _client(host: str):
    try:
        library = importlib.import_module("nntplib")
    except ImportError as error:
        raise NNTPSourceError("NNTP requires the companion requirements.txt on Python 3.13 or later") from error

    class UTCReader(library.NNTP_SSL):
        """The library's NEWNEWS helper omits the protocol's UTC selector."""

        def newnews(self, group, date, *, file=None):
            command = f"NEWNEWS {group} {date:%Y%m%d %H%M%S} GMT"
            return self._longcmdstring(command, file)

        def group_validity(self, group):
            with _BoundedBuffer(4096) as output:
                response, _ = self._longcmdstring(f"LIST ACTIVE.TIMES {group}", output)
                try:
                    lines = output.getvalue().decode("ascii").splitlines()
                except UnicodeDecodeError as error:
                    raise NNTPSourceError("NNTP group validity was not ASCII") from error
            fields = lines[0].split() if len(lines) == 1 else []
            if not response.startswith("215 ") or len(fields) != 3 or fields[0] != group or not fields[1].isdecimal():
                raise NNTPSourceError("NNTP did not provide the requested group's validity")
            validity = int(fields[1])
            if not 1 <= validity < 9223372036854775807:
                raise NNTPSourceError("NNTP group validity is outside the supported range")
            return validity

    connection = None
    try:
        connection = UTCReader(host, ssl_context=ssl.create_default_context(), timeout=20, usenetrc=False)
        yield connection
    except library.NNTPError as error:
        match = re.match(r"([1-5][0-9]{2})(?: |$)", str(error))
        status = f" ({match.group(1)})" if match else ""
        raise NNTPSourceError(f"NNTP server rejected a request{status}; pending work retained") from None
    finally:
        if connection is not None:
            connection._close()


def nntp_source(host: str, group: str) -> str:
    if (
        not isinstance(host, str) or len(host) > 253
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) is None
        or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split("."))
    ):
        raise ValueError("NNTP requires an explicit DNS hostname without credentials or a port")
    if not isinstance(group, str) or len(group) > 200 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", group) is None:
        raise ValueError("NNTP requires one explicit newsgroup, not a wildcard")
    return f"nntps://{host.lower()}/{group}"


def inspect_nntp_window(
    ledger: Ledger, message_id: str, *, host: str = DEFAULT_HOST, group: str = DEFAULT_GROUP,
) -> NNTPWindowInspection:
    source = nntp_source(host, group)
    checkpoint = ledger.nntp_checkpoint(source)
    if checkpoint is None:
        raise ValueError("Run discover-nntp before inspecting the bootstrap window")
    if not ledger.nntp_message_received(source, message_id):
        raise ValueError("Fetch this message from the selected NNTP group before inspecting it")
    (message,) = ledger.messages_by_id((message_id,))
    estimate = message.time_estimate
    position = "unknown"
    if estimate is not None:
        timestamp = datetime.fromisoformat(estimate.timestamp)
        cutoff = datetime.fromisoformat(checkpoint.cutoff)
        bootstrap = datetime.fromisoformat(checkpoint.bootstrap_at)
        position = "before" if timestamp < cutoff else "after" if timestamp > bootstrap else "inside"
    return NNTPWindowInspection(source, message.message_id, checkpoint.cutoff, checkpoint.bootstrap_at, estimate, position)


def _group_snapshot(client, group: str) -> tuple[int, int, int, int]:
    before = client.group_validity(group)
    response, _, low, high, name = client.group(group)
    after = client.group_validity(group)
    try:
        low, high = int(low), int(high)
    except (TypeError, ValueError) as error:
        raise NNTPSourceError("NNTP group returned invalid article bounds") from error
    if not response.startswith("211 ") or name != group or not 0 <= low <= high + 1 or not 0 <= high < 9223372036854775806:
        raise NNTPSourceError("NNTP group returned invalid article bounds")
    return before, low, high, after


def _group_cursor(ledger: Ledger, source: str, snapshot: tuple[int, int, int, int], *, initialize: bool) -> NNTPNumberCursor:
    before, low, high, after = snapshot
    cursor = ledger.nntp_number_cursor(source)

    def halt(kind: str, expected: int, observed: int) -> None:
        ledger.record_nntp_incident(source, kind, expected, observed)
        raise ArchiveSafetyBlocked(f"NNTP {kind} for {source}; inspect source history")

    if before != after:
        halt("validity_changed", before, after)
    if cursor is None:
        if not initialize:
            raise ValueError("Run discover-nntp to establish a numeric baseline first")
        cursor = ledger.begin_nntp_number_cursor(source, before, high)
    if cursor.uid_validity != before:
        halt("validity_changed", cursor.uid_validity, before)
    if high < cursor.next_article - 1:
        halt("high_decreased", cursor.next_article - 1, high)
    if cursor.next_article <= high and low > cursor.next_article:
        halt("low_increased", cursor.next_article, low)
    return cursor


def discover_nntp(
    ledger: Ledger, *, host: str = DEFAULT_HOST, group: str = DEFAULT_GROUP,
    hours: int = 148, response_bytes: int = MAX_DISCOVERY_BYTES,
    client_factory: Callable = _client,
) -> NNTPDiscovery:
    source = nntp_source(host, group)
    if not isinstance(response_bytes, int) or isinstance(response_bytes, bool) or not 1024 <= response_bytes <= MAX_DISCOVERY_BYTES:
        raise ValueError("Discovery byte budget must be between 1024 and 8 * 1024 * 1024")
    with ledger.worker_lock():
        ledger.assert_unconflicted(())
        if ledger.status().conflicted_message_ids:
            raise NNTPSourceError("Message-ID conflicts require inspection before NNTP discovery resumes")
        try:
            with client_factory(host.lower()) as client:
                if "NEWNEWS" not in client.getcapabilities():
                    raise NNTPSourceError("The NNTP server does not advertise NEWNEWS")
                snapshot = _group_snapshot(client, group)
                response, server_date = client.date()
                if not response.startswith("111 ") or not isinstance(server_date, datetime) or server_date.tzinfo is not None:
                    raise NNTPSourceError("NNTP DATE did not provide the expected UTC server time")
                now = server_date.replace(tzinfo=timezone.utc)
                checkpoint = ledger.begin_nntp_bootstrap(source, now, hours=hours)
                numeric_cursor = _group_cursor(ledger, source, snapshot, initialize=True)
                since = max(
                    datetime.fromisoformat(checkpoint.cutoff),
                    datetime.fromisoformat(checkpoint.checked_through) - timedelta(seconds=60),
                )
                with _BoundedBuffer(response_bytes) as output:
                    response, _ = client.newnews(group, since.replace(tzinfo=None), file=output)
                    if not response.startswith("230 "):
                        raise NNTPSourceError("NNTP NEWNEWS returned an unexpected response")
                    data = output.getvalue()
                try:
                    identifiers = tuple(data.decode("ascii").splitlines())
                except UnicodeDecodeError as error:
                    raise NNTPSourceError("NNTP discovery returned non-ASCII Message-IDs") from error
                queued = ledger.record_nntp_discovery(source, checkpoint.checked_through, now, identifiers)
                updated = ledger.nntp_checkpoint(source)
                return NNTPDiscovery(updated, len(identifiers), queued, since.isoformat(), len(data), numeric_cursor)
        except NNTPSourceError:
            raise
        except (OSError, EOFError) as error:
            raise NNTPSourceError(f"NNTP discovery failed: {type(error).__name__}; pending work retained") from None


def discover_nntp_numbers(
    ledger: Ledger, *, host: str = DEFAULT_HOST, group: str = DEFAULT_GROUP,
    limit: int = 128, response_bytes: int = MAX_DISCOVERY_BYTES, client_factory: Callable = _client,
) -> NNTPNumberDiscovery:
    source = nntp_source(host, group)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("NNTP article range limit must be between 1 and 1000")
    if not isinstance(response_bytes, int) or isinstance(response_bytes, bool) or not 1024 <= response_bytes <= MAX_DISCOVERY_BYTES:
        raise ValueError("Discovery byte budget must be between 1024 and 8 * 1024 * 1024")
    with ledger.worker_lock():
        ledger.assert_unconflicted(())
        if ledger.status().conflicted_message_ids:
            raise NNTPSourceError("Message-ID conflicts require inspection before NNTP discovery resumes")
        if ledger.nntp_checkpoint(source) is None:
            raise ValueError("Run discover-nntp before scanning article numbers")
        try:
            with client_factory(host.lower()) as client:
                snapshot = _group_snapshot(client, group)
                cursor = _group_cursor(ledger, source, snapshot, initialize=False)
                _, low, high, _ = snapshot
                if cursor.next_article > high:
                    return NNTPNumberDiscovery(cursor, low, high, 0, 0, 0, False)
                through = min(high, cursor.next_article + limit - 1)
                with _BoundedBuffer(response_bytes) as output:
                    response, _ = client.xhdr("Message-ID", f"{cursor.next_article}-{through}", file=output)
                    data = output.getvalue()
                if not response.startswith("221 "):
                    raise NNTPSourceError("NNTP XHDR returned an unexpected response")
                try:
                    lines = data.decode("ascii").splitlines()
                except UnicodeDecodeError as error:
                    raise NNTPSourceError("NNTP article range returned non-ASCII data") from error
                articles = []
                for line in lines:
                    match = re.fullmatch(r"([0-9]+) (<[^<>\s]+@[^<>\s]+>)", line)
                    if match is None:
                        raise NNTPSourceError("NNTP article range returned an invalid Message-ID")
                    articles.append((int(match.group(1)), match.group(2)))
                expected_numbers = tuple(range(cursor.next_article, through + 1))
                actual_numbers = tuple(number for number, _ in articles)
                if actual_numbers != expected_numbers:
                    expected = next((number for index, number in enumerate(expected_numbers) if index >= len(actual_numbers) or actual_numbers[index] != number), cursor.next_article)
                    observed = next((number for number in actual_numbers if number >= expected), 0)
                    ledger.record_nntp_incident(source, "range_gap", expected, observed)
                    raise ArchiveSafetyBlocked(f"NNTP article range has a gap at {expected}; inspect source history")
                queued = ledger.record_nntp_number_batch(source, cursor.uid_validity, cursor.next_article, through, tuple(articles))
                updated = ledger.nntp_number_cursor(source)
                return NNTPNumberDiscovery(updated, low, high, len(articles), queued, len(data), updated.next_article <= high)
        except NNTPSourceError:
            raise
        except (OSError, EOFError) as error:
            raise NNTPSourceError(f"NNTP numeric discovery failed: {type(error).__name__}; cursor retained") from None


def fetch_nntp(
    ledger: Ledger, *, host: str = DEFAULT_HOST, group: str = DEFAULT_GROUP,
    limit: int = 32, article_bytes: int = MAX_ARTICLE_BYTES,
    batch_bytes: int = MAX_BATCH_BYTES, min_free_bytes: int = MIN_FREE_BYTES,
    message_id: str | None = None, after: int = 0, timeout_retries: int = 0, client_factory: Callable = _client,
) -> NNTPFetch:
    source = nntp_source(host, group)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("NNTP batch limit must be between 1 and 1000")
    if not isinstance(timeout_retries, int) or isinstance(timeout_retries, bool) or not 0 <= timeout_retries <= 3:
        raise ValueError("NNTP timeout retries must be between 0 and 3")
    if not isinstance(after, int) or isinstance(after, bool) or not 0 <= after <= 9223372036854775807:
        raise ValueError("NNTP queue cursor is invalid")
    if message_id is not None and after:
        raise ValueError("Choose either a pending Message-ID or a queue cursor")
    for value, minimum, maximum, name in (
        (article_bytes, 1024, MAX_ARTICLE_BYTES, "Article byte budget"),
        (batch_bytes, article_bytes, MAX_BATCH_BYTES, "Batch byte budget"),
        (min_free_bytes, MIN_FREE_BYTES, 1024 * 1024 * 1024 * 1024, "Free-space reserve"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
    with ledger.worker_lock():
        ledger.assert_unconflicted(())
        if ledger.status().conflicted_message_ids:
            raise NNTPSourceError("Message-ID conflicts require inspection before NNTP ingestion resumes")
        if ledger.nntp_checkpoint(source) is None:
            raise ValueError("Run discover-nntp before fetching queued articles")
        pending = (
            (ledger.pending_nntp_message(source, message_id),) if message_id is not None
            else ledger.pending_nntp_messages(source, limit=limit, after=after)
        )
        ingested = replayed = received = dependencies = retries_used = 0
        if pending:
            try:
                with ExitStack() as connections:
                    client = connections.enter_context(client_factory(host.lower()))
                    _group_cursor(ledger, source, _group_snapshot(client, group), initialize=False)
                    for message_id in pending:
                        if batch_bytes - received < article_bytes:
                            break
                        while True:
                            if shutil.disk_usage(ledger.path.parent).free < min_free_bytes + 3 * article_bytes:
                                raise NNTPSourceError("Insufficient free space for NNTP ingestion and its SQLite reserve")
                            with _BoundedBuffer(article_bytes) as output:
                                try:
                                    response, article = client.article(f"<{message_id}>", file=output)
                                except (ValueError, IndexError):
                                    raise NNTPSourceError("NNTP ARTICLE returned malformed data; item retained") from None
                                except TimeoutError:
                                    received += output.tell()
                                    if retries_used >= timeout_retries:
                                        raise
                                    if batch_bytes - received < article_bytes:
                                        raise NNTPSourceError("NNTP retry exceeds the remaining batch byte budget; current item retained")
                                    retries_used += 1
                                    connections.close()
                                    client = connections.enter_context(client_factory(host.lower()))
                                    _group_cursor(ledger, source, _group_snapshot(client, group), initialize=False)
                                    continue
                                data = output.getvalue()
                            received += len(data)
                            break
                        parsed = parse_message(data)
                        headers = BytesHeaderParser(policy=policy.default).parsebytes(data)
                        newsgroups = headers.get_all("Newsgroups", [])
                        if (
                            not response.startswith("220 ")
                            or article.message_id != f"<{message_id}>"
                            or parsed.message_id != message_id
                            or len(newsgroups) != 1
                            or group not in {name.strip() for name in str(newsgroups[0]).split(",")}
                        ):
                            raise NNTPSourceError("NNTP ARTICLE did not match its requested Message-ID and group; item retained")
                        if not isinstance(article.number, int) or isinstance(article.number, bool) or article.number < 1:
                            raise NNTPSourceError("NNTP ARTICLE did not provide a number in the selected group; item retained")
                        try:
                            stat_response, stat_number, stat_message_id = client.stat(article.number)
                        except (ValueError, IndexError):
                            raise NNTPSourceError("NNTP numbered STAT returned malformed data; item retained") from None
                        if (
                            not stat_response.startswith("223 ")
                            or not isinstance(stat_number, int) or isinstance(stat_number, bool)
                            or stat_number != article.number or stat_message_id != f"<{message_id}>"
                        ):
                            raise NNTPSourceError("NNTP numbered STAT did not match the selected group article; item retained")
                        stored = ledger.record_parsed_message(parsed, provenance={
                            "transport": "nntp", "source": source, "article_number": article.number,
                            "lore_received_at": None, "receipt_time_status": "unverified",
                        })
                        dependencies += ledger.mark_nntp_received(source, message_id)
                        ingested += int(stored)
                        replayed += int(not stored)
            except NNTPSourceError:
                raise
            except (OSError, EOFError) as error:
                raise NNTPSourceError(f"NNTP article fetch failed: {type(error).__name__}; current item retained") from None
        return NNTPFetch(source, ingested, replayed, received, dependencies, bool(ledger.pending_nntp_messages(source, limit=1)), timeout_retries=retries_used)


def discover_nntp_thread(
    ledger: Ledger, message_id: str, *, inbox_url: str = DEFAULT_INBOX,
    host: str = DEFAULT_HOST, group: str = DEFAULT_GROUP,
    compressed_limit: int = 2 * 1024 * 1024, uncompressed_limit: int = 8 * 1024 * 1024,
    from_index_time: int | None = None, through_index_time: int | None = None,
) -> NNTPThreadDiscovery:
    source = nntp_source(host, group)
    for value, maximum in ((compressed_limit, MAX_THREAD_COMPRESSED), (uncompressed_limit, MAX_THREAD_UNCOMPRESSED)):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError("Thread byte limits must be positive and within their configured maxima")
    with ledger.worker_lock():
        ledger.assert_unconflicted(())
        if ledger.status().conflicted_message_ids:
            raise NNTPSourceError("Message-ID conflicts require inspection before thread discovery resumes")
        if not ledger.nntp_message_received(source, message_id):
            raise ValueError("Fetch the thread anchor from this NNTP group before discovering its thread")
        if shutil.disk_usage(ledger.path.parent).free < MIN_FREE_BYTES + 3 * uncompressed_limit:
            raise NNTPSourceError("Insufficient free space for thread discovery and its SQLite reserve")
        thread = fetch_public_inbox_thread(
            inbox_url, message_id, compressed_limit=compressed_limit, uncompressed_limit=uncompressed_limit,
            temporary_directory=ledger.path.parent, from_index_time=from_index_time, through_index_time=through_index_time,
        )
        ranged = from_index_time is not None or through_index_time is not None
        search_range = (inbox_url, from_index_time, through_index_time, thread.empty_or_unavailable) if ranged else None
        queued = ledger.queue_nntp_thread_messages(source, message_id, thread.message_ids, search_range=search_range)
        return NNTPThreadDiscovery(
            source, len(thread.message_ids), queued, thread.compressed_bytes, thread.uncompressed_bytes,
            from_index_time=from_index_time, through_index_time=through_index_time,
            empty_or_unavailable=thread.empty_or_unavailable,
        )
