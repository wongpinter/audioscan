# audioscan

`audioscan` reads audio and audiobook metadata from local files, HTTP URLs, and Google Drive. It reports tags, duration, chapters, cover art, and transfer stats. For remote files, it uses HTTP byte ranges so it can read metadata without downloading each full file.

## Install

Requires Python 3.11 or newer.

Install from this source checkout with `uv`:

```bash
uv sync --extra gdrive
uv run audioscan --help
```

Install with `pip`:

```bash
python -m pip install ".[gdrive]"
audioscan --help
```

The `gdrive` extra installs Google authentication support. For local files and HTTP URLs, install without the extra:

```bash
python -m pip install .
```

## Quick start

Scan a local folder:

```bash
audioscan scan ~/Audiobooks
```

Group tracks into likely books and report missing or duplicate chapter numbers:

```bash
audioscan scan ~/Audiobooks --group
```

Save JSON and CSV reports:

```bash
audioscan scan ~/Audiobooks --json report.json --csv report.csv
```

Scan a single remote file and show transfer statistics:

```bash
audioscan scan https://example.org/book.m4b --stats
```

Inspect one file's tags, chapters, and covers:

```bash
audioscan inspect "Chapter 31; The Battle of Hogwarts.mp3"
```

Extract covers while scanning:

```bash
audioscan scan ~/Audiobooks --extract-covers covers/
```

Write one file's cover art:

```bash
audioscan inspect book.m4b --cover-out cover.jpg
```

Run `audioscan --help`, `audioscan scan --help`, or `audioscan inspect --help` for all options.

## Targets

`scan` accepts one or more targets:

| Target | What it scans |
| --- | --- |
| `~/Audiobooks` | A local folder, recursively by default |
| `~/Audiobooks/book.m4b` | One local audio file |
| `https://host/book.m4b` | One HTTP or HTTPS file |
| `gdrive:` | Audio files in Google Drive |
| `gdrive://FOLDER_ID` | Files in one Drive folder, recursively by default |
| `gdrive://file/FILE_ID` | One Drive file |

Local scans recognize `.aac`, `.aif`, `.aiff`, `.ape`, `.dff`, `.dsf`, `.flac`, `.m4a`, `.m4b`, `.m4p`, `.mka`, `.mp2`, `.mp3`, `.mp4`, `.mpc`, `.oga`, `.ogg`, `.opus`, `.spx`, `.tta`, `.wav`, `.wave`, and `.wma`. Set a custom list with `--extensions`, for example `--extensions mp3,m4b,flac`.

Files with partial-download suffixes such as `.part` or `.crdownload` appear in scan results and are marked as partial.

## Google Drive

Install the `gdrive` extra first. For browser-based OAuth, create an OAuth client of type **Desktop app**, download its client secrets JSON, then run:

```bash
audioscan auth --credentials ~/client_secrets.json
audioscan auth --status
audioscan scan 'gdrive://FOLDER_ID' --group --stats
```

The OAuth flow opens a browser and saves a token under `~/.config/audioscan/token.json` by default. Use `--token PATH` to select another token location. Remove the cached token with:

```bash
audioscan auth --logout
```

For unattended scans, use a service account with read access to the target files:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/drive-reader.json"
audioscan scan 'gdrive://FOLDER_ID' --stats
```

You can also pass credentials per command with `--credentials PATH`. Keep credential and token files private. `audioscan` requests read-only Drive access.

To add a Drive search clause:

```bash
audioscan scan gdrive: --drive-query "name contains 'Potter'" --json potter.json
```

## Reports and options

`scan` prints a terminal table by default. Use `--json PATH` or `--csv PATH` to save reports. Use `-` as the path to write a report to standard output:

```bash
audioscan scan ~/Audiobooks --json - > report.json
audioscan scan ~/Audiobooks --csv - > report.csv
```

Use `--no-table` to suppress the terminal tables. Progress and status messages go to standard error, so standard output stays suitable for JSON or CSV.

Useful `scan` options:

| Option | Purpose |
| --- | --- |
| `--group` | Group tracks by book and show chapter gaps or duplicates |
| `--only-errors` | Show only tracks that failed to parse |
| `--min-duration SECONDS` | Skip shorter tracks |
| `--limit N` | Scan at most N files |
| `--workers N` | Set parallel probes; default is 8 |
| `--stats` | Show bytes fetched and cache statistics |
| `--extract-covers DIR` | Save cover images from scanned files |
| `--cover-index N` | Save only cover N from each file; default saves all covers |
| `--no-recursive` | Scan only the target folder's direct files |
| `--block-size KiB` | Set remote-reader cache block size; default is 256 KiB |
| `--max-fetch-mb MB` | Cap remote bytes read per file; default is 64 MB; `0` removes the cap |
| `--timeout SECONDS` | Set the HTTP timeout |
| `--retries N` | Set retry count for transient HTTP and Drive errors |
| `--header NAME:VALUE` | Add an HTTP header; repeat this option for more headers |

`inspect` accepts `--json PATH`, `--cover-out PATH`, and `--stats`. `auth` accepts `--credentials PATH`, `--token PATH`, `--status`, and `--logout`.

JSON reports contain `generated_at`, aggregate `totals`, and a `tracks` list. The totals include track count, total file size, fetched bytes, duration, chapter count, cover count, and error count. `--group` also adds a `groups` list. CSV contains one summary row per track.

Exit codes: `0` means all scanned files succeeded, `1` means at least one file failed, and `2` means a command or configuration error occurred.

## Remote reads and limits

`audioscan` uses a seekable reader with a bounded block cache. It can read file metadata stored near the start or end, including M4B files whose MP4 `moov` atom sits at the end. `--stats` shows the bytes fetched for remote files.

HTTP servers must support byte-range requests when a file needs seeks beyond the first cache block. A file that fits in one block can still be scanned with a server that ignores ranges. The remote-read budget prevents a metadata probe from downloading an unexpectedly large amount of data; set `--max-fetch-mb 0` only when you accept unlimited reads.

Chapter support includes ID3 `CHAP`/`CTOC`, MP4 `chpl`, and Vorbis chapter comments. QuickTime chapter tracks are not parsed. Google Docs and other non-audio Drive files are skipped. The app reads files only; it does not edit audio or Drive content.

## Python API

```python
from audioscan import probe
from audioscan.reader import SeekableBlockReader
from audioscan.sources.http import HttpRangeFetcher

reader = SeekableBlockReader(HttpRangeFetcher.open("https://example.org/book.m4b"))
try:
    track = probe(reader, name="book.m4b", source="http")
    print(track.title, track.duration, len(track.chapters))
    print(reader.stats.snapshot())
finally:
    reader.close()
```

## Development

```bash
uv sync --extra dev --extra gdrive
uv run pytest
uv run ruff check .
uv run mypy src
```
