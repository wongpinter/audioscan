"""Chapter parsing across the three conventions."""

from __future__ import annotations

import io

import pytest

from audioscan.chapters import (
    chapters_from_id3,
    chapters_from_mp4,
    chapters_from_vorbis,
    extract_chapters,
    parse_vorbis_time,
)
from audioscan.probe import probe
from fixtures import build_mp3


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00:00:00.000", 0.0),
        ("00:12:31.500", 751.5),
        ("01:02:03", 3723.0),
        ("12:34", 754.0),
        ("90", 90.0),
        ("0:00:01,250", 1.25),
        ("", None),
        ("not-a-time", None),
        ([], None),
    ],
)
def test_parse_vorbis_time(value: object, expected: float | None) -> None:
    assert parse_vorbis_time(value) == expected


def test_chapters_from_vorbis_orders_by_start() -> None:
    tags = {
        "CHAPTER002": ["00:02:00.000"],
        "CHAPTER002NAME": ["Second"],
        "CHAPTER001": ["00:00:30.000"],
        "CHAPTER001NAME": ["First"],
        "CHAPTER003": ["00:05:00.000"],
        # A name with no timestamp is ignored rather than invented.
        "CHAPTER004NAME": ["Orphan"],
    }
    chapters = chapters_from_vorbis(tags)
    assert [c.number for c in chapters] == [1, 2, 3]
    assert [c.title for c in chapters] == ["First", "Second", None]
    assert [c.start for c in chapters] == [30.0, 120.0, 300.0]
    assert chapters[0].end == pytest.approx(120.0)
    assert chapters[-1].end is None


def test_chapters_from_vorbis_handles_unparsable_timestamps() -> None:
    chapters = chapters_from_vorbis({"CHAPTER001": ["bogus"], "CHAPTER001NAME": ["Nope"]})
    assert chapters == []


def test_chapters_from_none_is_empty() -> None:
    assert chapters_from_vorbis(None) == []
    assert chapters_from_id3(None) == []
    assert chapters_from_mp4(object()) == []
    assert extract_chapters(None) == []


def test_chapters_from_id3_respects_ctoc_order() -> None:
    data = build_mp3(chapters=[(0.0, "One"), (30.0, "Two"), (60.0, "Three")])
    audio = _parse(data)
    chapters = chapters_from_id3(audio.tags)
    assert [c.title for c in chapters] == ["One", "Two", "Three"]
    assert [c.start for c in chapters] == [0.0, 30.0, 60.0]


def test_chapters_from_id3_without_ctoc_still_works() -> None:
    """CHAP frames alone are enough; CTOC only supplies ordering."""
    from mutagen.id3 import CHAP, ID3, TIT2

    tag = ID3()
    tag.add(
        CHAP(
            element_id="later",
            start_time=5000,
            end_time=0,
            sub_frames=[TIT2(encoding=3, text=["Later"])],
        )
    )
    tag.add(
        CHAP(
            element_id="earlier",
            start_time=1000,
            end_time=5000,
            sub_frames=[TIT2(encoding=3, text=["Earlier"])],
        )
    )
    chapters = chapters_from_id3(tag)
    assert [c.title for c in chapters] == ["Earlier", "Later"]
    assert [c.number for c in chapters] == [1, 2]


def test_chapters_from_vorbis_through_a_real_flac() -> None:
    from fixtures import build_flac

    data = build_flac(
        tags={
            "CHAPTER001": "00:00:00.000",
            "CHAPTER001NAME": "Prologue",
            "CHAPTER002": "00:20:00.000",
            "CHAPTER002NAME": "The Journey",
        }
    )
    meta = probe(io.BytesIO(data), name="chapters.flac")
    assert [c.title for c in meta.chapters] == ["Prologue", "The Journey"]
    assert meta.chapters[1].start == pytest.approx(1200.0)


def _parse(data: bytes) -> object:
    import mutagen

    audio = mutagen.File(io.BytesIO(data))
    assert audio is not None
    return audio
