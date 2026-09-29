"""Local filesystem source: plain ``open()``, no range requests needed."""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from datetime import UTC
from pathlib import Path
from typing import Any, BinaryIO

from ..models import SOURCE_LOCAL
from ..naming import PARTIAL_SUFFIXES, strip_partial_suffixes
from .base import RemoteFile

AUDIO_EXTENSIONS = frozenset(
    {
        ".aac",
        ".aif",
        ".aiff",
        ".ape",
        ".dff",
        ".dsf",
        ".flac",
        ".m4a",
        ".m4b",
        ".m4p",
        ".mka",
        ".mp2",
        ".mp3",
        ".mp4",
        ".mpc",
        ".oga",
        ".ogg",
        ".opus",
        ".spx",
        ".tta",
        ".wav",
        ".wave",
        ".wma",
    }
)


def looks_like_audio(name: str, extensions: frozenset[str] = AUDIO_EXTENSIONS) -> bool:
    """True when a file name ends in a known audio extension.

    Also accepts interrupted downloads (``book.mp3.part``) so they show up as
    partials instead of vanishing from the report.
    """
    return any(strip_partial_suffixes(name).endswith(ext) for ext in extensions)


class LocalSource:
    """Enumerate audio files under one or more local paths."""

    name = SOURCE_LOCAL

    def __init__(
        self,
        roots: Sequence[Path] | Path,
        *,
        extensions: frozenset[str] = AUDIO_EXTENSIONS,
        recursive: bool = True,
        root_label: str | None = None,
    ) -> None:
        if isinstance(roots, (str, Path)):
            roots = [Path(roots)]
        self._roots = [Path(root) for root in roots]
        self._extensions = extensions
        self._recursive = recursive
        self._root_label = root_label

    def iter_files(self) -> Iterator[RemoteFile]:
        """Yield every audio file found, sorted for stable output."""
        for root in self._roots:
            if root.is_file():
                yield self._describe(root, root.parent)
                continue
            if not root.is_dir():
                raise FileNotFoundError(f"no such file or directory: {root}")
            yield from self._walk(root)

    def _walk(self, root: Path) -> Iterator[RemoteFile]:
        if self._recursive:
            walker: Any = os.walk(root)
            for current, dirnames, filenames in walker:
                dirnames.sort()
                for filename in sorted(filenames):
                    if not looks_like_audio(filename, self._extensions):
                        continue
                    yield self._describe(Path(current) / filename, root)
        else:
            for entry in sorted(root.iterdir()):
                if entry.is_file() and looks_like_audio(entry.name, self._extensions):
                    yield self._describe(entry, root)

    def _describe(self, path: Path, root: Path) -> RemoteFile:
        try:
            stat = path.stat()
            size: int | None = stat.st_size
            modified: str | None = _iso(stat.st_mtime)
        except OSError:
            size = None
            modified = None
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.name
        return RemoteFile(
            id=str(path),
            name=path.name,
            path=relative,
            size=size,
            mime_type=None,
            modified=modified,
            extra={"abs_path": str(path)},
        )

    def open(self, item: RemoteFile) -> BinaryIO:
        """Open the file for reading."""
        target = item.extra.get("abs_path") or item.id
        return Path(target).open("rb")

    def close(self) -> None:
        return None


def _iso(timestamp: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="seconds")


__all__ = ["AUDIO_EXTENSIONS", "PARTIAL_SUFFIXES", "LocalSource", "looks_like_audio"]
