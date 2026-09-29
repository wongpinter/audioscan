"""HTTP range fetching, exercised against a real local HTTP server."""

from __future__ import annotations

import io
import random
from collections.abc import Iterator

import httpx
import pytest

from audioscan.reader import FetchError, RangeNotSupported, ReadBudgetExceeded, SeekableBlockReader
from audioscan.sources.http import (
    HttpRangeFetcher,
    HttpSource,
    _retry_after_seconds,
    filename_from_url,
    parse_content_range,
)
from http_server import ServerState, running_server

PAYLOAD = bytes((index * 13 + 7) % 256 for index in range(50_000))


@pytest.fixture
def http_server() -> Iterator[tuple[str, ServerState]]:
    with running_server(PAYLOAD) as pair:
        yield pair


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes 0-1023/4096", (0, 1023, 4096)),
        ("bytes 100-199/*", (100, 199, None)),
        ("bytes=0-0/12345", (0, 0, 12345)),
        (None, (None, None, None)),
        ("garbage", (None, None, None)),
    ],
)
def test_parse_content_range(header: str | None, expected: tuple[int, int, int]) -> None:
    assert parse_content_range(header) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://host.example/audio/book.m4b", "book.m4b"),
        ("https://host.example/audio/book%20one.m4b?token=x", "book one.m4b"),
        ("https://host.example/", "https://host.example/"),
    ],
)
def test_filename_from_url(url: str, expected: str) -> None:
    assert filename_from_url(url) == expected


# --------------------------------------------------------------------------- #
# size discovery
# --------------------------------------------------------------------------- #
def test_size_detected_via_head(http_server: tuple[str, ServerState]) -> None:
    url, state = http_server
    fetcher = HttpRangeFetcher.open(url)
    try:
        assert fetcher.size == len(PAYLOAD)
        assert state.methods[0] == "HEAD"
    finally:
        fetcher.close()


def test_size_detected_without_head_support(http_server: tuple[str, ServerState]) -> None:
    url, state = http_server
    state.head_allowed = False
    fetcher = HttpRangeFetcher.open(url)
    try:
        assert fetcher.size == len(PAYLOAD)
        assert "HEAD" in state.methods
        assert ("GET", "bytes=0-0") in state.requests
    finally:
        fetcher.close()


def test_known_size_skips_discovery_requests(http_server: tuple[str, ServerState]) -> None:
    url, state = http_server
    fetcher = HttpRangeFetcher.open(url, size=len(PAYLOAD))
    try:
        assert fetcher.size == len(PAYLOAD)
        assert state.requests == []
    finally:
        fetcher.close()


# --------------------------------------------------------------------------- #
# ranged reads
# --------------------------------------------------------------------------- #
def test_fetch_returns_exact_requested_bytes(http_server: tuple[str, ServerState]) -> None:
    url, _state = http_server
    fetcher = HttpRangeFetcher.open(url)
    try:
        assert fetcher.fetch(1000, 500) == PAYLOAD[1000:1500]
        assert fetcher.fetch(0, 10) == PAYLOAD[:10]
        assert fetcher.stats.bytes_fetched == 510
        assert fetcher.stats.requests == 2
    finally:
        fetcher.close()


def test_partial_content_response_is_bounded_and_validated() -> None:
    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 10-19/50000"},
            content=b"x" * 50000,
        )

    client = httpx.Client(transport=httpx.MockTransport(oversized))
    fetcher = HttpRangeFetcher.open("https://example.org/audio.mp3", size=50000, client=client)
    with pytest.raises(FetchError, match="invalid byte range"):
        fetcher.fetch(10, 10)
    fetcher.close()
    client.close()


def test_partial_content_requires_matching_content_range() -> None:
    def mismatched(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 0-9/50000"},
            content=b"x" * 10,
        )

    client = httpx.Client(transport=httpx.MockTransport(mismatched))
    fetcher = HttpRangeFetcher.open("https://example.org/audio.mp3", size=50000, client=client)
    with pytest.raises(FetchError, match="invalid byte range"):
        fetcher.fetch(10, 10)
    fetcher.close()
    client.close()


def test_fetch_past_eof_is_clamped(http_server: tuple[str, ServerState]) -> None:
    url, _state = http_server
    fetcher = HttpRangeFetcher.open(url)
    try:
        assert fetcher.fetch(len(PAYLOAD) - 4, 100) == PAYLOAD[-4:]
        assert fetcher.fetch(len(PAYLOAD) + 10, 100) == b""
    finally:
        fetcher.close()


def test_seekable_reader_over_http_matches_payload(http_server: tuple[str, ServerState]) -> None:
    url, _state = http_server
    reader = SeekableBlockReader(HttpRangeFetcher.open(url), block_size=4096)
    reference = io.BytesIO(PAYLOAD)
    rng = random.Random(3)
    try:
        for _ in range(80):
            offset = rng.randint(0, len(PAYLOAD) - 200)
            length = rng.choice([1, 16, 300, 4096])
            reader.seek(offset)
            reference.seek(offset)
            assert reader.read(length) == reference.read(length)
        reader.seek(0)
        assert reader.read() == PAYLOAD
        assert reader.stats.bytes_fetched >= len(PAYLOAD)
    finally:
        reader.close()


def test_reader_over_http_downloads_far_less_than_the_whole_file(
    http_server: tuple[str, ServerState],
) -> None:
    """Read a small header only — the rest of the file must not be fetched."""
    url, _state = http_server
    reader = SeekableBlockReader(HttpRangeFetcher.open(url), block_size=2048, max_blocks=4)
    try:
        reader.seek(0)
        header = reader.read(64)
        assert header == PAYLOAD[:64]
        reader.seek(len(PAYLOAD) - 64)
        assert reader.read(64) == PAYLOAD[-64:]
        assert reader.stats.bytes_fetched <= 2048 * 2
    finally:
        reader.close()


def test_reader_budget_applies_over_http(http_server: tuple[str, ServerState]) -> None:
    url, _state = http_server
    reader = SeekableBlockReader(HttpRangeFetcher.open(url), block_size=4096, budget_bytes=4096)
    try:
        reader.read(4096)
        with pytest.raises(ReadBudgetExceeded):
            reader.read(len(PAYLOAD))
    finally:
        reader.close()


# --------------------------------------------------------------------------- #
# misbehaving servers
# --------------------------------------------------------------------------- #
def test_server_ignoring_range_raises_when_seeking(http_server: tuple[str, ServerState]) -> None:
    url, state = http_server
    state.range_allowed = False
    fetcher = HttpRangeFetcher.open(url)
    try:
        with pytest.raises(RangeNotSupported):
            fetcher.fetch(5000, 100)
    finally:
        fetcher.close()


def test_server_ignoring_range_still_serves_the_first_block(
    http_server: tuple[str, ServerState],
) -> None:
    url, state = http_server
    state.range_allowed = False
    fetcher = HttpRangeFetcher.open(url, size=len(PAYLOAD))
    try:
        assert fetcher.fetch(0, 32) == PAYLOAD[:32]
    finally:
        fetcher.close()


def test_range_beyond_eof_returns_empty(http_server: tuple[str, ServerState]) -> None:
    url, _state = http_server
    fetcher = HttpRangeFetcher.open(url)
    try:
        assert fetcher.fetch(len(PAYLOAD) - 1, 1) == PAYLOAD[-1:]
    finally:
        fetcher.close()


# --------------------------------------------------------------------------- #
# retries and failures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [("5", 5.0), ("999", 30.0), ("0", 0.0), ("nonsense", None), (None, None)],
)
def test_retry_after_seconds_is_parsed_and_bounded(
    value: str | None, expected: float | None
) -> None:
    assert _retry_after_seconds(value) == expected


def test_transient_fetch_errors_are_retried(
    http_server: tuple[str, ServerState],
) -> None:
    """A 503 once, then success: Retry-After: 0 keeps the test fast."""
    url, state = http_server
    state.fail_times = 1
    state.fail_status = 503
    state.retry_after = "0"

    fetcher = HttpRangeFetcher.open(url, size=len(PAYLOAD))
    try:
        assert fetcher.fetch(100, 10) == PAYLOAD[100:110]
        # Only the successful response counts towards the traffic statistics.
        assert fetcher.stats.requests == 1
        assert fetcher.stats.bytes_fetched == 10
        assert state.methods.count("GET") == 2
    finally:
        fetcher.close()


def test_rate_limited_requests_are_retried(
    http_server: tuple[str, ServerState],
) -> None:
    url, state = http_server
    state.fail_times = 1
    state.fail_status = 429
    state.retry_after = "0"

    fetcher = HttpRangeFetcher.open(url, size=len(PAYLOAD))
    try:
        assert fetcher.fetch(0, 4) == PAYLOAD[:4]
        assert state.methods.count("GET") == 2
    finally:
        fetcher.close()


def test_exhausted_retries_raise_fetch_error(
    http_server: tuple[str, ServerState],
) -> None:
    url, state = http_server
    state.fail_times = 99
    state.fail_status = 500
    state.retry_after = "0"

    fetcher = HttpRangeFetcher.open(url, size=len(PAYLOAD), max_retries=1)
    try:
        with pytest.raises(FetchError):
            fetcher.fetch(0, 10)
    finally:
        fetcher.close()
    assert state.methods.count("GET") == 2  # initial attempt plus one retry


def test_client_errors_are_not_retried(
    http_server: tuple[str, ServerState],
) -> None:
    url, state = http_server
    state.fail_times = 99
    state.fail_status = 403

    fetcher = HttpRangeFetcher.open(url, size=len(PAYLOAD), max_retries=3)
    try:
        with pytest.raises(FetchError):
            fetcher.fetch(0, 10)
    finally:
        fetcher.close()
    assert state.methods.count("GET") == 1, "403 without credentials is not transient"


# --------------------------------------------------------------------------- #
# HttpSource
# --------------------------------------------------------------------------- #
def test_http_source_lists_one_file_and_opens_a_reader(
    http_server: tuple[str, ServerState],
) -> None:
    url, _state = http_server
    source = HttpSource(url, block_size=4096)
    try:
        items = list(source.iter_files())
        assert len(items) == 1
        assert items[0].name == "book.m4b"
        assert items[0].id == url

        reader = source.open(items[0])
        try:
            assert reader.size == len(PAYLOAD)
            assert reader.read(32) == PAYLOAD[:32]
        finally:
            reader.close()
    finally:
        source.close()
