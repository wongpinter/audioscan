"""End-to-end: a large remote audiobook is probed without downloading it.

This is the claim the package exists for. The file below is laid out the way real
audiobooks often are — metadata in a ``moov`` box *after* an ``mdat`` of audio
data — so a "download the first 2 MB" shortcut returns nothing, while the ranged
reader costs a few percent of the file.
"""

from __future__ import annotations

import json

import pytest

from audioscan.cli import main
from audioscan.probe import probe
from audioscan.reader import DEFAULT_BLOCK_SIZE
from audioscan.sources.http import HttpSource
from fixtures import build_m4b
from http_server import running_server

MDAT_SIZE = 40_000_000
#: The head-only shortcut this package replaces would read this much.
NAIVE_HEAD_BYTES = 256 * 1024
#: Cost of the sparse access pattern: roughly one head block plus one tail block.
FIXED_OVERHEAD_BYTES = 512 * 1024


@pytest.fixture(scope="module")
def remote_book() -> bytes:
    return build_m4b(
        title="The Dark Lord Ascending",
        artist="Stephen Fry",
        album="Deathly Hallows",
        albumartist="J.K. Rowling",
        chapters=[(0.0, "Chapter 1"), (1800.0, "Chapter 2"), (5400.5, "Chapter 3")],
        duration=7200.0,
        mdat_size=MDAT_SIZE,
        moov_at_end=True,
    )


def test_head_only_reading_would_miss_the_metadata(remote_book: bytes) -> None:
    """Document the failure mode: none of the metadata is in the first bytes."""
    head = remote_book[:NAIVE_HEAD_BYTES]
    assert remote_book.index(b"mdat") < remote_book.index(b"moov")
    assert b"moov" not in head
    assert b"chpl" not in head
    assert b"\xa9nam" not in head


def test_tail_moov_is_readable_through_ranged_requests(remote_book: bytes) -> None:
    with running_server(remote_book) as (url, state):
        source = HttpSource(url, block_size=64 * 1024)
        try:
            item = next(iter(source.iter_files()))
            reader = source.open(item)
            try:
                meta = probe(reader, name=item.name, source="http", size=reader.size)
                fetched = reader.stats.bytes_fetched
                requests = reader.stats.requests
            finally:
                reader.close()
        finally:
            source.close()

    assert meta.is_ok, meta.error
    assert meta.title == "The Dark Lord Ascending"
    assert meta.artist == "Stephen Fry"
    assert meta.duration == pytest.approx(7200.0)
    assert [c.title for c in meta.chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert meta.format == "MP4"

    # Server-side accounting, independent of the client's own counters.
    assert state.bytes_served == fetched
    assert fetched < FIXED_OVERHEAD_BYTES, f"fetched {fetched} bytes"
    assert fetched < len(remote_book) * 0.02, f"fetched {fetched} of {len(remote_book)} bytes"
    assert requests <= 8


def test_cli_scan_over_http_reports_minimal_traffic(
    capsys: pytest.CaptureFixture[str], remote_book: bytes
) -> None:
    with running_server(remote_book) as (url, _state):
        code = main(["scan", url, "--json", "-"])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["totals"]["tracks"] == 1
    assert payload["totals"]["bytes"] == len(remote_book)

    track = payload["tracks"][0]
    assert track["title"] == "The Dark Lord Ascending"
    assert track["chapters"][2]["title"] == "Chapter 3"
    assert track["source"] == "http"
    assert track["fetched_bytes"] < FIXED_OVERHEAD_BYTES
    assert track["fetched_bytes"] < len(remote_book) * 0.02


def test_scan_reports_an_error_when_the_server_cannot_seek(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A server that ignores Range cannot be probed; report it, do not crash.

    The file must be larger than one cache block, otherwise the whole thing
    arrives in the first response and no seek is ever needed.
    """
    multi_block = build_m4b(title="No range", duration=60.0, mdat_size=1_500_000)
    assert len(multi_block) > DEFAULT_BLOCK_SIZE

    with running_server(multi_block, range_allowed=False) as (url, _state):
        code = main(["scan", url, "--json", "-"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["totals"]["errors"] == 1
    assert payload["tracks"][0]["error"]


def test_file_smaller_than_one_block_needs_no_ranges(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A file that fits in a single block works even without Range support."""
    tiny = build_m4b(title="Tiny", duration=30.0, mdat_size=100_000)
    assert len(tiny) < DEFAULT_BLOCK_SIZE

    with running_server(tiny, range_allowed=False) as (url, state):
        code = main(["scan", url, "--json", "-"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["tracks"][0]["title"] == "Tiny"
    assert state.methods.count("GET") == 1, "one plain GET should have sufficed"
