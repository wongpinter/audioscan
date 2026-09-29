"""HTTP range source.

This is what makes remote probing cheap: instead of ``GET``-ing a 400 MB file,
every read becomes a ``Range: bytes=start-end`` request against the same URL. A
single ``httpx.Client`` is shared so connections are reused across files.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from ..models import SOURCE_HTTP
from ..reader import (
    DEFAULT_BLOCK_SIZE,
    FetchError,
    FetchStats,
    RangeNotSupported,
    SeekableBlockReader,
)
from .base import AuthProvider, ReadableStream, RemoteFile

#: Statuses worth retrying: transient server/network conditions.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_CONTENT_RANGE_RE = re.compile(
    r"bytes[\s=]+(?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+|\*)", re.IGNORECASE
)
_CHUNK = 64 * 1024


def parse_content_range(value: str | None) -> tuple[int | None, int | None, int | None]:
    """Parse a ``Content-Range: bytes 0-1023/4096`` header."""
    if not value:
        return (None, None, None)
    match = _CONTENT_RANGE_RE.search(value)
    if not match:
        return (None, None, None)
    total_text = match.group("total")
    total = int(total_text) if total_text.isdigit() else None
    return (int(match.group("start")), int(match.group("end")), total)


def filename_from_url(url: str) -> str:
    """Best-effort display name for a URL."""
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return name or url


class HttpRangeFetcher:
    """Serve byte ranges from an HTTP(S) URL."""

    def __init__(
        self,
        url: str,
        *,
        size: int | None = None,
        client: httpx.Client | None = None,
        headers: Mapping[str, str] | None = None,
        auth: AuthProvider | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        name: str = "",
    ) -> None:
        self._url = url
        self.name = name or filename_from_url(url)
        self.size: int = size or 0
        self._headers = dict(headers or {})
        self._auth = auth
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=True)
        self.stats = FetchStats()
        self._lock = threading.Lock()

    @classmethod
    def open(cls, url: str, **kwargs: Any) -> HttpRangeFetcher:
        """Create a fetcher, discovering the resource size when not supplied."""
        fetcher = cls(url, **kwargs)
        try:
            if not fetcher.size:
                fetcher.size = fetcher._detect_size()
        except BaseException:
            fetcher.close()
            raise
        return fetcher

    # -- size discovery ------------------------------------------------------
    def _detect_size(self) -> int:
        """Determine the total size via HEAD, falling back to a 1-byte GET."""
        headers = dict(self._headers)
        if self._auth is not None:
            headers.update(self._auth.headers())
        try:
            response = self._client.head(self._url, headers=headers, timeout=self._timeout)
            if response.status_code < 400:
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > 0:
                    return int(length)
        except httpx.HTTPError:
            pass

        # Servers that reject HEAD, or omit Content-Length, still advertise the
        # total in a Content-Range header. Stream the response and never touch the
        # body: if the server ignores Range we must not pull the whole file down
        # just to learn its size.
        with self._client.stream(
            "GET", self._url, headers={**headers, "Range": "bytes=0-0"}, timeout=self._timeout
        ) as response:
            if response.status_code == 206:
                _start, _end, total = parse_content_range(response.headers.get("content-range"))
                if total:
                    return total
            if response.status_code == 200:
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > 0:
                    return int(length)
            raise FetchError(f"{self.name}: could not determine size (HTTP {response.status_code})")

    # -- reading -------------------------------------------------------------
    def fetch(self, start: int, length: int) -> bytes:
        """Fetch ``length`` bytes from ``start``."""
        if length <= 0 or start >= self.size:
            return b""
        end = min(start + length, self.size) - 1
        wanted = end - start + 1
        refreshed = False

        for attempt in range(self._max_retries + 1):
            headers = dict(self._headers)
            if self._auth is not None:
                headers.update(self._auth.headers())
            headers["Range"] = f"bytes={start}-{end}"

            try:
                data, status, retry_after = self._stream_range(headers, wanted)
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise FetchError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
                time.sleep(self._backoff(attempt))
                continue

            if status in (200, 206):
                if status == 200 and start > 0:
                    raise RangeNotSupported(
                        f"{self.name}: server ignored the Range request and cannot be seeked"
                    )
                with self._lock:
                    self.stats.add_fetch(len(data))
                return data
            if status == 416:
                return b""
            if status in (401, 403) and self._auth is not None and not refreshed:
                refreshed = True
                try:
                    self._auth.refresh()
                except Exception as exc:  # pragma: no cover - auth library specific
                    raise FetchError(f"{self.name}: credential refresh failed: {exc}") from exc
                continue
            if status in RETRY_STATUS and attempt < self._max_retries:
                time.sleep(retry_after if retry_after is not None else self._backoff(attempt))
                continue
            raise FetchError(f"{self.name}: HTTP {status} for range {start}-{end}")

        raise FetchError(f"{self.name}: gave up after {self._max_retries + 1} attempts")

    def _stream_range(
        self, headers: Mapping[str, str], wanted: int
    ) -> tuple[bytes, int, float | None]:
        """Perform one ranged request, reading at most ``wanted`` bytes."""
        with self._client.stream(
            "GET", self._url, headers=headers, timeout=self._timeout
        ) as response:
            status = response.status_code
            retry_after = _retry_after_seconds(response.headers.get("retry-after"))
            if status == 206:
                return response.read(), status, retry_after
            if status == 200:
                # The server ignored Range. Read only what was asked for and let
                # the connection close early rather than pulling the whole file.
                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_bytes(_CHUNK):
                    chunks.append(chunk)
                    received += len(chunk)
                    if received >= wanted:
                        break
                return b"".join(chunks)[:wanted], status, retry_after
            return b"", status, retry_after

    def _backoff(self, attempt: int) -> float:
        return min(8.0, 0.5 * (2**attempt))

    def close(self) -> None:
        """Close the HTTP client when this fetcher created it."""
        if self._owns_client:
            self._client.close()


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return min(seconds, 30.0)


class HttpSource:
    """A single audio file served over HTTP(S)."""

    name = SOURCE_HTTP

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        block_size: int = DEFAULT_BLOCK_SIZE,
        budget_bytes: int | None = None,
        max_retries: int = 3,
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._block_size = block_size
        self._budget_bytes = budget_bytes
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=True)

    def iter_files(self):
        """Yield the single remote file this source represents."""
        yield RemoteFile(
            id=self._url,
            name=filename_from_url(self._url),
            path=self._url,
            size=None,
            mime_type=None,
        )

    def open(self, item: RemoteFile) -> ReadableStream:
        """Open a seekable reader over ranged requests."""
        fetcher = HttpRangeFetcher.open(
            item.id,
            client=self._client,
            headers=self._headers,
            timeout=self._timeout,
            max_retries=self._max_retries,
            name=item.name,
        )
        return SeekableBlockReader(
            fetcher,
            block_size=self._block_size,
            budget_bytes=self._budget_bytes,
            name=item.name,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


__all__ = [
    "RETRY_STATUS",
    "HttpRangeFetcher",
    "HttpSource",
    "filename_from_url",
    "parse_content_range",
]
