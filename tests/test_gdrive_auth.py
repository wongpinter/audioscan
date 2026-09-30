"""Credential resolution for Google Drive.

Only the google-auth seams are patched, so this exercises ``DriveAuth``'s real
logic: which loader is chosen for which file, when a token is refreshed, and how
failures are reported. Skips cleanly when the ``gdrive`` extra is not installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from audioscan.sources.gdrive import DriveAuth, DriveError

pytest.importorskip("google.auth")


class FakeCredential:
    """Stand-in for a google-auth credential."""

    def __init__(self, token: str, *, valid: bool = True) -> None:
        self.token = token
        self.valid = valid
        self.refreshed = 0

    def refresh(self, request: Any) -> None:
        self.refreshed += 1
        self.valid = True
        self.token = "refreshed-token"

    def to_json(self) -> str:
        return json.dumps({"token": self.token, "type": "authorized_user"})


@pytest.fixture
def fake_google(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch google-auth's credential factories and transport construction."""
    import google.auth
    import google.auth.transport.requests as transport
    import google.oauth2.credentials as oauth_credentials
    import google.oauth2.service_account as service_account

    calls: dict[str, Any] = {"valid": True}

    def make(token: str) -> FakeCredential:
        return FakeCredential(token, valid=bool(calls["valid"]))

    def fake_default(scopes: Any = None) -> tuple[FakeCredential, str]:
        calls["adc_scopes"] = tuple(scopes or ())
        if calls.get("fail_adc"):
            raise RuntimeError("no application default credentials")
        return make("adc-token"), "test-project"

    def from_service_account_file(path: str, scopes: Any = None) -> FakeCredential:
        calls["service_account"] = (str(path), tuple(scopes or ()))
        return make("sa-token")

    def from_authorized_user_file(path: str, scopes: Any = None) -> FakeCredential:
        calls["authorized_user"] = (str(path), tuple(scopes or ()))
        return make("user-token")

    class FakeRequest:
        def __init__(self) -> None:
            calls["request_adapter"] = True

        def __call__(self, *args: Any, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(google.auth, "default", fake_default, raising=False)
    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_file",
        staticmethod(from_service_account_file),
    )
    monkeypatch.setattr(
        oauth_credentials.Credentials,
        "from_authorized_user_file",
        staticmethod(from_authorized_user_file),
    )
    monkeypatch.setattr(transport, "Request", FakeRequest)
    return calls


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# credential selection
# --------------------------------------------------------------------------- #
def test_service_account_file_is_loaded(tmp_path: Path, fake_google: dict[str, Any]) -> None:
    path = write_json(tmp_path / "sa.json", {"type": "service_account"})
    auth = DriveAuth(credentials_path=path, token_path=tmp_path / "token.json")

    assert auth.headers() == {"Authorization": "Bearer sa-token"}
    assert fake_google["service_account"][0] == str(path)
    assert fake_google["service_account"][1] == ("https://www.googleapis.com/auth/drive.readonly",)


def test_authorized_user_file_is_loaded(tmp_path: Path, fake_google: dict[str, Any]) -> None:
    path = write_json(tmp_path / "user.json", {"type": "authorized_user"})
    auth = DriveAuth(credentials_path=path, token_path=tmp_path / "token.json")

    assert auth.headers() == {"Authorization": "Bearer user-token"}
    assert fake_google["authorized_user"][0] == str(path)


def test_cached_token_is_used_when_no_file_is_given(
    tmp_path: Path, fake_google: dict[str, Any]
) -> None:
    token = write_json(tmp_path / "token.json", {"type": "authorized_user"})
    auth = DriveAuth(token_path=token)

    assert auth.headers() == {"Authorization": "Bearer user-token"}
    assert fake_google["authorized_user"][0] == str(token)
    assert "service_account" not in fake_google


def test_environment_variable_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_google: dict[str, Any]
) -> None:
    path = write_json(tmp_path / "env.json", {"type": "service_account"})
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    auth = DriveAuth(token_path=tmp_path / "token.json")

    assert auth.headers() == {"Authorization": "Bearer sa-token"}


def test_application_default_credentials_are_the_last_resort(
    tmp_path: Path, fake_google: dict[str, Any]
) -> None:
    auth = DriveAuth(token_path=tmp_path / "absent.json")

    assert auth.headers() == {"Authorization": "Bearer adc-token"}
    assert fake_google["adc_scopes"] == ("https://www.googleapis.com/auth/drive.readonly",)


def test_missing_credentials_produce_an_actionable_error(
    tmp_path: Path, fake_google: dict[str, Any]
) -> None:
    fake_google["fail_adc"] = True
    auth = DriveAuth(token_path=tmp_path / "absent.json")

    with pytest.raises(DriveError) as excinfo:
        auth.headers()
    message = str(excinfo.value)
    assert "ruangdengar-scan auth" in message
    assert "GOOGLE_APPLICATION_CREDENTIALS" in message


def test_missing_credentials_file_raises(tmp_path: Path, fake_google: dict[str, Any]) -> None:
    auth = DriveAuth(credentials_path=tmp_path / "gone.json")
    with pytest.raises(DriveError, match="not found"):
        auth.credentials()


# --------------------------------------------------------------------------- #
# refreshing
# --------------------------------------------------------------------------- #
def test_expired_credentials_are_refreshed_before_use(
    tmp_path: Path, fake_google: dict[str, Any]
) -> None:
    fake_google["valid"] = False
    path = write_json(tmp_path / "sa.json", {"type": "service_account"})
    auth = DriveAuth(credentials_path=path, token_path=tmp_path / "token.json")

    assert auth.headers() == {"Authorization": "Bearer refreshed-token"}
    assert fake_google["request_adapter"] is True


def test_refresh_forces_a_new_token(tmp_path: Path, fake_google: dict[str, Any]) -> None:
    path = write_json(tmp_path / "sa.json", {"type": "service_account"})
    auth = DriveAuth(credentials_path=path, token_path=tmp_path / "token.json")

    assert auth.headers() == {"Authorization": "Bearer sa-token"}
    auth.refresh()
    assert auth.headers() == {"Authorization": "Bearer refreshed-token"}


def test_credentials_are_loaded_only_once(tmp_path: Path, fake_google: dict[str, Any]) -> None:
    path = write_json(tmp_path / "sa.json", {"type": "service_account"})
    auth = DriveAuth(credentials_path=path, token_path=tmp_path / "token.json")

    auth.headers()
    auth.headers()
    assert fake_google["service_account"][0] == str(path)


def test_request_adapter_supports_https() -> None:
    """OAuth refresh uses a transport that accepts Google's HTTPS token URL."""
    from audioscan.sources.gdrive import _request_adapter

    assert type(_request_adapter()).__module__ == "google.auth.transport.requests"


# --------------------------------------------------------------------------- #
# interactive login
# --------------------------------------------------------------------------- #
def test_login_retries_without_browser_when_browser_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_google: dict[str, Any]
) -> None:
    flow_module = pytest.importorskip("google_auth_oauthlib.flow")

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path: str, scopes: list[str]) -> FakeFlow:
            fake_google["flow"] = (str(path), tuple(scopes))
            return cls()

        def run_local_server(self, port: int = 0, open_browser: bool = True) -> FakeCredential:
            fake_google.setdefault("attempts", []).append(open_browser)
            if open_browser:
                from webbrowser import Error as BrowserError

                raise BrowserError("could not locate runnable browser")
            fake_google["port"] = port
            return FakeCredential("browser-token")

    monkeypatch.setattr(flow_module, "InstalledAppFlow", FakeFlow)
    secrets = write_json(tmp_path / "client_secrets.json", {"installed": {"client_id": "x"}})
    token = tmp_path / "nested" / "token.json"
    auth = DriveAuth(token_path=token)

    auth.login(client_secrets=secrets)

    assert json.loads(token.read_text())["token"] == "browser-token"
    assert auth.describe()["token_cached"] is True
    assert fake_google["attempts"] == [True, False]
    assert fake_google["port"] == 0
    assert fake_google["flow"][0] == str(secrets)
    # A later instance loads the cached token as authorized-user credentials.
    cached_auth = DriveAuth(token_path=token)
    assert cached_auth.headers() == {"Authorization": "Bearer user-token"}
    assert fake_google["authorized_user"][0] == str(token)


def test_login_requires_a_client_secrets_file(tmp_path: Path, fake_google: dict[str, Any]) -> None:
    auth = DriveAuth(token_path=tmp_path / "token.json")
    with pytest.raises(DriveError, match="client_secrets.json"):
        auth.login()


def test_login_rejects_a_service_account_file(tmp_path: Path, fake_google: dict[str, Any]) -> None:
    path = write_json(tmp_path / "sa.json", {"type": "service_account"})
    auth = DriveAuth(token_path=tmp_path / "token.json")
    with pytest.raises(DriveError, match="client secrets"):
        auth.login(client_secrets=path)


def test_login_is_blocked_when_interactive_is_not_allowed(
    tmp_path: Path, fake_google: dict[str, Any]
) -> None:
    secrets = write_json(tmp_path / "client_secrets.json", {"installed": {"client_id": "x"}})
    auth = DriveAuth(credentials_path=secrets, token_path=tmp_path / "token.json")

    with pytest.raises(DriveError, match="interactive sign-in required"):
        auth.headers()


# --------------------------------------------------------------------------- #
# status reporting
# --------------------------------------------------------------------------- #
def test_describe_reports_a_missing_credentials_file(tmp_path: Path) -> None:
    auth = DriveAuth(credentials_path=tmp_path / "gone.json", token_path=tmp_path / "t.json")
    description = auth.describe()

    assert description["problem"]
    assert "not found" in str(description["problem"])
    assert description["token_cached"] is False
