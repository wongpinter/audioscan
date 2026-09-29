from __future__ import annotations

import json
from pathlib import Path
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


def test_pwa_shell_manifest_and_service_worker(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, seeded_db(cfg.db_path)))
    assert "viewport-fit=cover" in client.get("/").text
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert "Service-Worker-Allowed" in worker.headers
    assert client.get("/favicon.ico").status_code == 200
