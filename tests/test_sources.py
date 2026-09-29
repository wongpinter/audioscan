"""Local source discovery and CLI target parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from audioscan.sources import (
    SourceOptions,
    build_sources,
    classify_target,
    close_sources,
    collect_files,
    parse_gdrive_target,
)
from audioscan.sources.gdrive import DriveSource
from audioscan.sources.http import HttpSource
from audioscan.sources.local import LocalSource, looks_like_audio
from fixtures import write_file


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("book.mp3", True),
        ("BOOK.M4B", True),
        ("book.flac", True),
        ("book.mp3.part", True),
        ("book.mp3.crdownload", True),
        ("book.mp3.partial", True),
        ("notes.txt", False),
        ("cover.jpg", False),
        ("book", False),
    ],
)
def test_looks_like_audio(name: str, expected: bool) -> None:
    assert looks_like_audio(name) is expected


def test_local_source_walks_recursively_and_sorts(tmp_path: Path) -> None:
    write_file(tmp_path, "b.mp3", b"x")
    write_file(tmp_path, "a.mp3", b"x")
    write_file(tmp_path, "notes.txt", b"x")
    write_file(tmp_path, "sub/deep/c.m4b", b"x")

    source = LocalSource(tmp_path)
    assert [item.name for item in source.iter_files()] == ["a.mp3", "b.mp3", "c.m4b"]


def test_local_source_non_recursive(tmp_path: Path) -> None:
    write_file(tmp_path, "a.mp3", b"x")
    write_file(tmp_path, "sub/b.mp3", b"x")
    source = LocalSource(tmp_path, recursive=False)
    assert [item.name for item in source.iter_files()] == ["a.mp3"]


def test_local_source_reports_size_path_and_opens_file(tmp_path: Path) -> None:
    payload = b"hello audio"
    write_file(tmp_path, "sub/book.m4b", payload)
    source = LocalSource(tmp_path)
    item = next(iter(source.iter_files()))

    assert item.size == len(payload)
    assert item.path == "sub/book.m4b"
    assert item.modified
    assert item.extra["abs_path"].endswith("book.m4b")

    with source.open(item) as stream:
        assert stream.read() == payload


def test_local_source_accepts_a_single_file(tmp_path: Path) -> None:
    path = write_file(tmp_path, "one.mp3", b"123")
    source = LocalSource(path)
    items = list(source.iter_files())
    assert [item.name for item in items] == ["one.mp3"]
    assert items[0].path == "one.mp3"


def test_local_source_custom_extensions(tmp_path: Path) -> None:
    write_file(tmp_path, "a.mp3", b"x")
    write_file(tmp_path, "b.ogg", b"x")
    source = LocalSource(tmp_path, extensions=frozenset({".ogg"}))
    assert [item.name for item in source.iter_files()] == ["b.ogg"]


def test_local_source_missing_path_raises(tmp_path: Path) -> None:
    source = LocalSource(tmp_path / "nope")
    with pytest.raises(FileNotFoundError):
        list(source.iter_files())


@pytest.mark.parametrize(
    ("target", "kind"),
    [
        ("~/Books", "local"),
        ("/tmp/a.m4b", "local"),
        ("https://host/book.m4b", "http"),
        ("http://host/book.m4b", "http"),
        ("gdrive:", "gdrive"),
        ("gdrive://folder123", "gdrive"),
        ("gdrive://file/abc", "gdrive"),
    ],
)
def test_classify_target(target: str, kind: str) -> None:
    assert classify_target(target) == kind


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("", (None, None)),
        ("//", (None, None)),
        ("//folder123", ("folder123", None)),
        ("folder123", ("folder123", None)),
        ("//folder/xyz", ("xyz", None)),
        ("folders/xyz", ("xyz", None)),
        ("//file/abc", (None, "abc")),
        ("file:abc", (None, "abc")),
    ],
)
def test_parse_gdrive_target(payload: str, expected: tuple[str | None, str | None]) -> None:
    assert parse_gdrive_target(payload) == expected


def test_build_sources_merges_consecutive_local_paths() -> None:
    sources = build_sources(
        ["/tmp/a", "/tmp/b", "gdrive://folder1", "https://host/x.mp3", "/tmp/c"],
        SourceOptions(),
    )
    try:
        assert [type(source).__name__ for source in sources] == [
            "LocalSource",
            "DriveSource",
            "HttpSource",
            "LocalSource",
        ]
        local = sources[0]
        assert isinstance(local, LocalSource)
        assert len(local._roots) == 2
    finally:
        close_sources(sources)


def test_build_sources_passes_options_through() -> None:
    options = SourceOptions(
        extensions=frozenset({".m4b"}),
        recursive=False,
        block_size=8192,
        budget_bytes=1234,
        http_timeout=7.5,
        max_retries=1,
    )
    sources = build_sources(["https://host/x.m4b", "gdrive:"], options)
    try:
        http_source = sources[0]
        assert isinstance(http_source, HttpSource)
        assert http_source._block_size == 8192
        assert http_source._budget_bytes == 1234
        assert http_source._timeout == 7.5
        assert http_source._max_retries == 1

        drive_source = sources[1]
        assert isinstance(drive_source, DriveSource)
        assert drive_source._extensions == frozenset({".m4b"})
        assert drive_source._recursive is False
    finally:
        close_sources(sources)


def test_collect_files_respects_limit(tmp_path: Path) -> None:
    for index in range(5):
        write_file(tmp_path, f"{index}.mp3", b"x")
    source = LocalSource(tmp_path)
    assert len(collect_files([source], limit=2)) == 2
    assert len(collect_files([source], limit=None)) == 5


def test_default_options_cap_remote_reads() -> None:
    options = SourceOptions()
    assert options.budget_bytes is not None
    assert options.recursive is True
    assert ".mp3" in options.extensions
