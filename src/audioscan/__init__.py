"""audioscan — read audio metadata without downloading whole files.

The core idea: audio tags live in a few kilobytes at the start (and sometimes the
end) of a file. ``audioscan`` serves ``mutagen`` a *seekable* view over HTTP range
requests, so probing a 400 MB audiobook costs a couple of megabytes of traffic
instead of the whole file.
"""

from __future__ import annotations

from .chapters import extract_chapters
from .models import Chapter, Cover, TrackMeta
from .naming import BookGroup, ParsedName, group_tracks, parse_name
from .probe import probe
from .reader import FetchStats, RangeNotSupported, SeekableBlockReader

__version__ = "0.1.0"

__all__ = [
    "BookGroup",
    "Chapter",
    "Cover",
    "FetchStats",
    "ParsedName",
    "RangeNotSupported",
    "SeekableBlockReader",
    "TrackMeta",
    "__version__",
    "extract_chapters",
    "group_tracks",
    "parse_name",
    "probe",
]
