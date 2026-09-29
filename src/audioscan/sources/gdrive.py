"""Google Drive source.

``files/{id}?alt=media`` honours HTTP ``Range`` headers, so the same
:class:`~audioscan.reader.SeekableBlockReader` used for plain HTTP works here
unchanged — probing a 400 MB audiobook costs a couple of megabytes of quota
instead of the whole file.

Credential resolution order:

1. an explicit ``--credentials`` file (service account, authorised user, or
   OAuth *client secrets*),
2. ``GOOGLE_APPLICATION_CREDENTIALS``,
3. a cached token from ``audioscan auth`` (``~/.config/audioscan/token.json``),
4. Application Default Credentials.

``google-auth`` is an optional dependency, so every import from it is lazy and
fails with an actionable message when the ``gdrive`` extra is missing.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from ..models import SOURCE_GDRIVE
from ..reader import DEFAULT_BLOCK_SIZE, SeekableBlockReader
from .base import ReadableStream, RemoteFile
from .http import RETRY_STATUS, HttpRangeFetcher
from .local import AUDIO_EXTENSIONS, looks_like_audio

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_FIELDS = "nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum,parents)"
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


class DriveError(RuntimeError):
    """Raised for configuration and API problems talking to Google Drive."""


class DriveNotInstalled(DriveError):
    """Raised when the optional Google libraries are unavailable."""


def config_dir() -> Path:
    """Directory holding audioscan's cached credentials."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "audioscan"


def default_token_path() -> Path:
    """Default location of the cached OAuth token."""
    return config_dir() / "token.json"


def _require_google_auth() -> None:
    try:
        import google.auth  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DriveNotInstalled(
            "Google Drive support requires the optional extra: pip install 'audioscan[gdrive]'"
        ) from exc


def _request_adapter() -> Any:
    """Build a google-auth transport ``Request`` used to refresh tokens.

    Use google-auth's requests transport. It supports HTTPS, which the stdlib
    ``http.client`` adapter does not support.
    """
    _require_google_auth()
    try:
        from google.auth.transport.requests import Request

        return Request()
    except ImportError as exc:  # pragma: no cover - depends on optional packages
        raise DriveNotInstalled(
            "token refresh requires the requests transport; install 'audioscan[gdrive]'"
        ) from exc


def credential_kind(path: Path) -> str:
    """Classify a JSON credential file by its shape."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DriveError(f"cannot read credential file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DriveError(f"unrecognised credential file {path}: expected a JSON object")
    kind = data.get("type")
    if kind == "service_account":
        return "service_account"
    if kind == "authorized_user":
        return "authorized_user"
    if "installed" in data or "web" in data:
        return "client_secrets"
    raise DriveError(f"unrecognised credential file {path} (type={kind!r})")


class DriveAuth:
    """Resolve, cache and refresh Google credentials for Drive read-only access."""

    def __init__(
        self,
        *,
        credentials_path: str | os.PathLike[str] | None = None,
        token_path: str | os.PathLike[str] | None = None,
        scopes: tuple[str, ...] = (DRIVE_READONLY_SCOPE,),
        allow_interactive: bool = False,
    ) -> None:
        self._credentials_path = Path(credentials_path).expanduser() if credentials_path else None
        self._token_path = Path(token_path).expanduser() if token_path else default_token_path()
        self._scopes = tuple(scopes)
        self._allow_interactive = allow_interactive
        self._credentials: Any = None
        self._lock = threading.Lock()
        self._description = "unresolved"

    # -- credential discovery ------------------------------------------------
    def _resolve_credentials_path(self) -> Path | None:
        for candidate in (self._credentials_path, os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")):
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            if not path.exists():
                raise DriveError(f"credential file not found: {path}")
            return path
        return None

    def _load(self) -> Any:
        path = self._resolve_credentials_path()
        if path is not None:
            kind = credential_kind(path)
            if kind == "service_account":
                self._description = f"service account ({path})"
                return self._load_service_account(path)
            if kind == "authorized_user":
                self._description = f"authorised user ({path})"
                return self._load_authorized_user(path)
            self._description = "interactive OAuth"
            return self._interactive_login(path)

        if self._token_path.exists():
            self._description = f"cached token ({self._token_path})"
            return self._load_authorized_user(self._token_path)

        self._description = "application default credentials"
        return self._load_adc()

    def _load_service_account(self, path: Path) -> Any:
        _require_google_auth()
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            str(path), scopes=list(self._scopes)
        )

    def _load_authorized_user(self, path: Path) -> Any:
        _require_google_auth()
        from google.oauth2.credentials import Credentials

        return Credentials.from_authorized_user_file(str(path), scopes=list(self._scopes))

    def _load_adc(self) -> Any:
        _require_google_auth()
        import google.auth

        try:
            credentials, _project = google.auth.default(scopes=list(self._scopes))
        except Exception as exc:
            raise DriveError(
                "no Google credentials found. Run `audioscan auth --credentials "
                "<client_secrets.json>`, or set GOOGLE_APPLICATION_CREDENTIALS, "
                "or pass --credentials."
            ) from exc
        return credentials

    def _interactive_login(self, client_secrets: Path) -> Any:
        if not self._allow_interactive:
            raise DriveError(
                "interactive sign-in required: run "
                f"`audioscan auth --credentials {client_secrets}` first"
            )
        return self.login(client_secrets=client_secrets)

    # -- AuthProvider protocol ----------------------------------------------
    def credentials(self) -> Any:
        """Return valid credentials, refreshing them when expired."""
        with self._lock:
            if self._credentials is None:
                self._credentials = self._load()
            credentials = self._credentials
            if not getattr(credentials, "valid", False):
                credentials.refresh(_request_adapter())
            return credentials

    def headers(self) -> dict[str, str]:
        """Authorisation header for the next Drive request."""
        token = getattr(self.credentials(), "token", None)
        if not token:
            raise DriveError("credentials have no access token; run `audioscan auth`")
        return {"Authorization": f"Bearer {token}"}

    def refresh(self) -> None:
        """Force a token refresh (called after a 401 from the API)."""
        with self._lock:
            if self._credentials is None:
                self._credentials = self._load()
            else:
                self._credentials.refresh(_request_adapter())

    # -- interactive setup ---------------------------------------------------
    def login(self, *, client_secrets: str | os.PathLike[str] | None = None) -> Any:
        """Run the installed-app OAuth flow and cache the resulting token."""
        _require_google_auth()
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise DriveNotInstalled(
                "interactive sign-in needs google-auth-oauthlib: pip install 'audioscan[gdrive]'"
            ) from exc

        secrets_path = (
            Path(client_secrets).expanduser() if client_secrets else self._credentials_path
        )
        if secrets_path is None:
            raise DriveError("pass --credentials <client_secrets.json> to sign in")
        if not secrets_path.exists():
            raise DriveError(f"client secrets file not found: {secrets_path}")
        if credential_kind(secrets_path) != "client_secrets":
            raise DriveError(
                f"{secrets_path} is not an OAuth client secrets file "
                "(expected a 'installed' or 'web' key)"
            )

        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), list(self._scopes))
        try:
            credentials = flow.run_local_server(port=0)
        except Exception as exc:
            from webbrowser import Error as BrowserError

            if not isinstance(exc, BrowserError):
                raise
            # ponytail: retry without browser launch; manual URL works when the
            # browser can reach this host's temporary OAuth callback port.
            credentials = flow.run_local_server(port=0, open_browser=False)
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(credentials.to_json(), encoding="utf-8")
        with contextlib.suppress(OSError):
            self._token_path.chmod(0o600)
        with self._lock:
            self._credentials = credentials
            self._description = f"cached token ({self._token_path})"
        return credentials

    def logout(self) -> bool:
        """Delete the cached token; returns True when a file was removed."""
        if self._token_path.exists():
            self._token_path.unlink()
            with self._lock:
                self._credentials = None
            return True
        return False

    def describe(self) -> dict[str, Any]:
        """Summarise the credential configuration without touching the network.

        Never raises: ``audioscan auth --status`` must be able to report a bad
        configuration rather than crashing on it.
        """
        problem: str | None = None
        path: Path | None = None
        kind: str | None = None
        try:
            path = self._resolve_credentials_path()
            if path is not None:
                kind = credential_kind(path)
        except DriveError as exc:
            problem = str(exc)
        shown_path = path or self._credentials_path
        return {
            "credentials_path": str(shown_path) if shown_path else None,
            "credential_kind": kind,
            "token_path": str(self._token_path),
            "token_cached": self._token_path.exists(),
            "description": self._description,
            "problem": problem,
        }


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or payload)[:200]


class DriveSource:
    """List and open audio files stored in Google Drive."""

    name = SOURCE_GDRIVE

    def __init__(
        self,
        *,
        folder_id: str | None = None,
        file_id: str | None = None,
        query: str | None = None,
        auth: DriveAuth | None = None,
        client: httpx.Client | None = None,
        extensions: frozenset[str] = AUDIO_EXTENSIONS,
        recursive: bool = True,
        page_size: int = 1000,
        timeout: float = 60.0,
        block_size: int = DEFAULT_BLOCK_SIZE,
        budget_bytes: int | None = None,
        max_retries: int = 3,
    ) -> None:
        self._folder_id = folder_id
        self._file_id = file_id
        self._query = query
        self._auth = auth or DriveAuth()
        self._extensions = extensions
        self._recursive = recursive
        self._page_size = page_size
        self._timeout = timeout
        self._block_size = block_size
        self._budget_bytes = budget_bytes
        self._max_retries = max(0, max_retries)
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=True)

    @property
    def auth(self) -> DriveAuth:
        """The credential provider backing this source."""
        return self._auth

    # -- listing -------------------------------------------------------------
    def _audio_query(self, folder_id: str | None) -> str:
        clauses = " or ".join(f"name contains '{ext}'" for ext in sorted(self._extensions))
        parts = ["trashed = false", f"({clauses})"]
        if folder_id:
            parts.insert(0, f"'{folder_id}' in parents")
        if self._query:
            parts.append(f"({self._query})")
        return " and ".join(parts)

    def _folder_query(self, folder_id: str) -> str:
        return f"trashed = false and mimeType = '{FOLDER_MIME}' and '{folder_id}' in parents"

    def _list_pages(self, query: str) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        while True:
            payload = self._request_list(query, page_token)
            for item in payload.get("files") or []:
                if isinstance(item, dict):
                    yield item
            page_token = payload.get("nextPageToken")
            if not page_token:
                return

    def _request_list(self, query: str, page_token: str | None) -> dict[str, Any]:
        params = {
            "q": query,
            "fields": DRIVE_FIELDS,
            "pageSize": str(self._page_size),
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token

        last_error = ""
        for attempt in range(self._max_retries + 1):
            response = self._client.get(
                DRIVE_API, params=params, headers=self._auth.headers(), timeout=self._timeout
            )
            if response.status_code == 401 and attempt == 0:
                self._auth.refresh()
                continue
            if response.status_code in RETRY_STATUS and attempt < self._max_retries:
                time.sleep(min(8.0, 0.5 * (2**attempt)))
                continue
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}: {_error_message(response)}"
                raise DriveError(f"Drive list request failed: {last_error}")
            payload = response.json()
            if not isinstance(payload, dict):  # pragma: no cover - defensive
                raise DriveError("Drive list request returned an unexpected payload")
            return payload
        raise DriveError(f"Drive list request failed after retries: {last_error}")

    def _get_file(self, file_id: str) -> dict[str, Any]:
        response = self._client.get(
            f"{DRIVE_API}/{file_id}",
            params={
                "fields": "id,name,mimeType,size,modifiedTime,md5Checksum,parents",
                "supportsAllDrives": "true",
            },
            headers=self._auth.headers(),
            timeout=self._timeout,
        )
        if response.status_code >= 400:
            raise DriveError(
                f"Drive file lookup failed: HTTP {response.status_code}: {_error_message(response)}"
            )
        return response.json()

    def _to_remote(self, item: dict[str, Any], prefix: str) -> RemoteFile:
        name = str(item.get("name") or item.get("id") or "")
        size_text = item.get("size")
        size = int(size_text) if isinstance(size_text, str) and size_text.isdigit() else None
        return RemoteFile(
            id=str(item["id"]),
            name=name,
            path=f"{prefix}{name}",
            size=size,
            mime_type=item.get("mimeType"),
            modified=item.get("modifiedTime"),
        )

    def iter_files(self) -> Iterator[RemoteFile]:
        """Yield every audio file in scope, walking sub-folders when asked."""
        if self._file_id:
            yield self._to_remote(self._get_file(self._file_id), "")
            return

        if self._folder_id and self._recursive:
            stack: list[tuple[str, str]] = [(self._folder_id, "")]
            while stack:
                folder_id, prefix = stack.pop()
                for folder in self._list_pages(self._folder_query(folder_id)):
                    child_prefix = f"{prefix}{folder.get('name', '')}/"
                    stack.append((str(folder["id"]), child_prefix))
                for item in self._list_pages(self._audio_query(folder_id)):
                    if looks_like_audio(str(item.get("name")), self._extensions):
                        yield self._to_remote(item, prefix)
            return

        for item in self._list_pages(self._audio_query(self._folder_id)):
            if looks_like_audio(str(item.get("name")), self._extensions):
                yield self._to_remote(item, "")

    # -- reading -------------------------------------------------------------
    def open(self, item: RemoteFile) -> ReadableStream:
        """Open a seekable reader over Drive's ranged media endpoint."""
        url = f"{DRIVE_API}/{item.id}?alt=media&supportsAllDrives=true"
        fetcher = HttpRangeFetcher.open(
            url,
            size=item.size,
            client=self._client,
            auth=self._auth,
            timeout=self._timeout,
            max_retries=self._max_retries,
            name=item.path or item.name,
        )
        return SeekableBlockReader(
            fetcher,
            block_size=self._block_size,
            budget_bytes=self._budget_bytes,
            name=item.path or item.name,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


__all__ = [
    "DRIVE_API",
    "DRIVE_READONLY_SCOPE",
    "DriveAuth",
    "DriveError",
    "DriveNotInstalled",
    "DriveSource",
    "config_dir",
    "credential_kind",
    "default_token_path",
]
