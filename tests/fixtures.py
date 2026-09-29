"""Deterministic audio fixtures built byte-by-byte.

No ``ffmpeg`` required. Each builder emits a genuinely valid container so the
tests exercise mutagen's real parsers:

* :func:`build_mp3` — an ID3v2 tag followed by real MPEG-1 Layer III frames.
* :func:`build_m4b` — a small MP4 with a working ``esds``, ``ilst`` tags and a
  ``chpl`` chapter atom. The ``moov`` box is placed *after* ``mdat`` by default,
  which reproduces the "tags at the end of the file" layout that breaks naive
  head-only readers.
* :func:`build_flac` — a real STREAMINFO block plus a VORBIS_COMMENT block.
"""

from __future__ import annotations

import io
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path

from mutagen.id3 import APIC, CHAP, COMM, CTOC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK

# --------------------------------------------------------------------------- #
# MP3
# --------------------------------------------------------------------------- #
#: MPEG-1 Layer III, 128 kbps, 44.1 kHz, no padding.
_MPEG_HEADER = b"\xff\xfb\x90\x00"
_MPEG_FRAME_LENGTH = 417
_SAMPLES_PER_FRAME = 1152
_MPEG_SAMPLE_RATE = 44100

JPEG_COVER = b"\xff\xd8\xff\xe0" + b"\x00" * 256
PNG_COVER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256


def mpeg_frames(count: int) -> bytes:
    """Return ``count`` valid MPEG-1 Layer III frames."""
    frame = _MPEG_HEADER + b"\x00" * (_MPEG_FRAME_LENGTH - len(_MPEG_HEADER))
    return frame * count


def mp3_duration(count: int) -> float:
    """Duration mutagen should report for ``count`` frames."""
    return count * _SAMPLES_PER_FRAME / _MPEG_SAMPLE_RATE


def build_mp3(
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    albumartist: str | None = None,
    track: str | None = None,
    genre: str | None = None,
    date: str | None = None,
    comment: str | None = None,
    chapters: Sequence[tuple[float, str]] | None = None,
    cover: bytes | None = None,
    cover_mime: str = "image/jpeg",
    frames: int = 120,
    with_id3: bool = True,
) -> bytes:
    """Build an MP3 with an ID3v2 tag, optional chapters and a cover image."""
    buffer = io.BytesIO()
    if with_id3:
        tag = ID3()
        if title:
            tag.add(TIT2(encoding=3, text=[title]))
        if artist:
            tag.add(TPE1(encoding=3, text=[artist]))
        if album:
            tag.add(TALB(encoding=3, text=[album]))
        if albumartist:
            tag.add(TPE2(encoding=3, text=[albumartist]))
        if track:
            tag.add(TRCK(encoding=3, text=[track]))
        if genre:
            tag.add(TCON(encoding=3, text=[genre]))
        if date:
            tag.add(TDRC(encoding=3, text=[date]))
        if comment:
            tag.add(COMM(encoding=3, lang="eng", desc="", text=[comment]))
        if cover:
            tag.add(APIC(encoding=3, mime=cover_mime, type=3, desc="front", data=cover))
        if chapters:
            element_ids = _add_chapters(tag, chapters)
            tag.add(
                CTOC(
                    element_id="toc",
                    flags=3,
                    child_element_ids=element_ids,
                    sub_frames=[TIT2(encoding=3, text=["Table of contents"])],
                )
            )
        tag.save(buffer)
    buffer.write(mpeg_frames(frames))
    return buffer.getvalue()


def _add_chapters(tag: ID3, chapters: Sequence[tuple[float, str]]) -> list[str]:
    element_ids: list[str] = []
    for index, (start, name) in enumerate(chapters):
        element_id = f"ch{index + 1}"
        end = chapters[index + 1][0] if index + 1 < len(chapters) else 0.0
        tag.add(
            CHAP(
                element_id=element_id,
                start_time=int(round(start * 1000)),
                end_time=int(round(end * 1000)),
                sub_frames=[TIT2(encoding=3, text=[name])],
            )
        )
        element_ids.append(element_id)
    return element_ids


# --------------------------------------------------------------------------- #
# MP4 / M4B
# --------------------------------------------------------------------------- #
def atom(name: str, payload: bytes) -> bytes:
    """Build an MP4 atom. Names are latin-1: ``©nam`` is 4 bytes, not 5."""
    encoded = name.encode("latin-1")
    assert len(encoded) == 4, f"atom name must be 4 bytes: {name!r}"
    return struct.pack(">I", len(payload) + 8) + encoded + payload


def fullbox(name: str, payload: bytes = b"", version: int = 0, flags: int = 0) -> bytes:
    """Build a full box (version + flags prefix)."""
    return atom(name, struct.pack(">B", version) + struct.pack(">I", flags)[1:] + payload)


def _descriptor(tag: int, payload: bytes) -> bytes:
    """Build an MPEG-4 descriptor (single-byte length; all fixtures are small)."""
    assert len(payload) < 128
    return struct.pack(">BB", tag, len(payload)) + payload


def _esds(avg_bitrate: int, sample_rate: int, channels: int) -> bytes:
    """AudioSpecificConfig inside an ES_Descriptor, so mutagen reports a bitrate."""
    rate_index = {48000: 3, 44100: 4, 32000: 5, 22050: 7, 16000: 8}.get(sample_rate, 4)
    asc = bytes([(2 << 3) | (rate_index >> 1), ((rate_index & 1) << 7) | (channels << 3)])
    decoder_config = _descriptor(
        0x04,
        b"\x40\x15\x00\x18\x00"
        + struct.pack(">I", avg_bitrate)
        + struct.pack(">I", avg_bitrate)
        + _descriptor(0x05, asc),
    )
    es = _descriptor(0x03, b"\x00\x01\x00" + decoder_config + _descriptor(0x06, b"\x02"))
    return atom("esds", b"\x00\x00\x00\x00" + es)


def _sample_entry(sample_rate: int, channels: int, avg_bitrate: int) -> bytes:
    # AudioSampleEntry: 28 fixed bytes, then child atoms (esds). mutagen requires
    # at least one child atom here and raises "truncated data" without it.
    fixed = (
        b"\x00" * 6
        + struct.pack(">H", 1)
        + b"\x00" * 8
        + struct.pack(">HHHH", channels, 16, 0, 0)
        + struct.pack(">I", sample_rate << 16)
    )
    return atom("mp4a", fixed + _esds(avg_bitrate, sample_rate, channels))


def _mvhd(timescale: int, duration: int) -> bytes:
    return fullbox(
        "mvhd",
        struct.pack(">II", 0, 0)
        + struct.pack(">II", timescale, duration)
        + struct.pack(">IH", 0x00010000, 0x0100)
        + b"\x00" * 10
        + struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
        + b"\x00" * 24
        + struct.pack(">I", 0xFFFF),
    )


def _trak(sample_rate: int, duration: int, channels: int, avg_bitrate: int) -> bytes:
    mdhd = fullbox(
        "mdhd",
        struct.pack(">II", 0, 0)
        + struct.pack(">II", sample_rate, duration)
        + struct.pack(">HH", 0x55C4, 0),
    )
    hdlr = fullbox("hdlr", b"\x00" * 4 + b"soun" + b"\x00" * 12 + b"SoundHandler\x00")
    stsd = fullbox("stsd", struct.pack(">I", 1) + _sample_entry(sample_rate, channels, avg_bitrate))
    minf = atom("minf", fullbox("smhd", b"\x00" * 4) + atom("stbl", stsd))
    return atom("trak", atom("mdia", mdhd + hdlr + minf))


def _cover_data_type(data: bytes) -> int:
    """MP4 well-known data type for an image payload (13 = JPEG, 14 = PNG)."""
    if data.startswith(b"\x89PNG"):
        return 14
    if data.startswith(b"\xff\xd8"):
        return 13
    return 0


def _ilst_item(name: str, payload: bytes, data_type: int = 1) -> bytes:
    return atom(name, atom("data", struct.pack(">II", data_type, 0) + payload))


def _ilst(
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    albumartist: str | None = None,
    track: tuple[int, int] | None = None,
    genre: str | None = None,
    date: str | None = None,
    comment: str | None = None,
    cover: bytes | None = None,
) -> bytes:
    items = b""
    if title:
        items += _ilst_item("\xa9nam", title.encode())
    if artist:
        items += _ilst_item("\xa9ART", artist.encode())
    if album:
        items += _ilst_item("\xa9alb", album.encode())
    if albumartist:
        items += _ilst_item("aART", albumartist.encode())
    if genre:
        items += _ilst_item("\xa9gen", genre.encode())
    if date:
        items += _ilst_item("\xa9day", date.encode())
    if comment:
        items += _ilst_item("\xa9cmt", comment.encode())
    if track:
        items += _ilst_item("trkn", struct.pack(">HHH", 0, track[0], track[1]))
    if cover:
        items += _ilst_item("covr", cover, data_type=_cover_data_type(cover))
    return atom("ilst", items)


def _chpl(chapters: Sequence[tuple[float, str]]) -> bytes:
    # Nero-style: 4 reserved bytes, 1 byte count, then (u64 start in 100 ns, u8 len, title).
    payload = b"\x00" * 4 + struct.pack(">B", len(chapters))
    for start_seconds, title in chapters:
        encoded = title.encode()
        payload += struct.pack(">Q", round(start_seconds * 10_000_000))
        payload += struct.pack(">B", len(encoded)) + encoded
    return fullbox("chpl", payload)


def build_m4b(
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    albumartist: str | None = None,
    track: tuple[int, int] | None = None,
    genre: str | None = None,
    date: str | None = None,
    comment: str | None = None,
    cover: bytes | None = None,
    chapters: Sequence[tuple[float, str]] | None = None,
    duration: float = 60.0,
    sample_rate: int = 44100,
    channels: int = 2,
    avg_bitrate: int = 128_000,
    mdat_size: int = 120_000,
    moov_at_end: bool = True,
    with_tags: bool = True,
) -> bytes:
    """Build a small but valid M4B, by default with ``moov`` after ``mdat``."""
    timescale = 1000
    udta_payload = b""
    if with_tags:
        udta_payload += fullbox(
            "meta",
            _ilst(
                title=title,
                artist=artist,
                album=album,
                albumartist=albumartist,
                track=track,
                genre=genre,
                date=date,
                comment=comment,
                cover=cover,
            ),
        )
    if chapters:
        udta_payload += _chpl(chapters)
    udta = atom("udta", udta_payload) if udta_payload else b""

    moov = atom(
        "moov",
        _mvhd(timescale, int(duration * timescale))
        + _trak(sample_rate, int(duration * sample_rate), channels, avg_bitrate)
        + udta,
    )
    ftyp = atom("ftyp", b"M4B \x00\x00\x00\x00M4B isommp42")
    mdat = atom("mdat", b"\x00" * mdat_size)
    return ftyp + (mdat + moov if moov_at_end else moov + mdat)


# --------------------------------------------------------------------------- #
# FLAC
# --------------------------------------------------------------------------- #
def build_flac(
    *,
    tags: Mapping[str, str | Sequence[str]] | None = None,
    sample_rate: int = 44100,
    channels: int = 2,
    bits_per_sample: int = 16,
    duration: float = 5.0,
    include_audio_frames: bool = True,
) -> bytes:
    """Build a FLAC file: STREAMINFO + VORBIS_COMMENT + dummy frame bytes."""
    total_samples = int(duration * sample_rate)
    streaminfo_bits = (
        (sample_rate << 44) | ((channels - 1) << 41) | ((bits_per_sample - 1) << 36) | total_samples
    )
    streaminfo = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x00"
        + b"\x00\x00\x00"
        + streaminfo_bits.to_bytes(8, "big")
        + b"\x00" * 16
    )
    out = bytearray(b"fLaC")
    out += bytes([0x00]) + len(streaminfo).to_bytes(3, "big") + streaminfo  # not last

    comment = _vorbis_comment(tags or {})
    out += bytes([0x04 | 0x80]) + len(comment).to_bytes(3, "big") + comment  # last block
    if include_audio_frames:
        out += b"\xff\xf8" + b"\x00" * 64
    return bytes(out)


def _vorbis_comment(tags: Mapping[str, str | Sequence[str]]) -> bytes:
    """Vorbis comment block — note the little-endian framing."""
    vendor = b"audioscan-tests"
    out = bytearray(struct.pack("<I", len(vendor)) + vendor)
    pairs: list[tuple[str, str]] = []
    for key, value in tags.items():
        values = [value] if isinstance(value, str) else list(value)
        for item in values:
            pairs.append((key, item))
    out += struct.pack("<I", len(pairs))
    for key, value in pairs:
        entry = f"{key}={value}".encode()
        out += struct.pack("<I", len(entry)) + entry
    return bytes(out)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def write_file(directory: Path, name: str, data: bytes) -> Path:
    """Write ``data`` to ``directory/name`` (creating parents) and return the path."""
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


#: File names taken from a real audiobook library, for naming tests.
REAL_NAMES = (
    "Chapter 31; The Battle of Hogwarts.mp3",
    "Chapter 06; Gilderoy Lockhart.mp3",
    "THE CALL OF THE OLD ONES; 35 Cthulhu Mythos Stories (2.mp3.part",
    "01 - Opening Credits.mp3",
    "Track 4 - The Wanderer.m4b",
    "Dune - 12 - Muad'Dib.flac",
)
