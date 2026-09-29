"""Serialise scan results as JSON/CSV and render them for a terminal."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from rich.table import Table
from rich.text import Text

from .fmt import format_bitrate, format_bytes, format_duration, truncate
from .models import TrackMeta
from .naming import BookGroup

CSV_FIELDS = tuple(TrackMeta().summary_row().keys())


def _totals(tracks: Sequence[TrackMeta]) -> dict[str, Any]:
    """Aggregate figures for a result set."""
    return {
        "tracks": len(tracks),
        "bytes": sum(t.size or 0 for t in tracks),
        "fetched_bytes": sum(t.fetched_bytes for t in tracks),
        "duration": round(sum(t.duration or 0.0 for t in tracks), 3),
        "chapters": sum(len(t.chapters) for t in tracks),
        "with_covers": sum(1 for t in tracks if t.has_cover),
        "errors": sum(1 for t in tracks if not t.is_ok),
    }


def to_json(
    tracks: Sequence[TrackMeta],
    groups: Sequence[BookGroup] | None = None,
    *,
    indent: int | None = 2,
) -> str:
    """Render results as a JSON document."""
    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "totals": _totals(tracks),
        "tracks": [track.to_dict() for track in tracks],
    }
    if groups is not None:
        payload["groups"] = [group.to_dict() for group in groups]
    return json.dumps(payload, indent=indent, ensure_ascii=False)


def to_csv(tracks: Sequence[TrackMeta]) -> str:
    """Render a flat CSV table, one row per track."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_FIELDS), extrasaction="ignore")
    writer.writeheader()
    for track in tracks:
        writer.writerow(track.summary_row())
    return buffer.getvalue()


def render_tracks_table(tracks: Sequence[TrackMeta], *, title: str | None = None) -> Table:
    """Build a Rich table summarising every track."""
    show_fetch = any(track.fetched_bytes for track in tracks)
    table = Table(title=title, header_style="bold", title_justify="left", expand=False)
    table.add_column("File", overflow="fold", max_width=44)
    table.add_column("Title", overflow="fold", max_width=32)
    table.add_column("Album", overflow="fold", max_width=24)
    table.add_column("Time", justify="right", no_wrap=True)
    table.add_column("Rate", justify="right", no_wrap=True)
    table.add_column("Ch", justify="right", no_wrap=True)
    table.add_column("Chap", justify="right", no_wrap=True)
    table.add_column("Art", justify="center", no_wrap=True)
    if show_fetch:
        table.add_column("Fetched", justify="right", no_wrap=True)

    for track in tracks:
        if track.error:
            status = Text(f"error: {truncate(track.error, 40)}", style="bold red")
            table.add_row(
                Text(track.name or track.path, style="red"),
                status,
                "",
                "",
                "",
                "",
                "",
                "",
                *([""] if show_fetch else []),
            )
            continue

        row = [
            track.name or track.path,
            track.title or "-",
            track.album or "-",
            format_duration(track.duration),
            format_bitrate(track.bitrate),
            str(track.channels) if track.channels else "-",
            str(len(track.chapters)) if track.chapters else "-",
            "" if not track.has_cover else f"{len(track.covers)}",
        ]
        if show_fetch:
            row.append(_fetch_label(track))
        table.add_row(*row)
    return table


def _fetch_label(track: TrackMeta) -> str:
    ratio = track.fetch_ratio
    label = format_bytes(track.fetched_bytes)
    if ratio is not None:
        label = f"{label} ({ratio * 100:.1f}%)"
    return label


def render_groups_table(groups: Sequence[BookGroup]) -> Table:
    """Build a Rich table of detected books, gaps and duplicates."""
    table = Table(header_style="bold", title="Books", title_justify="left")
    table.add_column("Book", overflow="fold", max_width=44)
    table.add_column("Tracks", justify="right", no_wrap=True)
    table.add_column("Time", justify="right", no_wrap=True)
    table.add_column("Problems", overflow="fold")

    for group in groups:
        problems = group.problems
        table.add_row(
            group.title or group.key,
            str(group.count),
            format_duration(group.total_duration),
            (
                Text("ok", style="green")
                if not problems
                else Text("; ".join(problems), style="yellow")
            ),
        )
    return table


def render_totals_table(tracks: Sequence[TrackMeta]) -> Table:
    """Build a small Rich table of aggregate statistics."""
    totals = _totals(tracks)
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column("metric", style="dim")
    table.add_column("value", justify="right")
    table.add_row("tracks", str(totals["tracks"]))
    table.add_row("total duration", format_duration(totals["duration"]))
    table.add_row("total size", format_bytes(totals["bytes"]))
    if totals["fetched_bytes"]:
        size = totals["bytes"]
        share = f" ({totals['fetched_bytes'] / size * 100:.1f}%)" if size else ""
        table.add_row("fetched", f"{format_bytes(totals['fetched_bytes'])}{share}")
    table.add_row("chapters", str(totals["chapters"]))
    table.add_row("covers", str(totals["with_covers"]))
    if totals["errors"]:
        table.add_row("errors", f"[red]{totals['errors']}[/red]")
    return table


__all__ = [
    "CSV_FIELDS",
    "render_groups_table",
    "render_totals_table",
    "render_tracks_table",
    "to_csv",
    "to_json",
]
