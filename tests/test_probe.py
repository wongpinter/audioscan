"""Probe-level tests: real MP3/M4B/FLAC containers, including ranged reads."""

from __future__ import annotations

import io
import random

import pytest

from audioscan.probe import cover_bytes, probe
from audioscan.reader import BytesFetcher, SeekableBlockReader
from fixtures import JPEG_COVER, PNG_COVER, build_flac, build_m4b, build_mp3, mp3_duration


# --------------------------------------------------------------------------- #
# MP3 / ID3
# --------------------------------------------------------------------------- #
def test_mp3_tags_and_stream_properties() -> None:
    frames = 120
    data = build_mp3(
        title="The Battle of Hogwarts",
        artist="J.K. Rowling",
        album="Deathly Hallows",
        albumartist="J.K. Rowling",
        track="7/36",
        genre="Audiobook",
        date="2007",
        comment="Chapter 31",
        frames=frames,
    )
    meta = probe(io.BytesIO(data), name="ch31.mp3", size=len(data))

    assert meta.is_ok, meta.error
    assert meta.format == "MP3"
    assert meta.title == "The Battle of Hogwarts"
    assert meta.artist == "J.K. Rowling"
    assert meta.album == "Deathly Hallows"
    assert meta.albumartist == "J.K. Rowling"
    assert meta.track == "7/36"
    assert meta.genre == "Audiobook"
    assert meta.date == "2007"
    assert meta.comment == "Chapter 31"
    assert meta.bitrate == 128_000
    assert meta.sample_rate == 44_100
    assert meta.channels == 2
    assert meta.codec == "MPEG 1 Layer 3"
    assert meta.duration == pytest.approx(mp3_duration(frames), abs=0.05)
    assert meta.mime_type == "audio/mp3"


def test_mp3_without_id3_still_reports_stream_info() -> None:
    data = build_mp3(with_id3=False, frames=60)
    meta = probe(io.BytesIO(data), name="raw.mp3")
    assert meta.is_ok
    assert meta.title is None
    assert meta.duration == pytest.approx(mp3_duration(60), abs=0.05)


def test_mp3_chapters_come_from_chap_and_ctoc() -> None:
    data = build_mp3(chapters=[(0.0, "Opening"), (12.5, "The Battle"), (90.0, "Aftermath")])
    meta = probe(io.BytesIO(data), name="ch.mp3")
    assert [c.number for c in meta.chapters] == [1, 2, 3]
    assert [c.title for c in meta.chapters] == ["Opening", "The Battle", "Aftermath"]
    assert [c.start for c in meta.chapters] == [0.0, 12.5, 90.0]
    assert meta.chapters[0].end == pytest.approx(12.5)
    assert meta.chapters[-1].end is None


def test_mp3_cover_art_is_detected_and_extractable() -> None:
    data = build_mp3(cover=JPEG_COVER, cover_mime="image/jpeg")
    meta = probe(io.BytesIO(data), name="cover.mp3")
    assert meta.has_cover
    assert len(meta.covers) == 1
    assert meta.covers[0].mime == "image/jpeg"
    assert meta.covers[0].size == len(JPEG_COVER)

    payload, mime = cover_bytes(io.BytesIO(data), 0)
    assert payload == JPEG_COVER
    assert mime == "image/jpeg"

    with pytest.raises(IndexError):
        cover_bytes(io.BytesIO(data), 3)


# --------------------------------------------------------------------------- #
# MP4 / M4B
# --------------------------------------------------------------------------- #
def test_m4b_tags_chapters_and_bitrate() -> None:
    data = build_m4b(
        title="The Dark Lord Ascending",
        artist="Stephen Fry",
        album="Deathly Hallows",
        albumartist="J.K. Rowling",
        track=(7, 36),
        genre="Audiobook",
        date="2007-07-21",
        comment="read by Stephen Fry",
        cover=JPEG_COVER,
        chapters=[(0.0, "Chapter 1"), (32.5, "Chapter 2"), (120.25, "Chapter 3")],
        duration=600.0,
    )
    meta = probe(io.BytesIO(data), name="book.m4b", size=len(data))

    assert meta.is_ok, meta.error
    assert meta.format == "MP4"
    assert meta.title == "The Dark Lord Ascending"
    assert meta.artist == "Stephen Fry"
    assert meta.album == "Deathly Hallows"
    assert meta.albumartist == "J.K. Rowling"
    assert meta.track == "7/36"
    assert meta.genre == "Audiobook"
    assert meta.date == "2007-07-21"
    assert meta.comment == "read by Stephen Fry"
    assert meta.duration == pytest.approx(600.0)
    assert meta.bitrate == 128_000
    assert meta.sample_rate == 44_100
    assert meta.channels == 2
    assert "AAC" in (meta.codec or "")

    assert [c.title for c in meta.chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert [c.start for c in meta.chapters] == [0.0, 32.5, 120.25]
    assert meta.covers[0].mime == "image/jpeg"


def test_m4b_png_cover_mime() -> None:
    data = build_m4b(cover=PNG_COVER)
    meta = probe(io.BytesIO(data), name="png.m4b")
    assert meta.covers[0].mime == "image/png"


def test_m4b_bitrate_falls_back_to_size_over_duration() -> None:
    """Without an esds bitrate, derive one from size and duration."""
    data = build_m4b(avg_bitrate=0, duration=60.0, mdat_size=120_000)
    meta = probe(io.BytesIO(data), name="no-esds.m4b", size=len(data))
    assert meta.is_ok
    assert meta.bitrate == int(len(data) * 8 / 60.0)


def test_m4b_without_tags_or_chapters() -> None:
    data = build_m4b(with_tags=False, duration=30.0)
    meta = probe(io.BytesIO(data), name="bare.m4b")
    assert meta.is_ok
    assert meta.title is None
    assert meta.chapters == []
    assert meta.duration == pytest.approx(30.0)


def test_m4b_chapters_absent_is_empty_list() -> None:
    data = build_m4b(title="No chapters")
    meta = probe(io.BytesIO(data), name="flat.m4b")
    assert meta.chapters == []


# --------------------------------------------------------------------------- #
# FLAC / Vorbis comments
# --------------------------------------------------------------------------- #
def test_flac_tags_and_vorbis_chapters() -> None:
    data = build_flac(
        tags={
            "TITLE": "Muad'Dib",
            "ARTIST": "Frank Herbert",
            "ALBUM": "Dune",
            "ALBUMARTIST": "Frank Herbert",
            "TRACKNUMBER": "12",
            "GENRE": "Science Fiction",
            "DATE": "1965",
            "CHAPTER001": "00:00:00.000",
            "CHAPTER001NAME": "Opening",
            "CHAPTER002": "00:12:31.500",
            "CHAPTER002NAME": "The Sleeper",
        },
        duration=120.0,
    )
    meta = probe(io.BytesIO(data), name="dune.flac")

    assert meta.is_ok, meta.error
    assert meta.format == "FLAC"
    assert meta.title == "Muad'Dib"
    assert meta.artist == "Frank Herbert"
    assert meta.album == "Dune"
    assert meta.albumartist == "Frank Herbert"
    assert meta.track == "12"
    assert meta.genre == "Science Fiction"
    assert meta.date == "1965"
    assert meta.codec == "FLAC 16-bit"
    assert meta.duration == pytest.approx(120.0)
    assert [c.start for c in meta.chapters] == [0.0, 751.5]
    assert [c.title for c in meta.chapters] == ["Opening", "The Sleeper"]


# --------------------------------------------------------------------------- #
# ranged reads (the point of the project)
# --------------------------------------------------------------------------- #
def test_moov_at_end_is_fully_readable_through_block_reader() -> None:
    """A head-only reader gets nothing useful here; a seekable one does not care."""
    data = build_m4b(
        title="Tail moov",
        chapters=[(0.0, "One"), (10.0, "Two")],
        duration=3600.0,
        mdat_size=2_000_000,
        moov_at_end=True,
    )
    assert data.index(b"moov") > data.index(b"mdat")

    fetcher = BytesFetcher(data, name="tail.m4b")
    reader = SeekableBlockReader(fetcher, block_size=64 * 1024)
    meta = probe(reader, name="tail.m4b", size=len(data))

    assert meta.is_ok, meta.error
    assert meta.title == "Tail moov"
    assert meta.duration == pytest.approx(3600.0)
    assert len(meta.chapters) == 2
    assert reader.stats.bytes_fetched < len(data) / 4


def test_reprobing_a_reader_reuses_the_cache() -> None:
    data = build_m4b(title="Cached", chapters=[(0.0, "One")], mdat_size=400_000)
    fetcher = BytesFetcher(data, name="cached.m4b")
    reader = SeekableBlockReader(fetcher, block_size=64 * 1024, max_blocks=64)

    first = probe(reader, name="cached.m4b")
    requests_after_first = fetcher.stats.requests
    assert first.is_ok
    assert requests_after_first > 0

    second = probe(reader, name="cached.m4b")
    assert second.title == "Cached"
    assert fetcher.stats.requests == requests_after_first


def test_probe_over_ranged_reader_never_downloads_the_whole_file() -> None:
    """Probing a 4 MB file must cost far less than 4 MB of range traffic."""
    data = build_m4b(title="Big", chapters=[(0.0, "A")], mdat_size=4_000_000)
    fetcher = BytesFetcher(data, name="big.m4b")
    reader = SeekableBlockReader(fetcher, block_size=64 * 1024)
    meta = probe(reader, name="big.m4b", size=len(data))

    assert meta.is_ok
    assert reader.stats.bytes_fetched < 512 * 1024
    assert len(fetcher.fetched_ranges) <= 12


def test_probe_with_random_seek_order_is_consistent() -> None:
    """Parsing must not depend on read order (the cache serves any layout)."""
    data = build_mp3(title="Order", chapters=[(0.0, "A"), (5.0, "B")], frames=80)
    rng = random.Random(11)
    for _ in range(5):
        fetcher = BytesFetcher(data)
        reader = SeekableBlockReader(fetcher, block_size=rng.choice([512, 4096, 32_768]))
        assert probe(reader, name="order.mp3").title == "Order"


# --------------------------------------------------------------------------- #
# failure handling
# --------------------------------------------------------------------------- #
def test_unreadable_data_reports_an_error_instead_of_raising() -> None:
    meta = probe(io.BytesIO(b"this is not audio at all" * 40), name="junk.mp3")
    assert not meta.is_ok
    assert meta.error


def test_empty_input_reports_an_error() -> None:
    meta = probe(io.BytesIO(b""), name="empty.mp3")
    assert not meta.is_ok


def test_m4b_missing_mp4_signature_reports_header_bytes() -> None:
    meta = probe(io.BytesIO(b"\0" * 256), name="broken.m4b")
    assert not meta.is_ok
    assert "no MP4 'ftyp' signature" in (meta.error or "")
    assert "header: 00000000000000000000000000000000" in (meta.error or "")


def test_plain_text_mp3_is_rejected() -> None:
    meta = probe(io.BytesIO(b"hello world\n" * 100), name="notes.mp3")
    assert not meta.is_ok
