"""Environment configuration, read once at startup."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    token: str
    guild_id: int
    assets_url: str
    db_path: Path


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `env` (defaults to the process environment plus .env)."""
    if env is None:
        load_dotenv()
        env = os.environ

    token = env.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigError("DISCORD_TOKEN is not set; see .env.example.")

    try:
        guild_id = int(env.get("GUILD_ID", ""))
    except ValueError:
        raise ConfigError("GUILD_ID is missing or not a number; see .env.example.") from None

    assets_url = env.get("ASSETS_URL", "").strip()
    if not assets_url:
        raise ConfigError("ASSETS_URL is not set; see .env.example.")
    if not assets_url.endswith("/"):
        assets_url += "/"

    return Settings(
        token=token,
        guild_id=guild_id,
        assets_url=assets_url,
        db_path=Path(env.get("DB_PATH", "data/kdbot.sqlite3")),
    )
