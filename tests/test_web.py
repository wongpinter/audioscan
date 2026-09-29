from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audioscan.reader import BytesFetcher, Fetcher
from audioscan.web import Database, WebConfig, _range_header, create_app


def config(tmp_path: Path) -> WebConfig:
    result = WebConfig()
    result.allowed_email = "reader@example.com"
    result.base_url = "https://books.example.com"
    result.secret = "local-test-key-" + "x" * 32
    result.client_secrets = tmp_path / "client.json"
    result.folder_id = "folder-id"
    result.db_path = tmp_path / "library.sqlite3"
    return result


def test_deployment_settings_require_https_long_secret_and_root_folder(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.base_url = "http://books.example.com"
    cfg.cookie_secure = False
    cfg.host = "books.example.com"
    cfg.require_https = False
    with pytest.raises(RuntimeError, match="HTTPS"):
        cfg.check()
    cfg.base_url = "https://books.example.com"
    cfg.cookie_secure = True
    cfg.require_https = True
    cfg.secret = "short"
    with pytest.raises(RuntimeError, match="32 characters"):
        cfg.check()
    cfg.secret = "strong-secret-" + "x" * 32
    cfg.folder_id = ""
    with pytest.raises(RuntimeError, match="AUDIOBOOKS_FOLDER_ID"):
        cfg.check()


def test_range_header_supports_byte_and_suffix_ranges() -> None:
    assert _range_header("bytes=10-19", 100) == (10, 19)
    assert _range_header("bytes=90-", 100) == (90, 99)
    assert _range_header("bytes=-10", 100) == (90, 99)
    assert _range_header("bytes=-200", 100) == (0, 99)
    for value in ("bytes=100-", "bytes=10-9", "bytes=0-1,4-5", "items=0-2"):
        with pytest.raises(ValueError):
            _range_header(value, 100)


def test_range_headers_reject_empty_suffix_and_huge_offset() -> None:
    for value in ("bytes=-0", "bytes=", "bytes=999999999999999999999999999-"):
        with pytest.raises(ValueError):
            _range_header(value, 100)


def seeded_db(path: Path) -> Database:
    db = Database(path)
    track = {
        "id": "drive-track",
        "book_id": "book-key",
        "name": "Book.m4b",
        "path": "Book.m4b",
        "size": 16,
        "mime_type": "application/octet-stream",
        "title": "Book",
        "duration": 60,
        "chapters": [],
    }
    book = {
        "id": "book-key",
        "title": "Book",
        "artist": "Author",
        "cover": None,
        "duration": 60,
        "tracks": [track],
    }
    db.save_library([book], [track])
    return db


def sign_in(client: TestClient) -> None:
    from itsdangerous import TimestampSigner
    from starlette.middleware.sessions import SessionMiddleware

    middleware = next(m for m in client.app.user_middleware if m.cls is SessionMiddleware)
    signer = TimestampSigner(middleware.kwargs["secret_key"])
    from base64 import b64encode

    cookie = signer.sign(b64encode(json.dumps({"email": "reader@example.com"}).encode())).decode()
    client.cookies.set("session", cookie)


def test_library_features_favorites_ratings_tags_playlists_history_storage(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    client = TestClient(create_app(cfg, db))
    sign_in(client)

    assert client.put("/api/books/book-key/favorite", json={"favorite": True}).json() == {
        "favorite": True
    }
    assert client.put("/api/books/book-key/rating", json={"rating": 5}).json() == {"rating": 5}
    assert client.put(
        "/api/books/book-key/tags", json={"tags": ["Sci-fi", "  Classic "]}
    ).json() == {"tags": ["Sci-fi", "Classic"]}
    playlist = client.post("/api/playlists", json={"name": "Road trip"})
    assert playlist.status_code == 201
    playlist_id = playlist.json()["id"]
    assert client.put(
        f"/api/playlists/{playlist_id}/books", json={"book_ids": ["book-key"]}
    ).json() == {"book_ids": ["book-key"]}
    assert client.post("/api/history/book-key", json={"track_id": "drive-track"}).status_code == 204
    assert client.get("/api/features").json()["playlists"][0]["book_ids"] == ["book-key"]
    book = client.get("/api/library").json()[0]
    assert (
        book["favorite"] is True and book["rating"] == 5 and book["tags"] == ["Classic", "Sci-fi"]
    )
    assert client.get("/api/features").json()["history"][0]["track_id"] == "drive-track"
    assert client.get("/api/storage").json()["bytes"] == 16

    assert client.put("/api/books/book-key/rating", json={"rating": 6}).status_code == 422
    assert client.put("/api/books/book-key/tags", json={"tags": [""]}).status_code == 422
    assert (
        client.put(
            f"/api/playlists/{playlist_id}/books", json={"book_ids": ["missing"]}
        ).status_code
        == 422
    )
    assert client.delete(f"/api/playlists/{playlist_id}").status_code == 204
    assert client.put("/api/books/book-key/favorite", json={"favorite": False}).json() == {
        "favorite": False
    }


def test_chapter_endpoint_pages_results_and_validates_bounds(tmp_path: Path) -> None:
    db = seeded_db(tmp_path / "app.sqlite")
    book = db.book("book-key")
    assert book is not None
    chapters = [{"number": i, "start": float(i), "title": f"Chapter {i}"} for i in range(1, 4)]
    book["tracks"][0]["chapters"] = chapters
    db.save_library([book], [book["tracks"][0]])
    client = TestClient(create_app(config(tmp_path), db))
    sign_in(client)

    first = client.get("/api/tracks/drive-track/chapters?offset=0&limit=2")
    assert first.status_code == 200
    assert [item["number"] for item in first.json()["chapters"]] == [1, 2]
    assert first.json()["next_offset"] == 2
    last = client.get("/api/tracks/drive-track/chapters?offset=2&limit=2")
    assert [item["number"] for item in last.json()["chapters"]] == [3]
    assert last.json()["next_offset"] is None
    assert client.get("/api/tracks/drive-track/chapters?offset=-1").status_code == 422
    assert client.get("/api/tracks/drive-track/chapters?limit=101").status_code == 422


def test_private_routes_require_allowed_signed_in_user(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    assert client.get("/api/library").status_code == 401
    sign_in(client)
    response = client.get("/api/library")
    assert response.status_code == 200
    assert response.json()[0]["title"] == "Book"


def test_invalid_app_settings_block_library_scan(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.folder_id = ""
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    sign_in(client)
    response = client.get("/api/library?refresh=true")
    assert response.status_code == 503
    assert "AUDIOBOOKS_FOLDER_ID" in response.text


def test_progress_requires_valid_book_track_and_position(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    path = "/api/progress/book-key"
    assert client.put(path, json={"track_id": "wrong", "position": 10}).status_code == 422
    assert client.put(path, json={"track_id": "drive-track", "position": -1}).status_code == 422
    saved = client.put(path, json={"track_id": "drive-track", "position": 12.5})
    assert saved.status_code == 200
    assert db.library()[0]["progress"]["position"] == 12.5


def test_audio_endpoint_proxies_only_requested_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import audioscan.web as web

    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    payload = b"0123456789abcdef"

    def open_reader(*args: Any, **kwargs: Any) -> Fetcher:
        return BytesFetcher(payload)

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(web.HttpRangeFetcher, "open", open_reader)
    monkeypatch.setattr(web.httpx, "Client", FakeClient)
    client = TestClient(create_app(cfg, db))
    sign_in(client)
    response = client.get("/api/tracks/drive-track/audio", headers={"Range": "bytes=4-7"})
    assert response.status_code == 206
    assert response.content == b"4567"
    assert response.headers["content-range"] == "bytes 4-7/16"
    assert response.headers["content-length"] == "4"
    assert response.headers["content-type"].startswith("audio/mp4")


def test_scan_commit_restores_nested_metadata_types(tmp_path: Path) -> None:
    from audioscan.models import Chapter, Cover
    from audioscan.web import LibraryScanner

    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    scanner = LibraryScanner(cfg, db)
    item = {
        "id": "track-1",
        "path": "Book/track.mp3",
        "name": "track.mp3",
        "size": 100,
        "mime_type": "audio/mpeg",
        "error": "",
        "meta": {
            "id": "track-1",
            "name": "track.mp3",
            "path": "Book/track.mp3",
            "album": "Book",
            "covers": [Cover(index=0, mime="image/jpeg").to_dict()],
            "chapters": [Chapter(number=1, start=0, title="Start").to_dict()],
        },
    }
    scanner._commit([item])
    book = db.book(db.library()[0]["id"])
    assert book is not None
    assert book["tracks"][0]["title"] == "track.mp3"


def test_scan_status_partial_update_keeps_required_status(tmp_path: Path) -> None:
    db = seeded_db(tmp_path / "library.sqlite3")
    db.save_scan_status(status="running", total=0, processed=0)
    db.save_scan_status(total=3, current="Book/")
    status = db.scan_status()
    assert status is not None
    assert status["status"] == "running"
    assert status["total"] == 3
    assert status["current"] == "Book/"


def test_cover_route_returns_embedded_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import audioscan.web as web

    cfg = config(tmp_path)
    db = seeded_db(cfg.db_path)
    client = TestClient(create_app(cfg, db))
    sign_in(client)

    class Reader:
        def close(self) -> None:
            pass

    class Source:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def open(self, item: Any) -> Reader:
            return Reader()

        def close(self) -> None:
            pass

    monkeypatch.setattr(web, "DriveSource", Source)
    import importlib

    probe_module = importlib.import_module("audioscan.probe")
    monkeypatch.setattr(probe_module, "cover_bytes", lambda stream: (b"jpeg", "image/jpeg"))
    response = client.get("/api/tracks/drive-track/cover")
    assert response.status_code == 200
    assert response.content == b"jpeg"
    assert response.headers["content-type"].startswith("image/jpeg")


def test_library_scan_routes_return_progress_and_require_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import audioscan.web as web

    class IdleScanner:
        lock = web.threading.Lock()
        event_lock = web.threading.Lock()
        events: list[dict[str, Any]] = []
        db: Database

        def __init__(self, config: WebConfig, db: Database) -> None:
            self.db = db

        def stop_incomplete_scan(self) -> None:
            return None

        def start(self, retry_failed: bool = True) -> bool:
            if self.lock.locked():
                return False
            self.lock.acquire()
            self.db.save_scan_status(status="running", total=0, processed=0)
            return True

    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    assert client.post("/api/library/refresh").status_code == 401
    sign_in(client)
    monkeypatch.setattr(web, "LibraryScanner", IdleScanner)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    sign_in(client)
    assert client.get("/api/library/scan").json()["status"] == "idle"
    events = client.get("/api/library/events")
    assert events.status_code == 200
    assert "text/event-stream" in events.headers["content-type"]
    assert '"status": "idle"' in events.text
    assert client.post("/api/library/refresh").json() == {"status": "running"}
    assert client.get("/api/library/scan").json()["status"] == "running"


def test_oauth_flow_preserves_pkce_verifier_across_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    cfg.client_secrets.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["https://books.example.com/auth/callback"],
                }
            }
        )
    )
    verifier = "test-verifier"

    class FakeFlow:
        credentials: Any
        client_config = {"client_id": "client-id"}
        redirect_uri = ""
        code_verifier = verifier
        oauth2session = SimpleNamespace(_client=SimpleNamespace(code_verifier=None))

        @classmethod
        def from_client_secrets_file(cls, *args: Any, **kwargs: Any) -> FakeFlow:
            result = cls()
            result.code_verifier = kwargs.get("code_verifier")
            return result

        def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
            return (
                f"https://accounts.example.com/authorize?state={kwargs['state']}&code_challenge=test",
                kwargs["state"],
            )

        def fetch_token(self, **kwargs: Any) -> None:
            assert self.code_verifier
            assert self.oauth2session._client.code_verifier == self.code_verifier
            assert self.oauth2session._client.code_challenge is None

        @property
        def credentials(self) -> Any:
            return SimpleNamespace(
                id_token="identity-token",
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
                refresh_token="refresh-token",
                to_json=lambda: json.dumps({"refresh_token": "refresh-token"}),
            )

    google_auth_oauthlib = SimpleNamespace(flow=SimpleNamespace(Flow=FakeFlow))
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", google_auth_oauthlib)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", google_auth_oauthlib.flow)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    from urllib.parse import parse_qs, urlsplit

    from google.oauth2 import id_token

    monkeypatch.setattr(
        id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: {"email": "reader@example.com", "email_verified": True},
    )
    monkeypatch.setenv("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    start = client.get("/auth/google", follow_redirects=False)
    assert start.status_code == 307
    assert "code_challenge=" in start.headers["location"]
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = client.get(f"/auth/callback?state={state}&code=auth-code", follow_redirects=False)
    assert callback.status_code == 307
    assert callback.headers["location"] == "/"
    assert "session=" in callback.headers["set-cookie"]
    assert client.get("/api/library").status_code == 200


def test_pwa_shell_manifest_and_service_worker(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    page = client.get("/").text
    assert "viewport-fit=cover" in page
    assert '<div id="root"></div>' in page
    assert "/static/assets/index-" in page
    assert 'id="layout"' not in page
    asset = page.split('href="/static/', 1)[1].split('"', 1)[0]
    assert client.get(f"/static/{asset}").status_code == 200
    assert client.get("/book/book-key").status_code == 200
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert "url.pathname !== '/'" in worker.text
    assert "Service-Worker-Allowed" in worker.headers
    assert client.get("/favicon.ico").status_code == 200
