from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
import os
import re
import sys


REQUIRED_ENV_VARS = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
)
ACCOUNT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


def missing_required_env_vars(env: Mapping[str, str] | None = None) -> list[str]:
    source = os.environ if env is None else env
    return [key for key in REQUIRED_ENV_VARS if not source.get(key, "").strip()]


def default_token_path(
    *, home: Path | None = None, platform_name: str | None = None
) -> Path:
    resolved_home = home or Path.home()
    resolved_platform = platform_name or sys.platform

    if resolved_platform == "darwin":
        return (
            resolved_home
            / "Library"
            / "Application Support"
            / "flin-ga4-mcp"
            / "token.json"
        )

    if resolved_platform.startswith("win"):
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / "flin-ga4-mcp" / "token.json"
        return (
            resolved_home / "AppData" / "Roaming" / "flin-ga4-mcp" / "token.json"
        )

    return resolved_home / ".config" / "flin-ga4-mcp" / "token.json"


def _parse_oauth_port(raw_value: str | None) -> int:
    if raw_value is None or not raw_value.strip():
        return 0

    try:
        parsed = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(
            f"Invalid GOOGLE_GA4_OAUTH_PORT value: {raw_value!r}."
        ) from exc

    if parsed < 0 or parsed > 65535:
        raise ConfigurationError(
            f"GOOGLE_GA4_OAUTH_PORT must be between 0 and 65535, got {parsed}."
        )

    return parsed


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    default_property_id: str | None
    token_path: Path
    token_dir: Path
    oauth_port: int


def resolve_token_path(*, settings: Settings, account: str | None = None) -> Path:
    if account is None:
        return settings.token_path

    normalized_account = account.strip()
    if (
        not normalized_account
        or normalized_account in {".", ".."}
        or not ACCOUNT_NAME_PATTERN.fullmatch(normalized_account)
    ):
        raise ConfigurationError(
            "Invalid account name. Use only letters, numbers, underscore, hyphen, and dot."
        )

    token_path = settings.token_dir / f"{normalized_account}.json"
    # Compare case-insensitively as well: macOS and Windows commonly use
    # case-insensitive filesystems, including before either file exists.
    aliases_default = (
        str(token_path.resolve()).casefold()
        == str(settings.token_path.resolve()).casefold()
    )
    if not aliases_default and token_path.exists() and settings.token_path.exists():
        aliases_default = token_path.samefile(settings.token_path)
    if aliases_default:
        raise ConfigurationError(
            "This account name conflicts with the default token file. "
            "Omit account to use the default account, or choose a different name."
        )

    return token_path


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    missing = missing_required_env_vars(source)
    if missing:
        missing_fmt = ", ".join(missing)
        raise ConfigurationError(
            "Missing required environment variables: "
            f"{missing_fmt}. See README.md for setup instructions."
        )

    token_override = source.get("GOOGLE_GA4_TOKEN_PATH")
    token_path = (
        Path(token_override).expanduser()
        if token_override
        else default_token_path()
    )
    token_dir_override = source.get("GOOGLE_GA4_TOKEN_DIR")
    token_dir = (
        Path(token_dir_override).expanduser()
        if token_dir_override
        else token_path.parent
    )

    default_property_id = source.get("GOOGLE_GA4_PROPERTY_ID")
    default_property_id = default_property_id.strip() if default_property_id else None

    return Settings(
        client_id=source["GOOGLE_CLIENT_ID"].strip(),
        client_secret=source["GOOGLE_CLIENT_SECRET"].strip(),
        default_property_id=default_property_id,
        token_path=token_path,
        token_dir=token_dir,
        oauth_port=_parse_oauth_port(source.get("GOOGLE_GA4_OAUTH_PORT")),
    )
