from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import os

import pytest

from flin_ga4_mcp.config import (
    ConfigurationError,
    default_token_path,
    load_settings,
    missing_required_env_vars,
    resolve_token_path,
)


def test_missing_required_env_vars_detects_all_missing() -> None:
    missing = missing_required_env_vars(env={})
    assert "GOOGLE_CLIENT_ID" in missing
    assert "GOOGLE_CLIENT_SECRET" in missing


def test_missing_required_env_vars_with_complete_env() -> None:
    env = {
        "GOOGLE_CLIENT_ID": "client-id",
        "GOOGLE_CLIENT_SECRET": "client-secret",
    }
    assert missing_required_env_vars(env=env) == []


def test_default_token_path_uses_macos_application_support() -> None:
    path = default_token_path(home=Path("/Users/tester"), platform_name="darwin")
    assert (
        path
        == Path(
            "/Users/tester/Library/Application Support/flin-ga4-mcp/token.json"
        )
    )


def test_default_token_path_uses_xdg_style_on_linux() -> None:
    path = default_token_path(home=Path("/home/tester"), platform_name="linux")
    assert (
        path
        == Path("/home/tester/.config/flin-ga4-mcp/token.json")
    )


def test_load_settings_uses_custom_token_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_GA4_TOKEN_PATH", "/tmp/custom-token.json")

    settings = load_settings()

    assert settings.token_path == Path("/tmp/custom-token.json")
    assert settings.token_dir == Path("/tmp")


def test_load_settings_uses_custom_token_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_GA4_TOKEN_DIR", "/tmp/ga4-tokens")

    settings = load_settings()

    assert settings.token_dir == Path("/tmp/ga4-tokens")


def test_load_settings_reads_optional_default_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_GA4_PROPERTY_ID", "123456789")

    settings = load_settings()

    assert settings.default_property_id == "123456789"


def test_resolve_token_path_uses_default_token_without_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_GA4_TOKEN_PATH", "/tmp/default-token.json")

    settings = load_settings()

    assert resolve_token_path(settings=settings, account=None) == Path(
        "/tmp/default-token.json"
    )


def test_resolve_token_path_uses_named_account_token_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_GA4_TOKEN_DIR", "/tmp/ga4-tokens")

    settings = load_settings()

    assert resolve_token_path(settings=settings, account="work") == Path(
        "/tmp/ga4-tokens/work.json"
    )


@pytest.mark.parametrize(
    ("filename", "account"),
    [("token.json", "token"), ("token.json", " TOKEN "), ("personal.json", "Personal")],
)
def test_named_accounts_cannot_alias_default_token(tmp_path, filename, account) -> None:
    settings = load_settings({
        "GOOGLE_CLIENT_ID": "client-id",
        "GOOGLE_CLIENT_SECRET": "client-secret",
        "GOOGLE_GA4_TOKEN_PATH": str(tmp_path / filename),
    })

    with pytest.raises(ConfigurationError, match="conflicts with the default"):
        resolve_token_path(settings=settings, account=account)


def test_named_account_can_use_same_filename_in_separate_directory(tmp_path) -> None:
    settings = load_settings({
        "GOOGLE_CLIENT_ID": "client-id",
        "GOOGLE_CLIENT_SECRET": "client-secret",
        "GOOGLE_GA4_TOKEN_PATH": str(tmp_path / "token.json"),
        "GOOGLE_GA4_TOKEN_DIR": str(tmp_path / "accounts"),
    })

    assert resolve_token_path(settings=settings, account="token") == (
        tmp_path / "accounts" / "token.json"
    )


@pytest.mark.skipif(os.name == "nt", reason="link semantics differ on Windows")
@pytest.mark.parametrize("link_type", ["symlink", "hardlink", "directory_symlink"])
def test_named_accounts_reject_filesystem_aliases_of_default_token(tmp_path, link_type) -> None:
    settings = load_settings({
        "GOOGLE_CLIENT_ID": "client-id",
        "GOOGLE_CLIENT_SECRET": "client-secret",
        "GOOGLE_GA4_TOKEN_PATH": str(tmp_path / "token.json"),
    })
    settings.token_path.write_text('{"token":"default"}', encoding="utf-8")
    account = "work"
    if link_type == "symlink":
        (tmp_path / "work.json").symlink_to(settings.token_path)
    elif link_type == "hardlink":
        os.link(settings.token_path, tmp_path / "work.json")
    else:
        (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        settings = replace(settings, token_dir=tmp_path / "alias")
        account = "token"

    with pytest.raises(ConfigurationError, match="conflicts with the default"):
        resolve_token_path(settings=settings, account=account)


@pytest.mark.parametrize("account", ["", "  ", "../work", "work/team", ".", ".."])
def test_resolve_token_path_rejects_unsafe_account_names(
    monkeypatch: pytest.MonkeyPatch,
    account: str,
) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    settings = load_settings()

    with pytest.raises(ConfigurationError):
        resolve_token_path(settings=settings, account=account)


def test_load_settings_rejects_invalid_oauth_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_GA4_OAUTH_PORT", "abc")

    with pytest.raises(ConfigurationError):
        load_settings()


def test_load_settings_caches_by_environment_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    first = load_settings()

    monkeypatch.setenv("GOOGLE_GA4_PROPERTY_ID", "987654321")
    second = load_settings()

    assert second.default_property_id == "987654321"
    assert first != second


@pytest.fixture(autouse=True)
def clear_related_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_GA4_PROPERTY_ID",
        "GOOGLE_GA4_TOKEN_PATH",
        "GOOGLE_GA4_TOKEN_DIR",
        "GOOGLE_GA4_OAUTH_PORT",
    ):
        monkeypatch.delenv(key, raising=False)

    os.environ.pop("GOOGLE_CLIENT_ID", None)
