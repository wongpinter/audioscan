"""Source abstraction: list audio files, then open one as a byte stream."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Protocol, runtime_checkable

from ..reader import SeekableBlockReader

#: What a source may hand to the probe layer: a plain file object, or our
#: seekable ranged reader.
ReadableStream = BinaryIO | SeekableBlockReader


@dataclass(slots=True)
class RemoteFile:
    """A file discovered by a :class:`Source`, described without reading it."""

    id: str
    name: str
    path: str = ""
    size: int | None = None
    mime_type: str | None = None
    modified: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.path:
            self.path = self.name


@runtime_checkable
class AuthProvider(Protocol):
    """Supplies and refreshes request headers for an authenticated fetcher."""

    def headers(self) -> dict[str, str]:
        """Return headers to attach to the next request."""

    def refresh(self) -> None:
        """Force a credential refresh after an authentication failure."""


@runtime_checkable
class Source(Protocol):
    """A collection of audio files that can be opened one at a time."""

    name: str

    def iter_files(self) -> Iterator[RemoteFile]:
        """Yield every audio file this source knows about."""

    def open(self, item: RemoteFile) -> ReadableStream:
        """Open ``item`` as a seekable binary stream."""

    def close(self) -> None:
        """Release any resources held by the source."""


__all__ = ["AuthProvider", "ReadableStream", "RemoteFile", "Source"]
