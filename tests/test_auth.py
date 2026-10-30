from __future__ import annotations

import os
from pathlib import Path
import stat
import json

import pytest

from flin_ga4_mcp.config import Settings
from flin_ga4_mcp import auth


class FakeCredentials:
    def __init__(
        self,
        *,
        valid: bool,
        expired: bool = False,
        refresh_token: str | None = None,
        payload: dict[str, str] | None = None,
    ) -> None:
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.payload = payload or {"token": "serialized"}
        self.refresh_calls = 0

    def refresh(self, request: object) -> None:
        self.refresh_calls += 1
        self.valid = True
        self.expired = False

    def to_json(self) -> str:
        return json.dumps(self.payload)


def _settings(token_path: Path) -> Settings:
    return Settings(
        client_id="client-id",
        client_secret="client-secret",
        default_property_id="123456789",
        token_path=token_path,
        token_dir=token_path.parent,
        oauth_port=0,
    )


def test_get_credentials_loads_valid_credentials_from_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token":"cached"}', encoding="utf-8")
    credentials = FakeCredentials(valid=True)

    monkeypatch.setattr(
        auth,
        "_build_credentials_from_info",
        lambda info, scopes: credentials,
    )

    result = auth.get_credentials(settings=_settings(token_path), interactive=False)

    assert result is credentials


def test_get_credentials_loads_named_account_credentials_from_token_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_token_path = tmp_path / "token.json"
    named_token_path = tmp_path / "work.json"
    default_token_path.write_text('{"token":"default"}', encoding="utf-8")
    named_token_path.write_text('{"token":"work"}', encoding="utf-8")
    credentials = FakeCredentials(valid=True)
    captured: dict[str, object] = {}

    def fake_build(info: dict[str, object], scopes: tuple[str, ...]) -> FakeCredentials:
        captured["info"] = info
        return credentials

    monkeypatch.setattr(auth, "_build_credentials_from_info", fake_build)

    result = auth.get_credentials(
        settings=_settings(default_token_path),
        account="work",
        interactive=False,
    )

    assert result is credentials
    assert captured["info"] == {"token": "work"}


def test_get_credentials_refreshes_expired_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token":"expired"}', encoding="utf-8")
    credentials = FakeCredentials(
        valid=False,
        expired=True,
        refresh_token="refresh-token",
        payload={"token": "refreshed"},
    )

    monkeypatch.setattr(
        auth,
        "_build_credentials_from_info",
        lambda info, scopes: credentials,
    )
    monkeypatch.setattr(auth, "_refresh_credentials", lambda creds: creds.refresh(None))

    result = auth.get_credentials(settings=_settings(token_path), interactive=False)

    assert result is credentials
    assert credentials.refresh_calls == 1
    assert json.loads(token_path.read_text(encoding="utf-8")) == {"token": "refreshed"}


def test_get_credentials_requires_interactive_auth_when_token_file_is_missing(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "missing-token.json"

    with pytest.raises(auth.AuthenticationRequiredError):
        auth.get_credentials(settings=_settings(token_path), interactive=False)


def test_get_credentials_raises_clean_error_for_corrupted_token_file(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(auth.AuthenticationError):
        auth.get_credentials(settings=_settings(token_path), interactive=False)


def test_get_credentials_runs_installed_app_flow_when_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "missing-token.json"
    credentials = FakeCredentials(valid=True, payload={"token": "interactive"})

    monkeypatch.setattr(
        auth,
        "_run_installed_app_flow",
        lambda settings: credentials,
    )

    result = auth.get_credentials(settings=_settings(token_path), interactive=True)

    assert result is credentials
    assert json.loads(token_path.read_text(encoding="utf-8")) == {"token": "interactive"}


def test_save_credentials_writes_private_file_and_directory_permissions(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "tokens" / "token.json"
    credentials = FakeCredentials(
        valid=True,
        payload={
            "token": "interactive",
            "refresh_token": "refresh-token",
            "client_id": "client-id",
            "client_secret": "client-secret",
        },
    )

    auth.save_credentials(credentials, token_path)

    assert json.loads(token_path.read_text(encoding="utf-8")) == credentials.payload
    if os.name != "nt":
        assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_save_credentials_preserves_existing_directory_permissions(tmp_path: Path) -> None:
    token_dir = tmp_path / "existing-directory"
    token_dir.mkdir()
    token_dir.chmod(0o755)
    token_path = token_dir / "token.json"

    auth.save_credentials(FakeCredentials(valid=True), token_path)

    assert stat.S_IMODE(token_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
@pytest.mark.parametrize("mode", [0o777, 0o770, 0o702])
def test_save_credentials_rejects_shared_writable_directory_without_changing_it(
    tmp_path: Path, mode: int,
) -> None:
    token_dir = tmp_path / "shared-directory"
    token_dir.mkdir()
    token_dir.chmod(mode)
    token_path = token_dir / "token.json"
    token_path.write_text('{"token":"original"}', encoding="utf-8")

    with pytest.raises(auth.AuthenticationError, match="writable by other users"):
        auth.save_credentials(FakeCredentials(valid=True), token_path)

    assert stat.S_IMODE(token_dir.stat().st_mode) == mode
    assert json.loads(token_path.read_text(encoding="utf-8")) == {"token": "original"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership")
def test_save_credentials_rejects_directory_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = tmp_path.stat().st_uid
    monkeypatch.setattr(auth.os, "getuid", lambda: owner + 1)

    with pytest.raises(auth.AuthenticationError, match="owned by the current user"):
        auth.save_credentials(FakeCredentials(valid=True), tmp_path / "token.json")

    assert not (tmp_path / "token.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_save_credentials_rejects_symlink_directory_without_changing_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(auth.AuthenticationError, match="symlinked directory"):
        auth.save_credentials(FakeCredentials(valid=True), link / "token.json")

    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not (target / "token.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_save_credentials_rejects_symlink_token_paths(tmp_path: Path) -> None:
    token_target = tmp_path / "outside-token.json"
    token_target.write_text('{"token":"original"}', encoding="utf-8")
    token_path = tmp_path / "token.json"
    token_path.symlink_to(token_target)
    credentials = FakeCredentials(valid=True, payload={"token": "replacement"})

    with pytest.raises(auth.AuthenticationError, match="symlink"):
        auth.save_credentials(credentials, token_path)

    assert json.loads(token_target.read_text(encoding="utf-8")) == {"token": "original"}


def test_run_installed_app_flow_suppresses_stdout_authorization_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import google_auth_oauthlib.flow as oauth_flow

    captured: dict[str, object] = {}

    class FakeFlow:
        def run_local_server(self, **kwargs: object) -> str:
            captured["kwargs"] = kwargs
            return "credentials"

    class FakeInstalledAppFlow:
        @staticmethod
        def from_client_config(client_config: dict[str, object], scopes: tuple[str, ...]) -> FakeFlow:
            captured["client_config"] = client_config
            captured["scopes"] = scopes
            return FakeFlow()

    monkeypatch.setattr(oauth_flow, "InstalledAppFlow", FakeInstalledAppFlow)

    result = auth._run_installed_app_flow(_settings(tmp_path / "token.json"))

    assert result == "credentials"
    assert captured["scopes"] == auth.GA4_SCOPES
    assert captured["kwargs"]["prompt"] == "consent select_account"
    assert captured["kwargs"]["authorization_prompt_message"] == ""


def test_describe_auth_state_omits_token_path_when_oauth_is_required(
    tmp_path: Path,
) -> None:
    state = auth.describe_auth_state(settings=_settings(tmp_path / "missing-token.json"))

    assert state == {
        "status": "oauth_required",
        "has_token_file": False,
    }


def test_describe_auth_state_uses_named_account_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_token_path = tmp_path / "token.json"
    named_token_path = tmp_path / "work.json"
    named_token_path.write_text('{"token":"work"}', encoding="utf-8")

    monkeypatch.setattr(
        auth,
        "_build_credentials_from_info",
        lambda info, scopes: FakeCredentials(valid=True),
    )

    state = auth.describe_auth_state(
        settings=_settings(default_token_path),
        account="work",
    )

    assert state == {
        "status": "ready",
        "has_token_file": True,
    }


def test_describe_auth_state_omits_token_path_from_invalid_token_errors(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{not-json", encoding="utf-8")

    state = auth.describe_auth_state(settings=_settings(token_path))

    assert state["status"] == "invalid_token_file"
    assert state["has_token_file"] is True
    assert "token_path" not in state
    assert str(token_path) not in state["error"]


@pytest.mark.parametrize('payload', ['[]', 'null', '"text"', '123'])
def test_token_file_requires_object(tmp_path, payload):
    path = tmp_path / 'token.json'
    path.write_text(payload)
    with pytest.raises(auth.AuthenticationError, match='JSON object'):
        auth._load_token_info(path)
