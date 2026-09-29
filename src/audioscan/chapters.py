"""Chapter extraction across the three conventions found in the wild.

* **ID3 (MP3)** — ``CHAP`` frames with millisecond offsets, optionally ordered by
  a ``CTOC`` table of contents frame.
* **MP4/M4B** — a ``chpl`` atom, surfaced by mutagen as ``audio.chapters``.
* **Vorbis comments (FLAC/OGG/Opus)** — ``CHAPTER003=00:12:31.500`` paired with
  ``CHAPTER003NAME=...``.

QuickTime chapter *tracks* (the ``tref``/``chap`` style used by some M4B rips)
are not parsed: mutagen does not expose them, so those files report zero chapters
rather than wrong ones.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from .models import Chapter

_CHAPTER_START_RE = re.compile(r"CHAPTER(\d+)$")
_CHAPTER_NAME_RE = re.compile(r"CHAPTER(\d+)NAME$")


def _first(value: Any) -> Any:
    """Unwrap mutagen's list-valued tags to a single scalar."""
    if isinstance(value, (list, tuple)):
        return _first(value[0]) if value else None
    return value


def parse_vorbis_time(value: Any) -> float | None:
    """Parse ``HH:MM:SS.mmm``, ``MM:SS`` or plain seconds into seconds."""
    if value is None:
        return None
    text = str(_first(value)).strip().replace(",", ".")
    if not text:
        return None
    if ":" in text:
        total = 0.0
        for part in text.split(":"):
            try:
                total = total * 60.0 + float(part or 0)
            except ValueError:
                return None
        return total
    try:
        return float(text)
    except ValueError:
        return None


def _finalise(pairs: list[tuple[float, str | None]]) -> list[Chapter]:
    """Sort by offset, number the chapters, and backfill missing end offsets."""
    ordered = sorted(pairs, key=lambda item: item[0])
    chapters: list[Chapter] = []
    for index, (start, title) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else None
        chapters.append(Chapter(number=index + 1, start=start, end=end, title=title))
    return chapters


def _sub_title(chap: Any) -> str | None:
    """Pull a human title out of a CHAP frame's nested TIT2/TIT3 frames."""
    for key in ("TIT2", "TIT3"):
        frame = getattr(chap, "sub_frames", {}).get(key)
        if frame is None:
            continue
        text = getattr(frame, "text", None)
        if text:
            return str(_first(text))
        rendered = str(frame).strip()
        if rendered:
            return rendered
    return None


def chapters_from_id3(tags: Any) -> list[Chapter]:
    """Extract chapters from ID3 ``CHAP``/``CTOC`` frames (MP3)."""
    if tags is None or not hasattr(tags, "getall"):
        return []
    try:
        chaps = list(tags.getall("CHAP"))
    except Exception:  # pragma: no cover - defensive against odd tags
        return []
    if not chaps:
        return []

    order: list[str] = []
    try:
        for ctoc in tags.getall("CTOC"):
            for child in getattr(ctoc, "child_element_ids", None) or []:
                if child not in order:
                    order.append(str(child))
    except Exception:  # pragma: no cover - defensive against odd tags
        order = []

    by_id = {str(chap.element_id): chap for chap in chaps}
    used: set[str] = set()
    seq: list[Any] = []
    for element_id in order:
        chap = by_id.get(element_id)
        if chap is not None and element_id not in used:
            seq.append(chap)
            used.add(element_id)
    for chap in chaps:
        element_id = str(chap.element_id)
        if element_id not in used:
            seq.append(chap)
            used.add(element_id)

    pairs: list[tuple[float, str | None]] = []
    for chap in seq:
        start = float(getattr(chap, "start_time", 0) or 0) / 1000.0
        pairs.append((start, _sub_title(chap)))
    return _finalise(pairs)


def chapters_from_mp4(audio: Any) -> list[Chapter]:
    """Extract chapters from an MP4/M4B ``chpl`` atom."""
    raw = getattr(audio, "chapters", None)
    if not raw:
        return []
    pairs: list[tuple[float, str | None]] = []
    for entry in raw:
        try:
            start = float(getattr(entry, "start", 0.0) or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        title = getattr(entry, "title", None)
        pairs.append((start, str(title) if title else None))
    return _finalise(pairs)


def chapters_from_vorbis(tags: Any) -> list[Chapter]:
    """Extract ``CHAPTERnnn`` / ``CHAPTERnnnNAME`` pairs from Vorbis comments."""
    if tags is None or not hasattr(tags, "keys"):
        return []
    starts: dict[int, float] = {}
    names: dict[int, str] = {}
    try:
        keys = list(tags.keys())
    except Exception:  # pragma: no cover - defensive
        return []

    for key in keys:
        upper = str(key).upper()
        name_match = _CHAPTER_NAME_RE.fullmatch(upper)
        if name_match:
            number = int(name_match.group(1))
            value = _first(tags.get(key))
            if value:
                names[number] = str(value)
            continue
        start_match = _CHAPTER_START_RE.fullmatch(upper)
        if start_match:
            number = int(start_match.group(1))
            seconds = parse_vorbis_time(tags.get(key))
            if seconds is not None:
                starts[number] = seconds

    pairs = [(start, names.get(number)) for number, start in starts.items()]
    return _finalise(pairs)


def extract_chapters(audio: Any) -> list[Chapter]:
    """Return chapters for any supported container, based on the parsed object."""
    if audio is None:
        return []

    class_name = type(audio).__name__
    if class_name == "MP4":
        return chapters_from_mp4(audio)

    tags = getattr(audio, "tags", None)
    if tags is None:
        # FLAC keeps its comments on the file object itself.
        tags = audio if hasattr(audio, "keys") else None

    if tags is not None and hasattr(tags, "getall"):
        found = chapters_from_id3(tags)
        if found:
            return found

    if tags is not None and hasattr(tags, "keys"):
        return chapters_from_vorbis(tags)
    return []


def decode_flac_picture(encoded: Any) -> Any:
    """Decode a base64 ``metadata_block_picture`` comment into a FLAC Picture."""
    from mutagen.flac import Picture

    raw = encoded
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else b""
    if isinstance(raw, str):
        raw = raw.encode("ascii", "ignore")
    return Picture(base64.b64decode(raw))


__all__ = [
    "chapters_from_id3",
    "chapters_from_mp4",
    "chapters_from_vorbis",
    "extract_chapters",
    "parse_vorbis_time",
]
