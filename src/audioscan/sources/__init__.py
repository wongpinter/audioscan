"""Turn CLI targets into :class:`~audioscan.sources.base.Source` objects.

Target syntax accepted by the CLI:

===============================  =========================================
``~/Books``                      local file or directory
``https://host/book.m4b``        single remote file over HTTP(S)
``gdrive:``                      every audio file in My Drive
``gdrive://<folder-id>``         one Drive folder (recursive by default)
``gdrive://file/<file-id>``      a single Drive file
===============================  =========================================
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..reader import DEFAULT_BLOCK_SIZE
from .base import AuthProvider, RemoteFile, Source
from .gdrive import DriveAuth, DriveSource
from .http import HttpSource
from .local import AUDIO_EXTENSIONS, LocalSource, looks_like_audio

#: Remote reads are capped by default so a malformed file cannot turn a metadata
#: scan into a full download. 0 disables the cap.
DEFAULT_REMOTE_BUDGET = 64 * 1024 * 1024


@dataclass(slots=True)
class SourceOptions:
    """Settings shared by every source built from CLI arguments."""

    extensions: frozenset[str] = AUDIO_EXTENSIONS
    recursive: bool = True
    block_size: int = DEFAULT_BLOCK_SIZE
    budget_bytes: int | None = DEFAULT_REMOTE_BUDGET
    http_headers: Mapping[str, str] | None = None
    http_timeout: float = 30.0
    gdrive_timeout: float = 60.0
    max_retries: int = 3
    gdrive_credentials: str | None = None
    gdrive_token: str | None = None
    gdrive_query: str | None = None
    allow_interactive: bool = False
    client: httpx.Client | None = None

    def new_client(self) -> httpx.Client:
        """Create a shared HTTP client (redirects on, generous pool)."""
        return httpx.Client(follow_redirects=True, limits=httpx.Limits(max_connections=16))


def parse_gdrive_target(payload: str) -> tuple[str | None, str | None]:
    """Split a ``gdrive:`` payload into ``(folder_id, file_id)``."""
    text = payload.strip().strip("/")
    if not text:
        return (None, None)
    lowered = text.lower()
    for prefix in ("file:", "file/"):
        if lowered.startswith(prefix):
            return (None, text[len(prefix) :])
    for prefix in ("folder:", "folder/", "folders/"):
        if lowered.startswith(prefix):
            return (text[len(prefix) :], None)
    return (text, None)


def classify_target(target: str) -> str:
    """Return ``local``, ``http`` or ``gdrive`` for a CLI target."""
    lowered = target.lower()
    if lowered.startswith(("http://", "https://")):
        return "http"
    if lowered.startswith("gdrive:"):
        return "gdrive"
    return "local"


def build_sources(
    targets: Sequence[str],
    options: SourceOptions | None = None,
) -> list[Source]:
    """Build one source per target, merging consecutive local paths."""
    options = options or SourceOptions()
    sources: list[Source] = []
    local_paths: list[Path] = []

    def flush_local() -> None:
        if local_paths:
            sources.append(
                LocalSource(
                    list(local_paths),
                    extensions=options.extensions,
                    recursive=options.recursive,
                )
            )
            local_paths.clear()

    for target in targets:
        kind = classify_target(target)
        if kind == "local":
            local_paths.append(Path(target).expanduser())
            continue

        flush_local()
        if kind == "http":
            sources.append(
                HttpSource(
                    target,
                    headers=options.http_headers,
                    client=options.client,
                    timeout=options.http_timeout,
                    block_size=options.block_size,
                    budget_bytes=options.budget_bytes,
                    max_retries=options.max_retries,
                )
            )
            continue

        folder_id, file_id = parse_gdrive_target(target.split(":", 1)[1])
        auth = DriveAuth(
            credentials_path=options.gdrive_credentials,
            token_path=options.gdrive_token,
            allow_interactive=options.allow_interactive,
        )
        sources.append(
            DriveSource(
                folder_id=folder_id,
                file_id=file_id,
                query=options.gdrive_query,
                auth=auth,
                client=options.client,
                extensions=options.extensions,
                recursive=options.recursive,
                timeout=options.gdrive_timeout,
                block_size=options.block_size,
                budget_bytes=options.budget_bytes,
                max_retries=options.max_retries,
            )
        )

    flush_local()
    return sources


def close_sources(sources: Sequence[Source]) -> None:
    """Close every source, ignoring individual failures."""
    for source in sources:
        try:
            source.close()
        except Exception:  # pragma: no cover - best effort cleanup
            continue


def collect_files(
    sources: Sequence[Source],
    *,
    limit: int | None = None,
) -> list[tuple[Source, RemoteFile]]:
    """Flatten every source into ``(source, file)`` pairs."""
    items: list[tuple[Source, RemoteFile]] = []
    for source in sources:
        for item in source.iter_files():
            items.append((source, item))
            if limit is not None and len(items) >= limit:
                return items
    return items


__all__ = [
    "DEFAULT_REMOTE_BUDGET",
    "AuthProvider",
    "RemoteFile",
    "Source",
    "SourceOptions",
    "build_sources",
    "classify_target",
    "close_sources",
    "collect_files",
    "looks_like_audio",
    "parse_gdrive_target",
]
