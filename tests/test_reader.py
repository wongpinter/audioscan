"""Tests for the seekable block reader."""

from __future__ import annotations

import io
import random

import pytest

from audioscan.reader import (
    BytesFetcher,
    ReadBudgetExceeded,
    SeekableBlockReader,
    open_block_reader,
)

SIZE = 200_000
BLOB = bytes((index * 37 + (index >> 8)) % 256 for index in range(SIZE))


def make_reader(**kwargs: object) -> tuple[SeekableBlockReader, BytesFetcher]:
    fetcher = BytesFetcher(BLOB, name="blob")
    reader = SeekableBlockReader(fetcher, **kwargs)  # type: ignore[arg-type]
    return reader, fetcher


def test_reads_and_seeks_match_bytesio() -> None:
    """Random read/seek sequences must return exactly what BytesIO would."""
    reference = io.BytesIO(BLOB)
    reader, _ = make_reader(block_size=4096, max_blocks=4)
    rng = random.Random(7)

    for _ in range(400):
        action = rng.random()
        if action < 0.45:
            count = rng.choice([1, 3, 17, 100, 4096, 9000, 70_000, -1])
            expected = reference.read(count)
            actual = reader.read(count)
            assert actual == expected, f"read({count}) at {reference.tell()}"
        else:
            whence = rng.choice([io.SEEK_SET, io.SEEK_CUR, io.SEEK_END])
            if whence == io.SEEK_SET:
                offset = rng.randint(0, SIZE + 5000)
            elif whence == io.SEEK_CUR:
                offset = rng.randint(-1000, 1000)
            else:
                offset = rng.randint(-SIZE, 1000)
            try:
                expected_pos = reference.seek(offset, whence)
            except ValueError:
                with pytest.raises(ValueError):
                    reader.seek(offset, whence)
                continue
            assert reader.seek(offset, whence) == expected_pos
        assert reader.tell() == reference.tell()

    assert reader.read() == reference.read()


def test_read_with_negative_size_returns_everything() -> None:
    reader, _ = make_reader(block_size=8192)
    reader.seek(1000)
    assert reader.read(-1) == BLOB[1000:]
    assert reader.tell() == SIZE


def test_read_at_and_past_eof_returns_empty() -> None:
    reader, _ = make_reader(block_size=8192)
    reader.seek(SIZE)
    assert reader.read(10) == b""
    reader.seek(SIZE + 500)
    assert reader.read(10) == b""
    assert reader.tell() == SIZE + 500


def test_seek_end_and_negative_position() -> None:
    reader, _ = make_reader(block_size=8192)
    assert reader.seek(-10, io.SEEK_END) == SIZE - 10
    assert reader.read(4) == BLOB[SIZE - 10 : SIZE - 6]
    with pytest.raises(ValueError):
        reader.seek(-1, io.SEEK_SET)
    with pytest.raises(ValueError):
        reader.seek(-(SIZE + 1), io.SEEK_END)


def test_invalid_whence_and_block_size() -> None:
    with pytest.raises(ValueError):
        make_reader(block_size=0)
    with pytest.raises(ValueError):
        make_reader(max_blocks=0)
    reader, _ = make_reader(block_size=1024)
    with pytest.raises(ValueError):
        reader.seek(0, whence=99)


def test_readinto_and_io_flags() -> None:
    reader, _ = make_reader(block_size=1024)
    buffer = bytearray(32)
    assert reader.readinto(buffer) == 32
    assert bytes(buffer) == BLOB[:32]
    assert reader.readable() and reader.seekable() and not reader.writable()

    reader.seek(SIZE - 4)
    tiny = bytearray(16)
    assert reader.readinto(tiny) == 4
    assert bytes(tiny[:4]) == BLOB[-4:]


def test_scattered_small_reads_share_one_block() -> None:
    """This is the whole point: many tiny seeks, one range request."""
    reader, fetcher = make_reader(block_size=64 * 1024, max_blocks=8)
    reader.seek(10)
    reader.read(16)
    reader.seek(50_000)
    reader.read(16)
    reader.seek(63_000)
    reader.read(16)
    assert fetcher.fetched_ranges == [(0, 64 * 1024)]
    assert reader.stats.cache_misses == 1
    assert reader.stats.cache_hits == 2


def test_large_contiguous_read_uses_one_request() -> None:
    reader, fetcher = make_reader(block_size=4096, max_blocks=8)
    data = reader.read(64 * 1024)
    assert data == BLOB[: 64 * 1024]
    assert len(fetcher.fetched_ranges) == 1
    assert fetcher.fetched_ranges[0] == (0, 64 * 1024)


def test_cached_large_read_falls_back_to_blocks() -> None:
    reader, fetcher = make_reader(block_size=64 * 1024, max_blocks=8)
    reader.read(64 * 1024)  # one block, now cached
    reader.seek(0)
    reader.read(64 * 1024)
    assert len(fetcher.fetched_ranges) == 1, "cached data must not be refetched"


def test_cache_eviction_is_bounded() -> None:
    reader, _ = make_reader(block_size=1024, max_blocks=3)
    for index in range(10):
        reader.seek(index * 1024)
        reader.read(8)
    assert reader.cache_blocks == 3


def test_budget_stops_runaway_reads() -> None:
    reader, _ = make_reader(block_size=4096, max_blocks=64, budget_bytes=8192)
    assert reader.read(4096)
    with pytest.raises(ReadBudgetExceeded):
        reader.read(64 * 1024)


def test_budget_allows_unlimited_when_none() -> None:
    reader, _ = make_reader(block_size=4096, budget_bytes=None)
    assert reader.read(SIZE) == BLOB


def test_stats_snapshot_reports_traffic() -> None:
    reader, _ = make_reader(block_size=1024, max_blocks=2)
    reader.read(16)
    reader.seek(0)
    reader.read(16)
    snapshot = reader.stats.snapshot()
    assert snapshot["requests"] == 1
    assert snapshot["bytes_fetched"] == 1024
    assert snapshot["cache_hits"] == 1


def test_open_block_reader_helper() -> None:
    reader = open_block_reader(BLOB[:100], block_size=32)
    assert reader.read() == BLOB[:100]
    reader.close()
    assert reader.closed


def test_close_releases_blocks_and_blocks_further_reads() -> None:
    reader, fetcher = make_reader(block_size=1024)
    reader.read(16)
    assert reader.cache_blocks == 1
    reader.close()
    assert reader.cache_blocks == 0
    with pytest.raises(ValueError):
        reader.read(1)


def test_reading_only_the_tail_does_not_touch_the_head() -> None:
    """Mirrors an M4B whose moov box sits at the end of a large file."""
    reader, fetcher = make_reader(block_size=64 * 1024, max_blocks=8)
    tail_start = SIZE - 1000
    reader.seek(tail_start)
    assert reader.read(1000) == BLOB[tail_start:]
    assert len(fetcher.fetched_ranges) == 1
    start, length = fetcher.fetched_ranges[0]
    assert start <= tail_start < start + length
    assert reader.stats.bytes_fetched < SIZE / 3
