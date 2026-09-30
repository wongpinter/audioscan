# Google Drive library scanning

RuangDengar includes a separate command-line scanner for reading audiobook metadata from Google Drive. It reads tags, chapters, and embedded covers with bounded byte-range requests. It does not download whole audio files for metadata scans.

## Install

Requires Python 3.11+.

```sh
uv sync --extra gdrive
uv run ruangdengar-scan --help
```

For Google Drive OAuth, create an OAuth client of type **Desktop app** and download its client secrets JSON. Then authorize:

```sh
uv run ruangdengar-scan auth --credentials ~/client_secrets.json
uv run ruangdengar-scan auth --status
```

The scanner stores its token under `~/.config/audioscan/token.json` by default. Use `--token PATH` to select another location. Keep client secrets and token files private. The scanner requests read-only Drive access.

## Scan a Drive folder

```sh
uv run ruangdengar-scan scan 'gdrive://FOLDER_ID' --group --stats
```

Scan one file by ID:

```sh
uv run ruangdengar-scan scan 'gdrive://file/FILE_ID' --json report.json
```

Save JSON and CSV reports:

```sh
uv run ruangdengar-scan scan 'gdrive://FOLDER_ID' --json report.json --csv report.csv
```

For headless scans, give a service account read access to the files:

```sh
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/drive-reader.json"
uv run ruangdengar-scan scan 'gdrive://FOLDER_ID' --stats
```

## Other scan targets

The scanner also accepts local folders, local files, and HTTP URLs:

```sh
uv run ruangdengar-scan scan ~/Audiobooks --group
uv run ruangdengar-scan scan https://example.org/book.m4b --stats
uv run ruangdengar-scan inspect book.m4b
```

Use `--json -` or `--csv -` to write a report to standard output. Use `--no-table` to keep standard output suitable for scripts. Run `uv run ruangdengar-scan scan --help`, `inspect --help`, or `auth --help` for options.

## About Drive reads

The scanner uses HTTP byte ranges and a bounded reader to read metadata stored near the start or end of a file, including M4B files with the MP4 `moov` atom at the end. `--stats` reports bytes fetched. Drive files remain unchanged. Google Docs and non-audio files are skipped.

The web player's scan is separate. It indexes the configured folder into the app's SQLite database for browsing and playback. CLI reports do not populate that database.
