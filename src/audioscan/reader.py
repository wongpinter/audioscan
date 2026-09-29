"""A seekable file-like object backed by ranged reads.

``mutagen`` needs random access: it jumps to offsets, reads a handful of bytes,
jumps again. Over HTTP every one of those jumps would be a request. This module
wraps a *block cache* around a byte-range ``Fetcher`` so those jumps are served
from a few aligned chunks.

It also matters for correctness, not just speed. Plenty of M4B files keep their
``moov`` atom (which holds duration and tags) at the *end* of the file, so a
"just read the first 2 MB" strategy silently returns empty metadata. A seekable
view handles either layout.
"""

from __future__ import annotations

import io
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

DEFAULT_BLOCK_SIZE = 256 * 1024
DEFAULT_MAX_BLOCKS = 64
#: Reads at least this large bypass the block cache and go out as one request.
DIRECT_READ_MULTIPLIER = 4


class FetchError(RuntimeError):
    """Raised when the underlying storage could not serve a requested range."""


class RangeNotSupported(FetchError):
    """Raised when the storage ignores range requests and cannot be seeked."""


class ReadBudgetExceeded(FetchError):
    """Raised when a reader exceeds its configured transfer budget."""


@dataclass
class FetchStats:
    """Counters describing how much traffic a scan actually cost."""

    requests: int = 0
    bytes_fetched: int = 0
    cache_hits: int = 0
    cache_misses: int = 0

    def add_fetch(self, nbytes: int) -> None:
        self.requests += 1
        self.bytes_fetched += nbytes

    def merge(self, other: FetchStats) -> FetchStats:
        """Return a new ``FetchStats`` summing ``self`` and ``other``."""
        return FetchStats(
            requests=self.requests + other.requests,
            bytes_fetched=self.bytes_fetched + other.bytes_fetched,
            cache_hits=self.cache_hits + other.cache_hits,
            cache_misses=self.cache_misses + other.cache_misses,
        )

    def snapshot(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "bytes_fetched": self.bytes_fetched,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
        }


@runtime_checkable
class Fetcher(Protocol):
    """A random-access byte source.

    Implementations must be able to report the total size up front and return
    the requested range (short reads only at EOF).
    """

    size: int
    stats: FetchStats

    def fetch(self, start: int, length: int) -> bytes:
        """Return up to ``length`` bytes starting at ``start``."""

    def close(self) -> None:
        """Release any underlying resources."""


class SeekableBlockReader(io.RawIOBase):
    """Serve ``read``/``seek`` from an aligned block cache over a ``Fetcher``.

    Parameters
    ----------
    fetcher:
        Backing byte source.
    block_size:
        Alignment and granularity of cached chunks. Larger blocks mean fewer
        requests but more wasted bytes when a parser reads only a small header.
    max_blocks:
        Cache capacity; the least recently used block is evicted first.
    budget_bytes:
        Optional cap on total bytes pulled through this reader. Exceeding it
        raises :class:`ReadBudgetExceeded`, which is the safety net that stops a
        malformed file from turning a metadata scan into a full download.
    """

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        block_size: int = DEFAULT_BLOCK_SIZE,
        max_blocks: int = DEFAULT_MAX_BLOCKS,
        budget_bytes: int | None = None,
        name: str = "",
    ) -> None:
        if block_size <= 0:
            raise ValueError("block_size must be positive")
        if max_blocks <= 0:
            raise ValueError("max_blocks must be positive")
        super().__init__()
        self._fetcher = fetcher
        self._block_size = block_size
        self._max_blocks = max_blocks
        self._budget_bytes = budget_bytes
        self._name = name
        self._pos = 0
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        self._cache_stats = FetchStats()
        self._lock = threading.Lock()
        self._direct_threshold = block_size * DIRECT_READ_MULTIPLIER
        self._ready = True

    # -- introspection -------------------------------------------------------
    @property
    def name(self) -> str:
        return self._name or getattr(self._fetcher, "name", "")

    @property
    def size(self) -> int:
        """Total size of the underlying stream in bytes."""
        return self._fetcher.size

    @property
    def block_size(self) -> int:
        return self._block_size

    @property
    def stats(self) -> FetchStats:
        """Transfer counters for this reader (cache + fetcher)."""
        return self._cache_stats.merge(self._fetcher.stats)

    @property
    def cache_blocks(self) -> int:
        """Number of blocks currently held in memory."""
        return len(self._blocks)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if not getattr(self, "_ready", False):
            return "<SeekableBlockReader (not constructed)>"
        return (
            f"<SeekableBlockReader name={self.name!r} size={self.size} "
            f"pos={self._pos} blocks={len(self._blocks)}>"
        )

    # -- io.RawIOBase --------------------------------------------------------
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        self._checkClosed()
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the read cursor; seeking past EOF is allowed (reads return b"")."""
        self._checkClosed()
        if whence == io.SEEK_SET:
            new_pos = offset
        elif whence == io.SEEK_CUR:
            new_pos = self._pos + offset
        elif whence == io.SEEK_END:
            new_pos = self.size + offset
        else:
            raise ValueError(f"invalid whence: {whence!r}")
        if new_pos < 0:
            raise ValueError("negative seek position")
        self._pos = new_pos
        return self._pos

    def read(self, size: int = -1) -> bytes:
        """Read ``size`` bytes from the current position (all of them if -1)."""
        self._checkClosed()
        if size is None or size < 0:
            return self._read_to_eof()
        if size == 0:
            return b""
        if size >= self._direct_threshold and not self._covers_cached_block(self._pos, size):
            return self._read_direct(size)
        return self._read_blockwise(size)

    def readinto(self, b: Any) -> int:
        """Read into a preallocated buffer; required by the ``RawIOBase`` API."""
        self._checkClosed()
        data = self.read(len(b))
        count = len(data)
        b[:count] = data
        return count

    def close(self) -> None:
        """Drop cached blocks and close the fetcher."""
        if not getattr(self, "_ready", False):
            # Construction failed before the base class was initialised. There is
            # nothing to release, but io.IOBase.__del__ still calls close().
            return
        if self.closed:
            return
        self._blocks.clear()
        try:
            self._fetcher.close()
        finally:
            super().close()

    # -- internals -----------------------------------------------------------
    def _check_budget(self, nbytes: int) -> None:
        if self._budget_bytes is None:
            return
        already = self._fetcher.stats.bytes_fetched
        if already + nbytes > self._budget_bytes:
            raise ReadBudgetExceeded(
                f"{self.name or 'stream'}: read budget of {self._budget_bytes} bytes exceeded"
            )

    def _block_for(self, index: int) -> bytes:
        cached = self._blocks.get(index)
        if cached is not None:
            self._blocks.move_to_end(index)
            with self._lock:
                self._cache_stats.cache_hits += 1
            return cached

        start = index * self._block_size
        length = min(self._block_size, self.size - start)
        if length <= 0:
            return b""
        self._check_budget(length)
        with self._lock:
            self._cache_stats.cache_misses += 1
        data = self._fetcher.fetch(start, length)
        self._blocks[index] = data
        self._blocks.move_to_end(index)
        while len(self._blocks) > self._max_blocks:
            self._blocks.popitem(last=False)
        return data

    def _covers_cached_block(self, pos: int, size: int) -> bool:
        first = pos // self._block_size
        last = (pos + size - 1) // self._block_size
        return last > first and all(i in self._blocks for i in range(first, last + 1))

    def _read_blockwise(self, size: int) -> bytes:
        if self._pos >= self.size:
            return b""
        wanted = min(size, self.size - self._pos)
        chunks: list[bytes] = []
        collected = 0
        while collected < wanted:
            index = self._pos // self._block_size
            offset = self._pos % self._block_size
            block = self._block_for(index)
            if not block:
                break
            take = min(wanted - collected, len(block) - offset)
            if take <= 0:
                break
            chunks.append(block[offset : offset + take])
            self._pos += take
            collected += take
        return b"".join(chunks)

    def _read_direct(self, size: int) -> bytes:
        """Serve a large contiguous read in a single request, bypassing the cache."""
        if self._pos >= self.size:
            return b""
        wanted = min(size, self.size - self._pos)
        self._check_budget(wanted)
        with self._lock:
            self._cache_stats.cache_misses += 1
        data = self._fetcher.fetch(self._pos, wanted)
        self._pos += len(data)
        return data

    def _read_to_eof(self) -> bytes:
        """Read the whole remainder, block by block.

        Deliberately goes through the block path rather than the direct path: a
        parser calling ``read()`` with no argument must get real data, and the
        per-fetch budget check still stops runaway reads on remote sources.
        """
        if self._pos >= self.size:
            return b""
        return self._read_blockwise(self.size - self._pos)


class BytesFetcher:
    """In-memory ``Fetcher``; handy for tests and for probing byte blobs."""

    def __init__(self, data: bytes, *, name: str = "<bytes>") -> None:
        self._data = data
        self.size = len(data)
        self.name = name
        self.stats = FetchStats()
        self.fetched_ranges: list[tuple[int, int]] = []

    def fetch(self, start: int, length: int) -> bytes:
        """Return bytes, recording the range so tests can assert access patterns."""
        self.fetched_ranges.append((start, length))
        chunk = self._data[start : start + length]
        self.stats.add_fetch(len(chunk))
        return chunk

    def close(self) -> None:
        return None


def open_block_reader(data: bytes, **kwargs: Any) -> SeekableBlockReader:
    """Convenience wrapper: wrap an in-memory blob in a seekable reader."""
    fetcher = BytesFetcher(data)
    return SeekableBlockReader(fetcher, **kwargs)


__all__ = [
    "DEFAULT_BLOCK_SIZE",
    "DEFAULT_MAX_BLOCKS",
    "BytesFetcher",
    "FetchError",
    "FetchStats",
    "Fetcher",
    "RangeNotSupported",
    "ReadBudgetExceeded",
    "SeekableBlockReader",
    "open_block_reader",
]
