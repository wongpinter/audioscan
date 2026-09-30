"""Command line interface: ``ruangdengar-scan scan|inspect|auth``."""

from __future__ import annotations

import argparse
import contextlib
import logging
import re
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import __version__
from .export import (
    render_groups_table,
    render_totals_table,
    render_tracks_table,
    to_csv,
    to_json,
)
from .fmt import format_bitrate, format_bytes, format_duration
from .models import TrackMeta
from .naming import group_tracks
from .probe import cover_bytes, probe
from .reader import DEFAULT_BLOCK_SIZE, FetchError
from .sources import (
    DEFAULT_REMOTE_BUDGET,
    SourceOptions,
    build_sources,
    close_sources,
    collect_files,
)
from .sources.base import RemoteFile, Source
from .sources.gdrive import DriveAuth, DriveError, DriveNotInstalled
from .sources.local import AUDIO_EXTENSIONS

console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2

_HEADER_RE = re.compile(r"^(?P<name>[A-Za-z0-9-]+)\s*:\s*(?P<value>.*)$")
_COVER_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}

EXAMPLES = """\
examples:
  ruangdengar-scan scan ~/Audiobooks
  ruangdengar-scan scan ~/Books --group --json report.json
  ruangdengar-scan scan 'gdrive://1AbCdEfFolderId' --stats
  ruangdengar-scan scan --drive-query "name contains 'Potter'" gdrive: --csv potter.csv
  ruangdengar-scan inspect "Chapter 31; The Battle of Hogwarts.mp3"
  ruangdengar-scan inspect 'gdrive://file/1XyZ...' --cover-out cover.jpg
  ruangdengar-scan auth --credentials ~/client_secrets.json
"""


class CliError(RuntimeError):
    """A usage or configuration problem worth reporting without a traceback."""


@dataclass(slots=True)
class Probed:
    """A probe result together with the source that produced it."""

    meta: TrackMeta
    source: Source
    item: RemoteFile


@dataclass(slots=True)
class CoverSink:
    """Where (and which) cover art should be written during a scan."""

    dest: Path
    index: int | None = None


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="ruangdengar-scan",
        description=(
            "Read tags, chapters and cover art from audio files — including remote "
            "ones — without downloading whole files. Audio metadata lives in a few "
            "kilobytes at the start (and sometimes the end) of a file, so RuangDengar Scan "
            "serves mutagen only the byte ranges it asks for."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ruangdengar-scan {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan",
        help="read metadata for audio files under one or more targets",
        description="Scan every audio file under TARGET and report what was found.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scan.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help="local path, https URL, gdrive:, gdrive://<folder-id>, gdrive://file/<id>",
    )
    scan.add_argument(
        "-j", "--json", metavar="PATH", help="write JSON report to PATH ('-' for stdout)"
    )
    scan.add_argument("--csv", metavar="PATH", help="write CSV report to PATH ('-' for stdout)")
    scan.add_argument("--no-table", action="store_true", help="suppress the terminal tables")
    scan.add_argument(
        "--group",
        action="store_true",
        help="group tracks into books and report missing/duplicate chapters",
    )
    scan.add_argument("--only-errors", action="store_true", help="show only files that failed")
    scan.add_argument(
        "--min-duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="ignore tracks shorter than SECONDS",
    )
    scan.add_argument("--limit", type=int, default=None, metavar="N", help="stop after N files")
    scan.add_argument(
        "--workers", type=int, default=8, metavar="N", help="parallel probes (default: 8)"
    )
    scan.add_argument("--stats", action="store_true", help="print transfer and cache statistics")
    scan.add_argument(
        "--extract-covers",
        metavar="DIR",
        help="write embedded cover art into DIR while scanning",
    )
    scan.add_argument(
        "--cover-index",
        type=int,
        default=None,
        metavar="N",
        help="only extract the Nth cover of each file (default: all)",
    )
    _add_source_options(scan)
    scan.set_defaults(handler=cmd_scan)

    inspect = subparsers.add_parser(
        "inspect",
        help="show every tag, chapter and cover of a single file",
        description="Deep-dive a single track: stream properties, all tags, chapters and covers.",
    )
    inspect.add_argument(
        "target", metavar="TARGET", help="local path, https URL or gdrive://file/<id>"
    )
    inspect.add_argument(
        "--json", metavar="PATH", help="write the JSON report to PATH ('-' for stdout)"
    )
    inspect.add_argument(
        "--cover-out",
        metavar="PATH",
        help="write cover art to PATH (a directory when the file has several covers)",
    )
    inspect.add_argument("--stats", action="store_true", help="print transfer and cache statistics")
    _add_source_options(inspect)
    inspect.set_defaults(handler=cmd_inspect)

    auth = subparsers.add_parser(
        "auth",
        help="manage Google Drive credentials",
        description="Sign in to Google Drive, show credential status, or forget a cached token.",
    )
    auth.add_argument(
        "--credentials", metavar="PATH", help="OAuth client secrets JSON (or service account)"
    )
    auth.add_argument("--token", metavar="PATH", help="where to cache the OAuth token")
    auth.add_argument("--status", action="store_true", help="show which credentials would be used")
    auth.add_argument("--logout", action="store_true", help="delete the cached token")
    auth.set_defaults(handler=cmd_auth)

    return parser


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_argument_group("selection")
    selection.add_argument(
        "--extensions",
        metavar="LIST",
        help="comma separated extensions to consider (default: every common audio type)",
    )
    selection.add_argument(
        "--no-recursive", action="store_true", help="do not descend into sub-directories"
    )

    transfer = parser.add_argument_group("transfer")
    transfer.add_argument(
        "--block-size",
        type=float,
        default=DEFAULT_BLOCK_SIZE / 1024,
        metavar="KiB",
        help=f"cache block size in KiB (default: {DEFAULT_BLOCK_SIZE // 1024})",
    )
    transfer.add_argument(
        "--max-fetch-mb",
        type=float,
        default=DEFAULT_REMOTE_BUDGET / (1024 * 1024),
        metavar="MB",
        help="cap bytes fetched per remote file, 0 for unlimited (default: 64)",
    )
    transfer.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS")
    transfer.add_argument("--retries", type=int, default=3, metavar="N")
    transfer.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="extra HTTP header for remote requests (repeatable)",
    )

    drive = parser.add_argument_group("google drive")
    drive.add_argument("--credentials", metavar="PATH", help="credentials JSON file")
    drive.add_argument("--token", metavar="PATH", help="cached OAuth token path")
    drive.add_argument("--drive-query", metavar="QUERY", help="extra Drive 'q' clause")

    parser.add_argument("-v", "--verbose", action="count", default=0, help="log HTTP activity")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _parse_extensions(text: str | None) -> frozenset[str]:
    if not text:
        return AUDIO_EXTENSIONS
    extensions = set()
    for chunk in text.replace(";", ",").split(","):
        item = chunk.strip().lower()
        if not item:
            continue
        extensions.add(item if item.startswith(".") else f".{item}")
    return frozenset(extensions) or AUDIO_EXTENSIONS


def _parse_headers(pairs: Sequence[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in pairs:
        match = _HEADER_RE.match(raw)
        if not match:
            raise CliError(f"invalid --header {raw!r}; expected NAME:VALUE")
        headers[match.group("name")] = match.group("value")
    return headers


def _source_options(args: argparse.Namespace) -> SourceOptions:
    block_size = int(max(4.0, args.block_size) * 1024)
    budget = None if args.max_fetch_mb <= 0 else int(args.max_fetch_mb * 1024 * 1024)
    return SourceOptions(
        extensions=_parse_extensions(args.extensions),
        recursive=not args.no_recursive,
        block_size=block_size,
        budget_bytes=budget,
        http_headers=_parse_headers(args.header) or None,
        http_timeout=args.timeout,
        gdrive_timeout=max(args.timeout, 60.0),
        max_retries=max(0, args.retries),
        gdrive_credentials=args.credentials,
        gdrive_token=args.token,
        gdrive_query=getattr(args, "drive_query", None),
    )


def _configure_logging(verbosity: int) -> None:
    if verbosity <= 0:
        return
    level = logging.WARNING if verbosity == 1 else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _safe_stem(name: str) -> str:
    stem = Path((name or "cover").replace("\\", "/")).name
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" .")
    return (stem or "cover")[:80]


def _probe_one(
    source: Source,
    item: RemoteFile,
    *,
    cover_sink: CoverSink | None = None,
) -> Probed:
    """Probe a single file; never raises, always returns a ``TrackMeta``."""
    meta = TrackMeta(
        source=source.name,
        id=item.id,
        name=item.name,
        path=item.path or item.name,
        size=item.size,
        mime_type=item.mime_type,
        modified=item.modified,
    )
    try:
        stream = source.open(item)
    except Exception as exc:
        meta.error = f"{type(exc).__name__}: {exc}"
        return Probed(meta=meta, source=source, item=item)

    try:
        streamed_size = getattr(stream, "size", None)
        if meta.size is None and isinstance(streamed_size, int):
            meta.size = streamed_size
        result = probe(
            stream,
            name=item.name,
            path=item.path or item.name,
            source=source.name,
            file_id=item.id,
            size=meta.size,
            mime_type=item.mime_type,
            modified=item.modified,
        )
        stats = getattr(stream, "stats", None)
        if stats is not None:
            result.fetched_bytes = stats.bytes_fetched
            result.fetch_requests = stats.requests
        if cover_sink is not None and result.covers:
            try:
                _write_covers(stream, result, cover_sink)
            except Exception as exc:  # pragma: no cover - filesystem dependent
                err_console.print(
                    f"[yellow]cover extraction failed for {meta.name}: {exc}[/yellow]"
                )
        return Probed(meta=result, source=source, item=item)
    except Exception as exc:
        meta.error = f"{type(exc).__name__}: {exc}"
        return Probed(meta=meta, source=source, item=item)
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def _write_covers(stream: Any, meta: TrackMeta, sink: CoverSink) -> int:
    """Write cover art found in ``meta`` to ``sink.dest``; returns files written."""
    sink.dest.mkdir(parents=True, exist_ok=True)
    indices = range(len(meta.covers)) if sink.index is None else [sink.index]
    written = 0
    for index in indices:
        if index >= len(meta.covers):
            err_console.print(
                f"[yellow]{meta.name}: cover {index} out of range "
                f"({len(meta.covers)} found)[/yellow]"
            )
            continue
        data, mime = cover_bytes(stream, index)
        suffix = _COVER_EXTENSIONS.get((mime or "").lower(), ".bin")
        if len(meta.covers) > 1:
            filename = f"{_safe_stem(meta.name)}.cover{index}{suffix}"
        else:
            filename = f"{_safe_stem(meta.name)}{suffix}"
        target = sink.dest / filename
        target.write_bytes(data)
        written += 1
    return written


def _probe_all(
    items: Sequence[tuple[Source, RemoteFile]],
    *,
    workers: int,
    cover_sink: CoverSink | None = None,
    quiet: bool = False,
) -> list[Probed]:
    """Probe every item, optionally on a thread pool, preserving order."""
    if not items:
        return []
    if workers <= 1 or len(items) == 1:
        results: list[Probed] = []
        for source, item in _progress(items, quiet):
            results.append(_probe_one(source, item, cover_sink=cover_sink))
        return results

    ordered: list[Probed | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_probe_one, source, item, cover_sink=cover_sink): index
            for index, (source, item) in enumerate(items)
        }
        for future in _progress(as_completed(futures), quiet, total=len(futures)):
            index = futures[future]
            ordered[index] = future.result()
    return [entry for entry in ordered if entry is not None]


def _progress(iterable: Any, quiet: bool, total: int | None = None) -> Any:
    """Yield from ``iterable`` behind a Rich progress bar (or bare when quiet).

    Progress goes to stderr so that ``--json -`` still produces clean stdout.
    Note both branches must *yield*: returning the iterable here would make this
    generator produce nothing at all.
    """
    if quiet:
        yield from iterable
        return
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err_console,
    )
    with progress:
        task = progress.add_task("probing", total=total)
        for entry in iterable:
            yield entry
            progress.advance(task)


def _write_output(path: str, text: str) -> None:
    if path == "-":
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        return
    target = Path(path).expanduser()
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(text)
        temp_path.replace(target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    err_console.print(f"wrote {target}")


def _stats_table(tracks: Sequence[TrackMeta]) -> Table:
    requests = sum(track.fetch_requests for track in tracks)
    fetched = sum(track.fetched_bytes for track in tracks)
    total = sum(track.size or 0 for track in tracks)
    table = Table(title="Transfer", show_header=True, header_style="bold", title_justify="left")
    table.add_column("metric", style="dim")
    table.add_column("value", justify="right")
    table.add_row("range requests", str(requests))
    table.add_row("bytes fetched", format_bytes(fetched))
    table.add_row("bytes on disk", format_bytes(total))
    if total:
        table.add_row("downloaded", f"{fetched / total * 100:.2f}% of the files")
    return table


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_scan(args: argparse.Namespace) -> int:
    """Run ``ruangdengar-scan scan``."""
    if not args.targets:
        raise CliError("scan needs at least one TARGET (a path, https URL, or gdrive:)")

    options = _source_options(args)
    cover_sink = (
        CoverSink(dest=Path(args.extract_covers).expanduser(), index=args.cover_index)
        if args.extract_covers
        else None
    )
    sources = build_sources(args.targets, options)
    try:
        with err_console.status("listing files…"):
            items = collect_files(sources, limit=args.limit)
        if not items:
            err_console.print("[yellow]no audio files matched[/yellow]")

        quiet = args.no_table and not args.stats
        probed = _probe_all(
            items, workers=max(1, args.workers), cover_sink=cover_sink, quiet=quiet or not items
        )

        if args.min_duration is not None:
            probed = [p for p in probed if (p.meta.duration or 0.0) >= args.min_duration]
        if args.only_errors:
            probed = [p for p in probed if not p.meta.is_ok]

        tracks = [p.meta for p in probed]
        groups = group_tracks(tracks) if args.group else None

        if args.json:
            _write_output(args.json, to_json(tracks, groups))
            if args.json == "-":
                return _exit_code(tracks)
        if args.csv:
            _write_output(args.csv, to_csv(tracks))
            if args.csv == "-":
                return _exit_code(tracks)

        if not args.no_table:
            console.print(render_totals_table(tracks))
            if tracks:
                console.print(render_tracks_table(tracks))
            if groups:
                console.print(render_groups_table(groups))
            if args.stats:
                console.print(_stats_table(tracks))

        return _exit_code(tracks)
    finally:
        close_sources(sources)


def _exit_code(tracks: Sequence[TrackMeta]) -> int:
    return EXIT_PARTIAL if any(not track.is_ok for track in tracks) else EXIT_OK


def cmd_inspect(args: argparse.Namespace) -> int:
    """Run ``ruangdengar-scan inspect``."""
    options = _source_options(args)
    sources = build_sources([args.target], options)
    try:
        items = collect_files(sources, limit=1)
        if not items:
            raise CliError(f"no audio file found for {args.target!r}")
        source, item = items[0]
        probed = _probe_one(source, item)
        meta = probed.meta

        if args.cover_out and meta.covers:
            _write_requested_cover(source, item, meta, args.cover_out)

        if args.json:
            _write_output(args.json, to_json([meta]))
            if args.json == "-":
                return EXIT_OK if meta.is_ok else EXIT_PARTIAL

        _print_inspect(meta, stats=args.stats)
        return EXIT_OK if meta.is_ok else EXIT_PARTIAL
    finally:
        close_sources(sources)


def _write_requested_cover(
    source: Source, item: RemoteFile, meta: TrackMeta, cover_out: str
) -> None:
    """Honour ``inspect --cover-out``: one file path, or a directory for many."""
    destination = Path(cover_out).expanduser()
    single = len(meta.covers) == 1 and destination.suffix != ""
    if single:
        data, _mime = _read_cover(source, item, 0)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        err_console.print(f"wrote {destination}")
        return

    target_dir = destination if destination.suffix == "" else destination.parent
    stream = source.open(item)
    try:
        _write_covers(stream, meta, CoverSink(dest=target_dir))
    finally:
        stream.close()
    err_console.print(f"wrote cover art under {target_dir}")


def _read_cover(source: Source, item: RemoteFile, index: int) -> tuple[bytes, str | None]:
    """Fetch one cover image's bytes from a freshly opened stream."""
    stream = source.open(item)
    try:
        return cover_bytes(stream, index)
    finally:
        stream.close()


def cmd_auth(args: argparse.Namespace) -> int:
    """Run ``ruangdengar-scan auth``."""
    auth = DriveAuth(
        credentials_path=args.credentials,
        token_path=args.token,
        allow_interactive=True,
    )
    if args.logout:
        removed = auth.logout()
        console.print("removed cached token" if removed else "no cached token to remove")
        return EXIT_OK
    if args.status or not args.credentials:
        _print_auth_status(auth.describe())
        if not args.status and not args.credentials:
            console.print(
                "\nTo sign in:  ruangdengar-scan auth --credentials <client_secrets.json>\n"
                "Or use a service account:  ruangdengar-scan scan --credentials sa.json gdrive://<folder-id>"
            )
        return EXIT_OK

    auth.login(client_secrets=args.credentials)
    console.print(f"[green]signed in[/green] — token cached at {auth.describe()['token_path']}")
    return EXIT_OK


def _print_auth_status(description: dict[str, Any]) -> None:
    table = Table(title="Google Drive credentials", show_header=False, title_justify="left")
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("credentials file", str(description["credentials_path"] or "-"))
    table.add_row("kind", str(description["credential_kind"] or "-"))
    table.add_row("token cache", str(description["token_path"]))
    table.add_row("token present", "yes" if description["token_cached"] else "no")
    if description.get("problem"):
        table.add_row("problem", f"[red]{description['problem']}[/red]")
    console.print(table)


def _print_inspect(meta: TrackMeta, *, stats: bool = False) -> None:
    if not meta.is_ok:
        err_console.print(f"[red]error:[/red] {meta.error}")

    summary = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
    summary.add_column("field", style="dim", no_wrap=True)
    summary.add_column("value")
    summary.add_row("file", meta.name)
    summary.add_row("path", meta.path)
    summary.add_row("source", meta.source)
    summary.add_row("size", format_bytes(meta.size))
    summary.add_row("format", f"{meta.format or '?'} ({meta.mime_type or 'unknown mime'})")
    summary.add_row("codec", meta.codec or "-")
    summary.add_row("duration", f"{format_duration(meta.duration)} ({meta.duration or 0:.2f}s)")
    summary.add_row("bitrate", format_bitrate(meta.bitrate))
    summary.add_row(
        "sample rate",
        f"{meta.sample_rate} Hz" if meta.sample_rate else "-",
    )
    summary.add_row("channels", str(meta.channels) if meta.channels else "-")
    summary.add_row("modified", meta.modified or "-")
    if meta.fetched_bytes:
        share = f" ({meta.fetch_ratio * 100:.2f}% of file)" if meta.fetch_ratio else ""
        summary.add_row(
            "fetched",
            f"{format_bytes(meta.fetched_bytes)} in {meta.fetch_requests} requests{share}",
        )
    console.print(summary)

    tags = Table(title="Tags", show_header=False, title_justify="left")
    tags.add_column("field", style="dim", no_wrap=True)
    tags.add_column("value", overflow="fold")
    for field in (
        "title",
        "artist",
        "album",
        "albumartist",
        "track",
        "disc",
        "genre",
        "date",
        "comment",
    ):
        value = getattr(meta, field, None)
        if value:
            tags.add_row(field, str(value))
    for key, value in sorted(meta.tags.items()):
        tags.add_row(key, value)
    console.print(tags)

    if meta.chapters:
        chapters = Table(
            title=f"Chapters ({len(meta.chapters)})",
            header_style="bold",
            title_justify="left",
        )
        chapters.add_column("#", justify="right")
        chapters.add_column("start", justify="right")
        chapters.add_column("length", justify="right")
        chapters.add_column("title", overflow="fold")
        for chapter in meta.chapters:
            chapters.add_row(
                str(chapter.number),
                format_duration(chapter.start),
                format_duration(chapter.duration),
                chapter.title or "-",
            )
        console.print(chapters)

    if meta.covers:
        covers = Table(
            title=f"Covers ({len(meta.covers)})", header_style="bold", title_justify="left"
        )
        covers.add_column("#", justify="right")
        covers.add_column("mime")
        covers.add_column("description", overflow="fold")
        covers.add_column("size", justify="right")
        for cover in meta.covers:
            covers.add_row(
                str(cover.index),
                cover.mime or "-",
                cover.description or "-",
                format_bytes(cover.size),
            )
        console.print(covers)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch; returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", 0))
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        err_console.print("[yellow]interrupted[/yellow]")
        return 130
    except (CliError, DriveError, DriveNotInstalled, FetchError) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        return EXIT_USAGE
    except FileNotFoundError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        return EXIT_USAGE


__all__ = ["build_parser", "main"]
