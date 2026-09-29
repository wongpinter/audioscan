"""Private, single-user audiobook web app."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.middleware.sessions import SessionMiddleware

from .models import SOURCE_GDRIVE, TrackMeta
from .naming import group_tracks
from .probe import probe
from .reader import FetchError, SeekableBlockReader
from .sources.base import RemoteFile
from .sources.gdrive import DRIVE_API, DRIVE_READONLY_SCOPE, DriveError, DriveSource
from .sources.http import HttpRangeFetcher


class WebConfig:
    def __init__(self) -> None:
        self.allowed_email = os.getenv("APP_ALLOWED_EMAIL", "").strip().lower()
        self.base_url = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
        self.host = self.base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        self.secret = os.getenv("APP_SECRET_KEY", "")
        self.cookie_secure = self.base_url.startswith("https://")
        self.require_https = self.cookie_secure or self.host in {"localhost", "127.0.0.1", "::1"}
        self.client_secrets = Path(os.getenv("GOOGLE_CLIENT_SECRETS", "client.json"))
        self.folder_id = os.getenv("AUDIOBOOKS_FOLDER_ID", "")
        self.db_path = Path(os.getenv("APP_DB_PATH", "data/audiobooks.sqlite3"))
        self.credentials_path = self.db_path.with_name("credentials.json")

    def check(self) -> None:
        if not self.require_https:
            raise RuntimeError("APP_BASE_URL must use HTTPS outside local development")
        if len(self.secret) < 32:
            raise RuntimeError("APP_SECRET_KEY must contain at least 32 characters")
        missing = [
            name
            for name, value in (
                ("APP_ALLOWED_EMAIL", self.allowed_email),
                ("APP_SECRET_KEY", self.secret),
                ("AUDIOBOOKS_FOLDER_ID", self.folder_id),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("Missing required settings: " + ", ".join(missing))


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT,
                    cover TEXT, duration REAL NOT NULL DEFAULT 0, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY, book_id TEXT NOT NULL, name TEXT NOT NULL,
                    path TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
                    mime_type TEXT, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS progress (
                    book_id TEXT PRIMARY KEY, track_id TEXT NOT NULL,
                    position REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def credentials(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM credentials WHERE id=1").fetchone()
        if not row:
            return None
        data = json.loads(row["data"])
        return data if data.get("refresh_token") else None

    def save_credentials(self, data: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO credentials(id,data) VALUES(1,?)", (data,))

    def sync_credentials(self, path: Path) -> None:
        data = self.credentials()
        if data is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)

    def library(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            books = db.execute("SELECT data FROM books ORDER BY title COLLATE NOCASE").fetchall()
            progress = {row["book_id"]: dict(row) for row in db.execute("SELECT * FROM progress")}
        output = []
        for row in books:
            book = json.loads(row["data"])
            book["progress"] = progress.get(book["id"])
            output.append(book)
        return output

    def book(self, book_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM books WHERE id=?", (book_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def track(self, track_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM tracks WHERE id=?", (track_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def save_library(self, books: list[dict[str, Any]], tracks: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM tracks")
            db.execute("DELETE FROM books")
            db.executemany(
                "INSERT INTO books(id,title,artist,cover,duration,data) VALUES(?,?,?,?,?,?)",
                [
                    (
                        b["id"],
                        b["title"],
                        b.get("artist"),
                        b.get("cover"),
                        b["duration"],
                        json.dumps(b),
                    )
                    for b in books
                ],
            )
            db.executemany(
                "INSERT INTO tracks(id,book_id,name,path,size,mime_type,data) "
                "VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        t["id"],
                        t["book_id"],
                        t["name"],
                        t["path"],
                        t.get("size") or 0,
                        t.get("mime_type"),
                        json.dumps(t),
                    )
                    for t in tracks
                ],
            )

    def save_progress(self, book_id: str, track_id: str, position: float) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO progress(book_id,track_id,position,updated_at)
                VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(book_id) DO UPDATE SET
                track_id=excluded.track_id, position=excluded.position,
                updated_at=CURRENT_TIMESTAMP""",
                (book_id, track_id, position),
            )


class StoredDriveAuth:
    """Load and refresh OAuth credentials stored on the server."""

    def __init__(self, db: Database, credentials_path: Path) -> None:
        self.db = db
        self._credentials_path = credentials_path
        self.creds: Any = None

    def _load(self) -> Any:
        from google.oauth2.credentials import Credentials

        data = self.db.credentials()
        if not data:
            raise DriveError("Drive access is not connected. Sign in again.")
        self.creds = Credentials.from_authorized_user_info(data, scopes=[DRIVE_READONLY_SCOPE])
        return self.creds

    def headers(self) -> dict[str, str]:
        from google.auth.transport.requests import Request as GoogleRequest

        creds = self.creds or self._load()
        if not creds.valid:
            creds.refresh(GoogleRequest())
            self.db.save_credentials(creds.to_json())
        self.db.sync_credentials(self._credentials_path)
        return {"Authorization": f"Bearer {creds.token}"}

    def refresh(self) -> None:
        self.creds = None
        self.headers()


def _range_header(value: str, size: int) -> tuple[int, int]:
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", value.strip())
    if match and size > 0:
        start = int(match[1])
        end = min(int(match[2]) if match[2] else size - 1, size - 1)
        if start < size and end >= start:
            return start, end
    suffix = re.fullmatch(r"bytes=-(\d+)", value.strip())
    if suffix and size > 0 and int(suffix[1]) > 0:
        return max(0, size - int(suffix[1])), size - 1
    raise ValueError("invalid or unsatisfiable range")


def _mime_type(track: dict[str, Any]) -> str:
    provided = track.get("mime_type")
    if isinstance(provided, str) and provided.startswith("audio/"):
        return provided
    return {
        ".m4a": "audio/mp4",
        ".m4b": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".aac": "audio/aac",
    }.get(Path(track["name"]).suffix.lower(), "application/octet-stream")


def create_app(config: WebConfig | None = None, db: Database | None = None) -> FastAPI:
    config = config or WebConfig()
    if not config.secret:
        config.secret = "development-only-change-me-set-APP_SECRET_KEY"
    db = db or Database(config.db_path)
    app = FastAPI(title="Audiobooks")
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret or "development-only-change-me",
        https_only=config.cookie_secure,
        same_site="lax",
    )
    app.state.config = config
    app.state.db = db

    def user(request: Request) -> str:
        email = str(request.session.get("email", "")).lower()
        if not email or email != config.allowed_email:
            raise HTTPException(401, "Sign in required")
        return email

    def flow_for(state: str | None = None) -> Any:
        from google_auth_oauthlib.flow import Flow

        if not config.client_secrets.is_file():
            raise HTTPException(503, "Set GOOGLE_CLIENT_SECRETS to an OAuth web client JSON file")
        flow = Flow.from_client_secrets_file(
            str(config.client_secrets),
            scopes=["openid", "email", "profile", DRIVE_READONLY_SCOPE],
            state=state,
        )
        flow.redirect_uri = f"{config.base_url}/auth/callback"
        return flow

    @app.get("/auth/google")
    def auth_google(request: Request) -> RedirectResponse:
        if not config.allowed_email:
            raise HTTPException(503, "Set APP_ALLOWED_EMAIL before enabling sign-in")
        state = secrets.token_urlsafe(32)
        flow = flow_for()
        url, _ = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent", state=state
        )
        request.session["oauth_state"] = state
        request.session["oauth_callback_url"] = flow.redirect_uri
        return RedirectResponse(url)

    @app.get("/auth/callback")
    def auth_callback(
        request: Request, state: str = "", code: str = "", error: str = ""
    ) -> RedirectResponse:
        expected = request.session.pop("oauth_state", "")
        valid_state = state and expected and secrets.compare_digest(state, expected)
        if error or not code or not valid_state:
            raise HTTPException(400, "Google sign-in was cancelled or state validation failed")
        flow = flow_for(state)
        if request.session.pop("oauth_callback_url", "") != flow.redirect_uri:
            raise HTTPException(400, "OAuth callback URL changed during sign-in")
        flow.fetch_token(code=code)
        creds = flow.credentials
        try:
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2 import id_token

            if not creds.id_token:
                raise ValueError("Google did not return an identity token")
            identity = id_token.verify_oauth2_token(
                creds.id_token, GoogleRequest(), audience=flow.client_config["client_id"]
            )
        except Exception as exc:
            raise HTTPException(401, "Google identity token validation failed") from exc
        email = str(identity.get("email", "")).lower()
        if not identity.get("email_verified") or email != config.allowed_email:
            raise HTTPException(403, "This Google account is not allowed")
        granted = set(creds.scopes or [])
        if DRIVE_READONLY_SCOPE not in granted:
            raise HTTPException(403, "Approve read-only Google Drive access to use the library")
        if not creds.refresh_token:
            raise HTTPException(403, "Reconnect Google with offline access enabled")
        request.session.clear()
        request.session["email"] = email
        db.save_credentials(creds.to_json())
        db.sync_credentials(config.credentials_path)
        return RedirectResponse("/")

    @app.post("/auth/logout")
    def logout(request: Request) -> Response:
        user(request)
        request.session.clear()
        db.save_credentials("{}")
        config.credentials_path.unlink(missing_ok=True)
        return Response(status_code=204)

    def refresh_library() -> None:
        config.check()
        auth = StoredDriveAuth(db, config.credentials_path)
        source = DriveSource(folder_id=config.folder_id, auth=auth, timeout=60, budget_bytes=None)
        try:
            metadata: list[TrackMeta] = []
            remote_by_id: dict[str, RemoteFile] = {}
            for remote in source.iter_files():
                stream = source.open(remote)
                try:
                    track = probe(
                        stream,
                        name=remote.name,
                        path=remote.path,
                        source=SOURCE_GDRIVE,
                        file_id=remote.id,
                        size=remote.size,
                        mime_type=remote.mime_type,
                        modified=remote.modified,
                    )
                    metadata.append(track)
                    remote_by_id[remote.id] = remote
                finally:
                    stream.close()
            groups = group_tracks(metadata)
            tracks: list[dict[str, Any]] = []
            books: list[dict[str, Any]] = []
            for group in groups:
                book_id = group.key
                members = []
                for grouped in group.tracks:
                    meta = grouped.track.to_dict()
                    remote = remote_by_id[meta["id"]]
                    item = {
                        "id": remote.id,
                        "book_id": book_id,
                        "name": remote.name,
                        "path": remote.path,
                        "size": remote.size or 0,
                        "mime_type": remote.mime_type,
                        "title": meta.get("title") or remote.name,
                        "duration": meta.get("duration") or 0,
                        "chapters": meta.get("chapters") or [],
                        "format": meta.get("format"),
                    }
                    tracks.append(item)
                    members.append(item)
                first = group.tracks[0].track
                books.append(
                    {
                        "id": book_id,
                        "title": group.title,
                        "artist": first.albumartist or first.artist,
                        "cover": f"/api/tracks/{members[0]['id']}/cover" if first.covers else None,
                        "duration": group.total_duration,
                        "tracks": members,
                    }
                )
            db.save_library(books, tracks)
        finally:
            source.close()

    @app.get("/api/library")
    def library(request: Request, refresh: bool = False) -> list[dict[str, Any]]:
        user(request)
        try:
            config.check()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        books = db.library()
        if refresh or not books:
            try:
                refresh_library()
            except Exception as exc:
                raise HTTPException(502, f"Could not scan Google Drive: {exc}") from exc
            books = db.library()
        return books

    @app.post("/api/library/refresh")
    def library_refresh(request: Request) -> dict[str, int]:
        user(request)
        try:
            refresh_library()
        except Exception as exc:
            raise HTTPException(502, f"Could not scan Google Drive: {exc}") from exc
        return {"books": len(db.library())}

    @app.get("/api/books/{book_id}")
    def get_book(book_id: str, request: Request) -> dict[str, Any]:
        user(request)
        book = db.book(book_id)
        if book is None:
            raise HTTPException(404, "Book not found")
        if book.get("tracks"):
            return book
        raise HTTPException(404, "Book has no playable tracks")

    @app.put("/api/progress/{book_id}")
    async def progress(book_id: str, request: Request) -> dict[str, Any]:
        user(request)
        book = db.book(book_id)
        if book is None:
            raise HTTPException(404, "Book not found")
        if not isinstance(body := await request.json(), dict):
            raise HTTPException(422, "request body must be a JSON object")
        track_id = str(body.get("track_id", ""))
        try:
            position = float(body.get("position", 0))
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "position must be a number") from exc
        if not 0 <= position <= 10_000_000:
            raise HTTPException(422, "position is out of range")
        if not any(track["id"] == track_id for track in book["tracks"]):
            raise HTTPException(422, "track does not belong to this book")
        db.save_progress(book_id, track_id, position)
        return {"book_id": book_id, "track_id": track_id, "position": position}

    @app.get("/api/progress/{book_id}")
    def get_progress(book_id: str, request: Request) -> dict[str, Any] | None:
        user(request)
        book = db.book(book_id)
        if book is None:
            raise HTTPException(404, "Book not found")
        with db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM progress WHERE book_id=?", (book_id,)
            ).fetchone()
        return dict(row) if row else None

    @app.head("/api/tracks/{track_id}/audio")
    def audio_head(track_id: str, request: Request) -> Response:
        user(request)
        track = db.track(track_id)
        if track is None:
            raise HTTPException(404, "Track not found")
        size = int(track.get("size") or 0)
        if size <= 0:
            raise HTTPException(502, "Drive did not report the audio file size")
        return Response(
            status_code=200,
            media_type=_mime_type(track),
            headers={"Content-Length": str(size), "Accept-Ranges": "bytes"},
        )

    @app.get("/api/tracks/{track_id}/audio")
    def audio(track_id: str, request: Request) -> Response:
        user(request)
        track = db.track(track_id)
        if track is None:
            raise HTTPException(404, "Track not found")
        size = int(track.get("size") or 0)
        if size <= 0:
            raise HTTPException(502, "Drive did not report the audio file size")
        try:
            start, end = _range_header(request.headers.get("range", f"bytes=0-{size - 1}"), size)
        except ValueError:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        auth = StoredDriveAuth(db, config.credentials_path)
        client = httpx.Client(follow_redirects=True)
        url = f"{DRIVE_API}/{track_id}?alt=media&supportsAllDrives=true"
        try:
            fetcher = HttpRangeFetcher.open(
                url, size=size, client=client, auth=auth, name=track["name"]
            )
            reader = SeekableBlockReader(fetcher, block_size=64 * 1024, name=track["name"])
        except Exception:
            client.close()
            raise
        length = end - start + 1

        def chunks() -> Iterator[bytes]:
            try:
                reader.seek(start)
                remaining = length
                while remaining:
                    chunk = reader.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            except FetchError as exc:
                raise HTTPException(502, f"Drive audio stream failed: {exc}") from exc
            finally:
                reader.close()
                client.close()

        mime = _mime_type(track)
        upstream_name = track["name"].lower()
        if upstream_name.endswith((".m4a", ".m4b", ".m4p", ".mp4")):
            mime = "audio/mp4"
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Cache-Control": "private, no-store",
        }
        if start != 0 or end != size - 1:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            status = 206
        else:
            status = 200
        return StreamingResponse(chunks(), status_code=status, media_type=mime, headers=headers)

    @app.get("/api/tracks/{track_id}/cover")
    def cover(track_id: str, request: Request) -> Response:
        user(request)
        track = db.track(track_id)
        if not track:
            raise HTTPException(404, "Track not found")
        remote = RemoteFile(
            id=track["id"],
            name=track["name"],
            path=track["path"],
            size=track["size"],
            mime_type=track["mime_type"],
        )
        source = DriveSource(
            auth=StoredDriveAuth(db, config.credentials_path),
            file_id=remote.id,
            budget_bytes=4 * 1024 * 1024,
        )
        stream = source.open(remote)
        try:
            from .probe import cover_bytes

            data = cover_bytes(stream)
        finally:
            stream.close()
            source.close()
        if not data:
            raise HTTPException(404, "Cover not found")
        return Response(
            data[0][2],
            media_type=str(data[0][0]),
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(_INDEX)

    @app.get("/manifest.webmanifest")
    def manifest() -> JSONResponse:
        return JSONResponse(
            {
                "name": "Audiobooks",
                "short_name": "Books",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#101010",
                "theme_color": "#101010",
                "icons": [
                    {
                        "src": "/icon.svg",
                        "sizes": "any",
                        "type": "image/svg+xml",
                        "purpose": "any maskable",
                    }
                ],
            },
            media_type="application/manifest+json",
        )

    @app.get("/icon.svg")
    def icon() -> Response:
        return Response(_ICON, media_type="image/svg+xml")

    @app.get("/sw.js")
    def service_worker() -> Response:
        return Response(
            _SERVICE_WORKER,
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(_ICON, media_type="image/svg+xml")

    return app


_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="112" fill="#101010"/>
<path d="M256 54a202 202 0 1 0 0 404 202 202 0 0 0 0-404zm-58 295V163l178 93z" fill="#b5f36d"/>
</svg>"""
_SERVICE_WORKER = """self.addEventListener('install', event => event.waitUntil(
  caches.open('audiobooks-v1').then(cache => cache.addAll([
    '/', '/manifest.webmanifest', '/icon.svg'
  ]))
));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  const isStaticGet = url.origin === location.origin
    && event.request.method === 'GET'
    && !url.pathname.startsWith('/api/');
  if (isStaticGet) {
    event.respondWith(
      caches.match(event.request).then(response => response || fetch(event.request))
    );
  }
});"""

_INDEX = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="theme-color" content="#101010">
  <link rel="manifest" href="/manifest.webmanifest">
  <title>Audiobooks</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101010;
      --panel: #1c1c1e;
      --text: #f5f5f7;
      --muted: #a1a1a6;
      --accent: #b5f36d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 16px system-ui, -apple-system, sans-serif;
      padding-bottom: calc(94px + env(safe-area-inset-bottom));
    }
    header {
      position: sticky;
      top: 0;
      background: #101010ee;
      backdrop-filter: blur(15px);
      padding: 18px 20px 12px;
      z-index: 2;
    }
    h1 { font-size: 28px; margin: 0 0 14px; }
    input {
      width: 100%;
      border: 0;
      border-radius: 14px;
      background: var(--panel);
      color: var(--text);
      padding: 14px;
      font: inherit;
    }
    main { padding: 8px 18px 24px; }
    .row {
      display: flex;
      gap: 14px;
      align-items: center;
      padding: 12px 2px;
      border-bottom: 1px solid #29292c;
    }
    .cover {
      width: 68px;
      height: 68px;
      border-radius: 9px;
      object-fit: cover;
      background: linear-gradient(135deg, #384d2b, #243b55);
      flex: none;
    }
    .meta { min-width: 0; flex: 1; }
    .meta strong, .meta span {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .meta span { color: var(--muted); font-size: 14px; margin-top: 5px; }
    .button {
      border: 0;
      border-radius: 12px;
      background: var(--accent);
      color: #121212;
      padding: 12px 16px;
      font-weight: 700;
    }
    #player {
      position: fixed;
      z-index: 3;
      bottom: 0;
      left: 0;
      right: 0;
      background: #202022f5;
      border-top: 1px solid #37373a;
      padding: 12px 18px calc(12px + env(safe-area-inset-bottom));
      backdrop-filter: blur(18px);
    }
    #now {
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 8px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    audio { width: 100%; height: 38px; }
    #chapters { padding: 0 18px; }
    .chapter { padding: 14px; border-bottom: 1px solid #2a2a2c; }
    .empty { padding: 30px 6px; color: var(--muted); line-height: 1.5; }
    .top { display: flex; justify-content: space-between; align-items: center; }
    .link { border: 0; color: var(--accent); background: none; font: inherit; }
  </style>
  </style>
</head>
<body>
  <header>
    <div class="top">
      <h1 id="heading">Your library</h1>
      <button class="link" id="signin">Sign in</button>
    </div>
    <input id="search" placeholder="Search books and authors" autocomplete="off">
  </header>
  <main id="library">
    <div class="empty">Sign in to connect your private audiobook library.</div>
  </main>
  <section id="chapters"></section>
  <div id="player">
    <div id="now">Choose a book to start listening</div>
    <audio id="audio" controls preload="metadata"></audio>
  </div>
  <script>
    const $ = selector => document.querySelector(selector);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, char =>
      ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
    let books = [], active = null, lastSave = 0;
    $('#signin').onclick = () => location.href = '/auth/google';
    async function api(url, options = {}) {
      const response = await fetch(url, {credentials: 'same-origin', ...options});
      if (response.status === 401) {
        $('#library').innerHTML = '<div class="empty">Sign in to view your library.</div>';
        throw Error('Sign in required');
      }
      if (!response.ok) throw Error(await response.text());
      return response.json();
    }
    async function load() {
      try {
        books = await api('/api/library');
        render();
      } catch (error) {
        if (error.message !== 'Sign in required') {
          $('#library').innerHTML =
            '<div class="empty">Could not load library. Check server settings.</div>';
        }
      }
    }
    function render() {
      const query = $('#search').value.toLowerCase();
      const filtered = books.filter(book =>
        (book.title + ' ' + (book.artist || '')).toLowerCase().includes(query));
      $('#library').innerHTML = filtered.length ? filtered.map(book =>
        `<article class="row" data-id="${esc(book.id)}">
          <img class="cover" src="${esc(book.cover || '')}"
               onerror="this.style.visibility='hidden'">
          <div class="meta"><strong>${esc(book.title)}</strong>
            <span>${esc(book.artist || 'Audiobook')} ·
              ${Math.round((book.duration || 0) / 60)} min</span>
            <span>${book.progress ? 'Resume · ' + Math.floor(book.progress.position / 60) + ' min' :
              'Tap to play'}</span>
          </div><button class="button play">Play</button></article>`).join('') :
          '<div class="empty">No matching books.</div>';
      document.querySelectorAll('.row .play').forEach(button =>
        button.onclick = () => play(button.closest('.row').dataset.id));
    }
    function play(id) {
      active = books.find(book => book.id === id);
      if (!active) return;
      $('#heading').textContent = active.title;
      $('#chapters').innerHTML = (active.tracks || []).flatMap(track =>
        (track.chapters || []).map((chapter, index) =>
          `<div class="chapter" data-track="${esc(track.id)}"
               data-start="${Number(chapter.start) || 0}">
            ${esc(chapter.title || ('Chapter ' + (index + 1)))}</div>`)).join('');
      document.querySelectorAll('.chapter').forEach(element => {
        element.onclick = () => startTrack(
          active.tracks.find(track => track.id === element.dataset.track),
          Number(element.dataset.start)
        );
      });
      const resume = active.tracks.find(track => track.id === active.progress?.track_id);
      startTrack(resume || active.tracks[0], resume ? active.progress.position : 0);
    }
    function startTrack(track, position = 0) {
      if (!track) return;
      $('#now').textContent = active.title + ' · ' + (track.title || track.name);
      const player = $('#audio');
      player.src = '/api/tracks/' + encodeURIComponent(track.id) + '/audio';
      player.onloadedmetadata = () => { if (position > 0) player.currentTime = position; };
      player.onended = () => {
        const index = active.tracks.findIndex(item => item.id === track.id);
        const next = active.tracks[index + 1];
        if (next) startTrack(next);
      };
      player.play().catch(() => {});
      player.ontimeupdate = () => {
        if (active && player.currentTime > 0 && Date.now() - lastSave > 10000) {
          save(track.id, player.currentTime);
        }
      };
    }
    async function save(trackId, position, bookId = active?.id) {
      if (!bookId) return;
      lastSave = Date.now();
      try {
        await fetch('/api/progress/' + encodeURIComponent(bookId), {
          method: 'PUT', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({track_id: trackId, position})
        });
        if (active?.id === bookId) active.progress = {track_id: trackId, position};
      } catch (error) {}
    }
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
    $('#search').oninput = render;
    document.addEventListener('visibilitychange', () => {
      if (document.hidden && active) {
        const track = active.tracks.find(item =>
          $('#audio').src.includes(encodeURIComponent(item.id)));
        if (track) save(track.id, $('#audio').currentTime);
      }
    });
    load();
</body>
</html>"""


def main() -> None:
    import uvicorn

    config = WebConfig()
    app = create_app(config)
    uvicorn.run(
        app, host=os.getenv("APP_HOST", "127.0.0.1"), port=int(os.getenv("APP_PORT", "8000"))
    )


app = create_app()
