"""File-name parsing and book grouping, using names from a real library."""

from __future__ import annotations

import pytest

from audioscan.models import TrackMeta
from audioscan.naming import (
    find_duplicates,
    find_gaps,
    group_tracks,
    normalize_key,
    parse_name,
    split_extension,
)
from fixtures import REAL_NAMES


@pytest.mark.parametrize(
    ("name", "number", "title", "confidence"),
    [
        ("Chapter 31; The Battle of Hogwarts.mp3", 31, "The Battle of Hogwarts", "high"),
        ("Chapter 06; Gilderoy Lockhart.mp3", 6, "Gilderoy Lockhart", "high"),
        ("Chapter 12 - The Wandmaker.mp3", 12, "The Wandmaker", "high"),
        ("01 - Opening Credits.mp3", 1, "Opening Credits", "medium"),
        ("07. The Forest Again.mp3", 7, "The Forest Again", "medium"),
        ("Track 4 - The Wanderer.m4b", 4, "The Wanderer", "medium"),
        ("Dune - 12 - Muad'Dib.flac", 12, "Muad'Dib", "high"),
        ("Some Random Book.m4b", None, "Some Random Book", "low"),
    ],
)
def test_parse_name_patterns(name: str, number: int | None, title: str, confidence: str) -> None:
    parsed = parse_name(name)
    assert parsed.number == number
    assert parsed.title == title
    assert parsed.confidence == confidence


def test_parse_name_handles_interrupted_download() -> None:
    parsed = parse_name("THE CALL OF THE OLD ONES; 35 Cthulhu Mythos Stories (2.mp3.part")
    assert parsed.is_partial
    assert parsed.extension == "mp3"
    assert parsed.revision == 2
    assert parsed.number == 35
    assert parsed.title == "Cthulhu Mythos Stories"
    assert parsed.book == "THE CALL OF THE OLD ONES"


def test_split_extension_variants() -> None:
    assert split_extension("book.mp3") == ("book", "mp3", False)
    assert split_extension("book.M4B") == ("book", "m4b", False)
    assert split_extension("book.mp3.crdownload") == ("book", "mp3", True)
    assert split_extension("no-extension") == ("no-extension", "", False)
    assert split_extension("") == ("", "", False)


def test_parse_name_of_extension_only_name() -> None:
    parsed = parse_name(".mp3")
    assert parsed.number is None
    assert parsed.title is None


def test_normalize_key_folds_case_and_punctuation() -> None:
    assert normalize_key("Harry Potter 7: Deathly Hallows!") == "harry potter 7 deathly hallows"
    assert normalize_key("") == ""


@pytest.mark.parametrize(
    ("numbers", "expected"),
    [
        ([1, 2, 3], []),
        ([1, 2, 4], [3]),
        ([1, 2, 3, 7, 8, 10], [4, 5, 6, 9]),
        ([5], []),
        ([], []),
        ([1, 4000], []),  # implausible span is ignored rather than reported
    ],
)
def test_find_gaps(numbers: list[int], expected: list[int]) -> None:
    assert find_gaps(numbers) == expected


def test_find_duplicates() -> None:
    assert find_duplicates([1, 2, 2, 3, 3, 3]) == [2, 3]
    assert find_duplicates([None, 1]) == []


def test_group_tracks_by_album_tag() -> None:
    tracks = [
        TrackMeta(name="Chapter 02; Two.mp3", album="Deathly Hallows", albumartist="J.K. Rowling"),
        TrackMeta(name="Chapter 01; One.mp3", album="Deathly Hallows", albumartist="J.K. Rowling"),
        TrackMeta(
            name="Chapter 03; Three.mp3", album="Deathly Hallows", albumartist="J.K. Rowling"
        ),
    ]
    groups = group_tracks(tracks)
    assert len(groups) == 1
    group = groups[0]
    assert group.title == "Deathly Hallows"
    assert group.count == 3
    assert [t.number for t in group.tracks] == [1, 2, 3]
    assert group.missing == []
    assert group.problems == []


def test_group_tracks_reports_missing_and_duplicate_chapters() -> None:
    tracks = [
        TrackMeta(name="Chapter 01; One.mp3", album="Book"),
        TrackMeta(name="Chapter 02; Two.mp3", album="Book"),
        TrackMeta(name="Chapter 02; Two again.mp3", album="Book"),
        TrackMeta(name="Chapter 05; Five.mp3", album="Book"),
    ]
    group = group_tracks(tracks)[0]
    assert group.missing == [3, 4]
    assert group.duplicates == [2]
    problems = "; ".join(group.problems)
    assert "missing 3-4" in problems
    assert "duplicate 2" in problems


def test_group_tracks_falls_back_to_folder_when_untagged() -> None:
    tracks = [
        TrackMeta(name="Chapter 01; One.mp3", path="Dune/Chapter 01; One.mp3"),
        TrackMeta(name="Chapter 02; Two.mp3", path="Dune/Chapter 02; Two.mp3"),
        TrackMeta(name="Chapter 01; Other.mp3", path="Neuromancer/Chapter 01; Other.mp3"),
    ]
    groups = group_tracks(tracks)
    assert {g.title for g in groups} == {"Dune", "Neuromancer"}
    dune = next(g for g in groups if g.title == "Dune")
    assert dune.count == 2


def test_group_tracks_handles_unnumbered_and_partial_files() -> None:
    tracks = [
        TrackMeta(name="Chapter 01; One.mp3", album="Book"),
        TrackMeta(name="Chapter 02; Two.mp3", album="Book"),
        TrackMeta(name="Cover Art Notes.txt", album="Book"),
        TrackMeta(name="THE CALL OF THE OLD ONES; 35 Stories (2.mp3.part", album="Book"),
    ]
    group = group_tracks(tracks)[0]
    assert group.count == 4
    assert group.unnumbered == 1
    problems = "; ".join(group.problems)
    assert "1 unnumbered" in problems
    assert "1 partial download" in problems
    # Numbered tracks come first, unnumbered last.
    assert group.tracks[-1].number is None


def test_group_tracks_counts_errored_tracks_as_problems() -> None:
    tracks = [
        TrackMeta(name="Chapter 01; One.mp3", album="Book"),
        TrackMeta(name="Chapter 02; Two.mp3", album="Book", error="MutagenError: boom"),
    ]
    group = group_tracks(tracks)[0]
    assert any("unreadable" in problem for problem in group.problems)


def test_group_tracks_total_duration_and_serialisation() -> None:
    tracks = [
        TrackMeta(name="Chapter 01; One.mp3", album="Book", duration=60.0),
        TrackMeta(name="Chapter 02; Two.mp3", album="Book", duration=90.5),
    ]
    group = group_tracks(tracks)[0]
    assert group.total_duration == pytest.approx(150.5)
    payload = group.to_dict()
    assert payload["count"] == 2
    assert payload["title"] == "Book"
    assert len(payload["tracks"]) == 2


def test_parse_all_real_names_without_error() -> None:
    for name in REAL_NAMES:
        parsed = parse_name(name)
        assert parsed.raw == name
        assert parsed.title is not None or parsed.number is None
