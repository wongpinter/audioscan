"""End-to-end CLI behaviour."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from audioscan.cli import main
from fixtures import JPEG_COVER, build_m4b, build_mp3, write_file


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    write_file(
        root,
        "Deathly Hallows/Chapter 01; One.mp3",
        build_mp3(
            title="One",
            artist="J.K. Rowling",
            album="Deathly Hallows",
            albumartist="J.K. Rowling",
            track="1/36",
            chapters=[(0.0, "Intro"), (1.0, "The Boy Who Lived")],
            cover=JPEG_COVER,
        ),
    )
    write_file(
        root,
        "Deathly Hallows/Chapter 03; Three.mp3",
        build_mp3(title="Three", album="Deathly Hallows", albumartist="J.K. Rowling"),
    )
    write_file(root, "Deathly Hallows/notes.txt", b"ignore me")
    write_file(
        root,
        "Dune/book.m4b",
        build_m4b(
            title="Dune",
            artist="Frank Herbert",
            album="Dune",
            chapters=[(0.0, "Arrakis")],
            duration=3600.0,
            mdat_size=200_000,
        ),
    )
    return root


@pytest.fixture
def library_with_broken_file(library: Path) -> Path:
    write_file(library, "broken.mp3", b"definitely not audio" * 50)
    return library


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def test_scan_json_to_stdout_is_clean(capsys: pytest.CaptureFixture[str], library: Path) -> None:
    """`--json -` must emit only JSON on stdout, even without --no-table."""
    code = main(["scan", str(library), "--json", "-"])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["totals"]["tracks"] == 3
    assert payload["totals"]["errors"] == 0

    names = {track["name"] for track in payload["tracks"]}
    assert names == {"Chapter 01; One.mp3", "Chapter 03; Three.mp3", "book.m4b"}
    assert "notes.txt" not in captured.out

    by_name = {track["name"]: track for track in payload["tracks"]}
    assert by_name["Chapter 01; One.mp3"]["title"] == "One"
    assert by_name["Chapter 01; One.mp3"]["artist"] == "J.K. Rowling"
    assert by_name["Chapter 01; One.mp3"]["chapters"][1]["title"] == "The Boy Who Lived"
    assert by_name["Chapter 01; One.mp3"]["covers"][0]["mime"] == "image/jpeg"
    assert by_name["book.m4b"]["duration"] == 3600.0
    assert by_name["book.m4b"]["chapters"][0]["title"] == "Arrakis"


def test_scan_csv_to_stdout(capsys: pytest.CaptureFixture[str], library: Path) -> None:
    code = main(["scan", str(library), "--csv", "-", "--limit", "1"])
    captured = capsys.readouterr()

    assert code == 0
    rows = list(csv.DictReader(io.StringIO(captured.out)))
    assert len(rows) == 1
    assert rows[0]["name"] == "Chapter 01; One.mp3"
    assert rows[0]["title"] == "One"


def test_scan_writes_report_files(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, library: Path
) -> None:
    json_path = tmp_path / "reports/report.json"
    csv_path = tmp_path / "reports/report.csv"
    code = main(
        ["scan", str(library), "--json", str(json_path), "--csv", str(csv_path), "--no-table"]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert json_path.exists() and csv_path.exists()
    assert "wrote" in captured.err
    assert json.loads(json_path.read_text())["totals"]["tracks"] == 3
    assert len(list(csv.DictReader(io.StringIO(csv_path.read_text())))) == 3


def test_report_write_failure_keeps_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from audioscan import cli

    target = tmp_path / "report.json"
    target.write_text("old report", encoding="utf-8")

    class BrokenWriter:
        name = str(tmp_path / "temporary")

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def write(self, text: str) -> None:
            raise OSError("disk full")

    monkeypatch.setattr(cli.tempfile, "NamedTemporaryFile", lambda **kwargs: BrokenWriter())
    with pytest.raises(OSError, match="disk full"):
        cli._write_output(str(target), "new report")
    assert target.read_text(encoding="utf-8") == "old report"


def test_scan_table_output(
    capsys: pytest.CaptureFixture[str], library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rich folds long cells in an 80-column non-tty console; give it room so the
    # assertions below are about content, not width.
    monkeypatch.setenv("COLUMNS", "200")
    code = main(["scan", str(library)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Deathly Hallows" in out
    assert "Dune" in out
    assert "Chapter 01; One.mp3" in out


def test_scan_group_reports_missing_chapters(
    capsys: pytest.CaptureFixture[str], library: Path
) -> None:
    code = main(["scan", str(library), "--group", "--json", "-"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    groups = {group["title"]: group for group in payload["groups"]}
    assert groups["Deathly Hallows"]["missing"] == [2]
    assert groups["Deathly Hallows"]["count"] == 2
    assert groups["Dune"]["count"] == 1


def test_scan_group_table(capsys: pytest.CaptureFixture[str], library: Path) -> None:
    code = main(["scan", str(library), "--group"])
    out = capsys.readouterr().out
    assert code == 0
    assert "missing 2" in out


def test_scan_limit(capsys: pytest.CaptureFixture[str], library: Path) -> None:
    main(["scan", str(library), "--limit", "1", "--json", "-"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["tracks"] == 1


def test_scan_min_duration_filters_short_files(
    capsys: pytest.CaptureFixture[str], library: Path
) -> None:
    main(["scan", str(library), "--min-duration", "60", "--json", "-"])
    payload = json.loads(capsys.readouterr().out)
    assert [track["name"] for track in payload["tracks"]] == ["book.m4b"]


def test_scan_extracts_covers(tmp_path: Path, library: Path) -> None:
    covers = tmp_path / "covers"
    code = main(["scan", str(library), "--extract-covers", str(covers), "--no-table"])

    assert code == 0
    written = sorted(path.name for path in covers.iterdir())
    # Unsafe characters are folded into underscores for the output file name.
    assert written == ["Chapter 01_ One.mp3.jpg"]
    assert (covers / written[0]).read_bytes() == JPEG_COVER


def test_scan_stats_reports_traffic(capsys: pytest.CaptureFixture[str], library: Path) -> None:
    code = main(["scan", str(library), "--stats", "--limit", "3"])
    out = capsys.readouterr().out
    assert code == 0
    assert "range requests" in out


def test_scan_reports_errors_and_exits_1(
    capsys: pytest.CaptureFixture[str], library_with_broken_file: Path
) -> None:
    code = main(["scan", str(library_with_broken_file), "--json", "-"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["totals"]["errors"] == 1
    broken = next(track for track in payload["tracks"] if track["name"] == "broken.mp3")
    assert broken["error"]


def test_scan_only_errors(
    capsys: pytest.CaptureFixture[str], library_with_broken_file: Path
) -> None:
    code = main(["scan", str(library_with_broken_file), "--only-errors", "--json", "-"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert [track["name"] for track in payload["tracks"]] == ["broken.mp3"]


def test_scan_without_targets_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan"]) == 2
    assert "needs at least one TARGET" in capsys.readouterr().err


def test_scan_missing_path_exits_2(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["scan", str(tmp_path / "nope")]) == 2
    assert "no such file or directory" in capsys.readouterr().err.lower()


def test_scan_empty_directory_reports_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["scan", str(empty), "--json", "-"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["tracks"] == []
    assert "no audio files matched" in captured.err


def test_scan_rejects_bad_header(capsys: pytest.CaptureFixture[str], library: Path) -> None:
    assert main(["scan", str(library), "--header", "no-colon"]) == 2
    assert "invalid --header" in capsys.readouterr().err


def test_scan_custom_extensions(capsys: pytest.CaptureFixture[str], library: Path) -> None:
    main(["scan", str(library), "--extensions", "m4b", "--json", "-"])
    payload = json.loads(capsys.readouterr().out)
    assert [track["name"] for track in payload["tracks"]] == ["book.m4b"]


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #
def test_inspect_json(capsys: pytest.CaptureFixture[str], library: Path) -> None:
    target = library / "Deathly Hallows/Chapter 01; One.mp3"
    code = main(["inspect", str(target), "--json", "-"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["tracks"][0]["title"] == "One"
    assert payload["tracks"][0]["chapters"][0]["title"] == "Intro"


def test_inspect_renders_details(capsys: pytest.CaptureFixture[str], library: Path) -> None:
    target = library / "Deathly Hallows/Chapter 01; One.mp3"
    code = main(["inspect", str(target), "--stats"])
    out = capsys.readouterr().out

    assert code == 0
    assert "The Boy Who Lived" in out
    assert "MPEG 1 Layer 3" in out
    assert "image/jpeg" in out


def test_inspect_extracts_cover_to_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, library: Path
) -> None:
    target = library / "Deathly Hallows/Chapter 01; One.mp3"
    destination = tmp_path / "cover.jpg"
    assert main(["inspect", str(target), "--cover-out", str(destination)]) == 0
    assert destination.read_bytes() == JPEG_COVER
    assert "wrote" in capsys.readouterr().err


def test_inspect_extracts_cover_to_directory(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, library: Path
) -> None:
    target = library / "Deathly Hallows/Chapter 01; One.mp3"
    directory = tmp_path / "covers-out"
    assert main(["inspect", str(target), "--cover-out", str(directory)]) == 0
    assert [path.name for path in directory.iterdir()] == ["Chapter 01_ One.mp3.jpg"]


def test_inspect_errors_on_unreadable_file(
    capsys: pytest.CaptureFixture[str], library_with_broken_file: Path
) -> None:
    target = library_with_broken_file / "broken.mp3"
    assert main(["inspect", str(target)]) == 1
    assert "error" in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- #
# auth and top level
# --------------------------------------------------------------------------- #
def test_auth_status(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    token = tmp_path / "token.json"
    assert main(["auth", "--status", "--token", str(token)]) == 0
    out = capsys.readouterr().out
    assert "token cache" in out
    assert str(token) in out


def test_auth_without_credentials_explains_how_to_sign_in(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["auth", "--token", str(tmp_path / "token.json")]) == 0
    assert "To sign in" in capsys.readouterr().out


def test_auth_logout(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    assert main(["auth", "--logout", "--token", str(token)]) == 0
    assert not token.exists()
    assert "removed cached token" in capsys.readouterr().out


def test_version_exits_cleanly() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_help_exits_cleanly() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0


def test_scan_no_table_still_returns_every_track(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, library: Path
) -> None:
    """Regression: --no-table once returned zero results.

    The progress helper is a generator, so an early `return iterable` in quiet
    mode silently yielded nothing and every scan with --no-table came back empty.
    """
    report = tmp_path / "report.json"
    assert main(["scan", str(library), "--no-table", "--json", str(report)]) == 0
    captured = capsys.readouterr()

    assert json.loads(report.read_text())["totals"]["tracks"] == 3
    assert "Chapter 01" not in captured.out, "--no-table must not print a table"


def test_scan_reports_an_unreachable_endpoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dead remote endpoint is reported per file, not as a crash."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    code = main(["scan", f"http://127.0.0.1:{free_port}/book.mp3", "--json", "-"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["totals"]["errors"] == 1
    assert payload["tracks"][0]["error"]


def test_module_entry_point_runs() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "audioscan", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert "ruangdengar-scan" in result.stdout


def test_unknown_command_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code != 0
