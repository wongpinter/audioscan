"""Serialisation and Rich table rendering."""

from __future__ import annotations

import csv
import io
import json

from audioscan.export import (
    CSV_FIELDS,
    render_groups_table,
    render_totals_table,
    render_tracks_table,
    to_csv,
    to_json,
)
from audioscan.models import Chapter, Cover, TrackMeta
from audioscan.naming import group_tracks


def sample_tracks() -> list[TrackMeta]:
    good = TrackMeta(
        source="gdrive",
        id="file-1",
        name="Chapter 01; One.mp3",
        path="Potter/Chapter 01; One.mp3",
        title="One",
        artist="J.K. Rowling",
        album="Deathly Hallows",
        duration=3725.25,
        bitrate=128000,
        sample_rate=44100,
        channels=2,
        format="MP3",
        codec="MPEG 1 Layer 3",
        size=5_000_000,
        fetched_bytes=65_536,
        fetch_requests=2,
        chapters=[Chapter(number=1, start=0.0, end=60.0, title="Intro")],
        covers=[Cover(index=0, mime="image/jpeg", size=1024)],
    )
    bad = TrackMeta(name="broken.mp3", error="MutagenError: no header")
    return [good, bad]


def test_to_json_shape_and_totals() -> None:
    payload = json.loads(to_json(sample_tracks(), group_tracks(sample_tracks())))
    assert payload["totals"]["tracks"] == 2
    assert payload["totals"]["errors"] == 1
    assert payload["totals"]["chapters"] == 1
    assert payload["totals"]["with_covers"] == 1
    assert payload["totals"]["fetched_bytes"] == 65_536
    assert payload["tracks"][0]["title"] == "One"
    assert payload["tracks"][0]["duration_hms"] == "1:02:05"
    assert payload["tracks"][0]["chapters"][0]["title"] == "Intro"
    assert payload["tracks"][1]["error"].startswith("MutagenError")
    groups = {group["title"]: group for group in payload["groups"]}
    assert groups["Deathly Hallows"]["count"] == 1
    assert groups["broken.mp3"]["count"] == 1
    assert payload["generated_at"]


def test_to_json_omits_groups_when_not_requested() -> None:
    payload = json.loads(to_json(sample_tracks()))
    assert "groups" not in payload


def test_to_csv_round_trips() -> None:
    text = to_csv(sample_tracks())
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 2
    assert set(rows[0]) == set(CSV_FIELDS)
    assert rows[0]["title"] == "One"
    assert rows[0]["duration"] == "3725.25"
    assert rows[0]["duration_hms"] == "1:02:05"
    assert rows[0]["fetched_bytes"] == "65536"
    assert rows[0]["chapters"] == "1"
    assert rows[1]["error"].startswith("MutagenError")


def test_render_tracks_table_has_a_row_per_track() -> None:
    tracks = sample_tracks()
    table = render_tracks_table(tracks)
    assert table.row_count == 2
    headers = [column.header for column in table.columns]
    assert "Fetched" in headers  # shown because one track was fetched remotely


def test_render_tracks_table_hides_fetch_column_for_local_files() -> None:
    table = render_tracks_table([TrackMeta(name="local.mp3", title="Local")])
    headers = [column.header for column in table.columns]
    assert "Fetched" not in headers


def test_render_groups_table_lists_problems() -> None:
    tracks = [
        TrackMeta(name="Chapter 01; One.mp3", album="Book"),
        TrackMeta(name="Chapter 03; Three.mp3", album="Book"),
    ]
    table = render_groups_table(group_tracks(tracks))
    assert table.row_count == 1


def test_render_totals_table_handles_empty_input() -> None:
    table = render_totals_table([])
    assert table.row_count >= 1
