"""Google Drive source, driven through an httpx MockTransport (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from audioscan.reader import SeekableBlockReader
from audioscan.sources.gdrive import (
    DriveAuth,
    DriveError,
    DriveSource,
    credential_kind,
)

FOLDER_MIME = "application/vnd.google-apps.folder"


class FakeAuth:
    """Minimal AuthProvider that counts refreshes."""

    def __init__(self) -> None:
        self.refreshes = 0

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer token-{self.refreshes}"}

    def refresh(self) -> None:
        self.refreshes += 1


def listing(files: list[dict[str, Any]], next_token: str | None = None) -> httpx.Response:
    payload: dict[str, Any] = {"files": files}
    if next_token:
        payload["nextPageToken"] = next_token
    return httpx.Response(200, json=payload)


def range_response(request: httpx.Request, data: bytes) -> httpx.Response:
    """Serve bytes the way Drive does when a Range header is present."""
    header = request.headers.get("range")
    start, end = 0, len(data) - 1
    if header:
        spec = header.split("=", 1)[1]
        start_text, _, end_text = spec.partition("-")
        start = int(start_text)
        if end_text:
            end = min(int(end_text), len(data) - 1)
    if start >= len(data):
        return httpx.Response(416, headers={"Content-Range": f"bytes */{len(data)}"})
    chunk = data[start : end + 1]
    headers = {
        "Content-Range": f"bytes {start}-{end}/{len(data)}",
        "Content-Length": str(len(chunk)),
    }
    return httpx.Response(206 if header else 200, headers=headers, content=chunk)


class DriveStub:
    """Callable httpx transport handler with a request log."""

    def __init__(self, handler: Any = None, *, media: bytes = b"") -> None:
        self.handler = handler or (lambda request, params: listing([]))
        self.media = media
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = dict(request.url.params)
        if params.get("alt") == "media":
            return range_response(request, self.media)
        return self.handler(request, params)

    @property
    def media_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if dict(r.url.params).get("alt") == "media"]

    @property
    def list_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if "q" in dict(r.url.params)]


def make_source(stub: DriveStub, **kwargs: Any) -> tuple[DriveSource, FakeAuth]:
    auth = FakeAuth()
    client = httpx.Client(transport=httpx.MockTransport(stub))
    return DriveSource(auth=auth, client=client, **kwargs), auth


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #
def test_listing_yields_audio_only_and_queries_by_extension() -> None:
    stub = DriveStub(
        lambda request, params: listing(
            [
                {"id": "1", "name": "a.mp3", "mimeType": "audio/mpeg", "size": "100"},
                {"id": "2", "name": "cover.jpg", "mimeType": "image/jpeg"},
                {"id": "3", "name": "b.m4b", "mimeType": "application/octet-stream", "size": "200"},
            ]
        )
    )
    source, _auth = make_source(stub)
    items = list(source.iter_files())

    assert [item.name for item in items] == ["a.mp3", "b.m4b"]
    assert items[0].size == 100
    assert items[1].size == 200
    assert items[1].mime_type == "application/octet-stream"

    params = dict(stub.list_requests[0].url.params)
    query = params["q"]
    assert "trashed = false" in query
    assert "name contains '.mp3'" in query
    assert params["supportsAllDrives"] == "true"
    assert params["includeItemsFromAllDrives"] == "true"


def test_listing_follows_pagination() -> None:
    def handler(request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        if params.get("pageToken") == "page-2":
            return listing([{"id": "2", "name": "second.mp3"}])
        return listing([{"id": "1", "name": "first.mp3"}], next_token="page-2")

    source, _auth = make_source(DriveStub(handler))
    assert [item.name for item in source.iter_files()] == ["first.mp3", "second.mp3"]


def test_recursive_walk_descends_into_subfolders() -> None:
    def handler(request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        query = params["q"]
        if FOLDER_MIME in query:
            if "'root-folder' in parents" in query:
                return listing([{"id": "sub", "name": "Book 1", "mimeType": FOLDER_MIME}])
            return listing([])
        if "'sub' in parents" in query:
            return listing([{"id": "5", "name": "ch01.mp3", "size": "10"}])
        return listing([{"id": "4", "name": "root.mp3", "size": "10"}])

    source, _auth = make_source(DriveStub(handler), folder_id="root-folder")
    assert {item.path for item in source.iter_files()} == {"root.mp3", "Book 1/ch01.mp3"}


def test_non_recursive_folder_listing_skips_subfolders() -> None:
    def handler(request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        assert FOLDER_MIME not in params["q"]
        if "root-folder" in params["q"]:
            return listing([{"id": "4", "name": "root.mp3", "size": "10"}])
        return listing([])

    source, _auth = make_source(DriveStub(handler), folder_id="root-folder", recursive=False)
    assert [item.name for item in source.iter_files()] == ["root.mp3"]


def test_extra_drive_query_is_appended() -> None:
    stub = DriveStub()
    source, _auth = make_source(stub, query="name contains 'Potter'")
    list(source.iter_files())
    query = dict(stub.list_requests[0].url.params)["q"]
    assert query.endswith("(name contains 'Potter')")


def test_single_file_lookup_does_not_list() -> None:
    stub = DriveStub(
        lambda request, params: httpx.Response(
            200, json={"id": "x1", "name": "book.m4b", "size": "2048"}
        )
    )
    source, _auth = make_source(stub, file_id="x1")
    items = list(source.iter_files())

    assert [item.id for item in items] == ["x1"]
    assert items[0].size == 2048
    assert stub.list_requests == []
    assert (
        stub.requests[0].url.params["fields"]
        == "id,name,mimeType,size,modifiedTime,md5Checksum,parents"
    )


def test_single_file_lookup_refreshes_expired_token() -> None:
    calls = 0

    def handler(request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
        return httpx.Response(200, json={"id": "x1", "name": "book.m4b"})

    source, auth = make_source(DriveStub(handler), file_id="x1")
    assert [item.id for item in source.iter_files()] == ["x1"]
    assert calls == 2
    assert auth.refreshes == 1


# --------------------------------------------------------------------------- #
# media access
# --------------------------------------------------------------------------- #
def test_media_reads_use_range_headers_and_the_listed_size() -> None:
    data = bytes(range(256)) * 40  # 10240 bytes
    stub = DriveStub(
        lambda request, params: listing([{"id": "f1", "name": "book.m4b", "size": str(len(data))}]),
        media=data,
    )
    source, _auth = make_source(stub, block_size=1024)
    item = next(iter(source.iter_files()))

    reader = source.open(item)
    assert isinstance(reader, SeekableBlockReader)
    try:
        assert reader.size == len(data)
        reader.seek(1000)
        assert reader.read(50) == data[1000:1050]
        reader.seek(0)
        assert reader.read(16) == data[:16]
        assert reader.stats.bytes_fetched < len(data)
    finally:
        reader.close()

    media_requests = stub.media_requests
    assert media_requests
    assert all(request.headers.get("range") for request in media_requests)
    assert all("Authorization" in request.headers for request in media_requests)
    assert all("supportsAllDrives=true" in str(request.url) for request in media_requests)
    # The listing already reported the size, so no HEAD-style discovery was needed.
    assert all(request.method == "GET" for request in media_requests)


def test_transient_list_errors_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 500 from the Drive backend is worth another attempt."""
    monkeypatch.setattr("audioscan.sources.gdrive.time.sleep", lambda _seconds: None)
    calls = {"count": 0}

    def handler(request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(500, json={"error": {"message": "backend error"}})
        return listing([{"id": "1", "name": "a.mp3"}])

    source, _auth = make_source(DriveStub(handler))
    assert [item.name for item in source.iter_files()] == ["a.mp3"]


def test_client_errors_are_reported_without_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("audioscan.sources.gdrive.time.sleep", lambda _seconds: None)
    stub = DriveStub(
        lambda request, params: httpx.Response(400, json={"error": {"message": "bad query"}})
    )
    source, _auth = make_source(stub)
    with pytest.raises(DriveError, match="bad query"):
        list(source.iter_files())
    assert len(stub.list_requests) == 1


def test_single_file_lookup_failure_is_reported() -> None:
    stub = DriveStub(
        lambda request, params: httpx.Response(404, json={"error": {"message": "not found"}})
    )
    source, _auth = make_source(stub, file_id="missing")
    with pytest.raises(DriveError, match="not found"):
        list(source.iter_files())


def test_media_error_without_a_json_body_is_reported() -> None:
    stub = DriveStub(lambda request, params: httpx.Response(503, content=b"<html>oops</html>"))
    source, _auth = make_source(stub)
    with pytest.raises(DriveError, match="503"):
        list(source.iter_files())


def test_reading_past_the_end_returns_nothing() -> None:
    data = b"x" * 100
    stub = DriveStub(
        lambda request, params: listing([{"id": "f", "name": "a.mp3", "size": "100"}]),
        media=data,
    )
    source, _auth = make_source(stub, block_size=64)
    item = next(iter(source.iter_files()))

    reader = source.open(item)
    try:
        reader.seek(500)
        assert reader.read(10) == b""
        assert reader.read() == b""
    finally:
        reader.close()


def test_source_owns_and_closes_its_client() -> None:
    source = DriveSource(auth=FakeAuth())
    assert not source._client.is_closed
    source.close()
    assert source._client.is_closed


def test_borrowed_client_is_not_closed() -> None:
    client = httpx.Client(transport=httpx.MockTransport(DriveStub()))
    source = DriveSource(auth=FakeAuth(), client=client)
    source.close()
    assert not client.is_closed
    client.close()


# --------------------------------------------------------------------------- #
# auth and failures
# --------------------------------------------------------------------------- #
def test_expired_token_is_refreshed_and_the_request_retried() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
        return listing([{"id": "1", "name": "a.mp3"}])

    source, auth = make_source(DriveStub(handler))
    assert [item.name for item in source.iter_files()] == ["a.mp3"]
    assert auth.refreshes == 1


def test_api_error_raises_drive_error_with_the_message() -> None:
    stub = DriveStub(
        lambda request, params: httpx.Response(404, json={"error": {"message": "File not found"}})
    )
    source, _auth = make_source(stub)
    with pytest.raises(DriveError, match="File not found"):
        list(source.iter_files())


def test_unexpected_payload_raises_drive_error() -> None:
    stub = DriveStub(lambda request, params: httpx.Response(200, json=["not", "a", "dict"]))
    source, _auth = make_source(stub)
    with pytest.raises(DriveError):
        list(source.iter_files())


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"type": "service_account"}, "service_account"),
        ({"type": "authorized_user"}, "authorized_user"),
        ({"installed": {"client_id": "x"}}, "client_secrets"),
        ({"web": {"client_id": "x"}}, "client_secrets"),
    ],
)
def test_credential_kind_detection(tmp_path: Path, payload: dict[str, Any], expected: str) -> None:
    path = tmp_path / "creds.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert credential_kind(path) == expected


def test_unknown_credential_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"type": "mystery"}), encoding="utf-8")
    with pytest.raises(DriveError):
        credential_kind(path)


def test_missing_credential_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DriveError):
        credential_kind(tmp_path / "nope.json")


def test_describe_reports_paths_without_touching_the_network(tmp_path: Path) -> None:
    token = tmp_path / "token.json"
    auth = DriveAuth(credentials_path=str(tmp_path / "sa.json"), token_path=token)
    description = auth.describe()
    assert description["credentials_path"] is not None
    assert description["token_path"] == str(token)
    assert description["token_cached"] is False


def test_logout_removes_cached_token(tmp_path: Path) -> None:
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    auth = DriveAuth(token_path=token)
    assert auth.logout() is True
    assert not token.exists()
    assert auth.logout() is False


def test_missing_credentials_path_raises_on_use(tmp_path: Path) -> None:
    auth = DriveAuth(credentials_path=str(tmp_path / "gone.json"))
    with pytest.raises(DriveError, match="not found"):
        auth.credentials()
