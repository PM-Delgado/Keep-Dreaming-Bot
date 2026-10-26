"""Environment configuration, read once at startup."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from bot.features.riot.platforms import normalise_platform


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    token: str
    guild_id: int
    assets_url: str
    db_path: Path
    admin_ids: frozenset[int] = frozenset()  # Discord user IDs allowed to run admin-only commands
    # Optional: without a key the Riot integration is disabled and everything works manually.
    riot_api_key: str | None = field(default=None, repr=False)
    riot_default_platform: str = "euw1"


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

    admin_ids = _parse_admin_ids(env.get("ADMIN_USER_IDS", ""))
    riot_api_key = env.get("RIOT_API_KEY", "").strip() or None
    try:
        riot_platform = normalise_platform(env.get("RIOT_DEFAULT_PLATFORM", "euw1"))
    except ValueError:
        raise ConfigError("RIOT_DEFAULT_PLATFORM is not a Riot platform ID (e.g. euw1).") from None

    return Settings(
        token=token,
        guild_id=guild_id,
        assets_url=assets_url,
        db_path=Path(env.get("DB_PATH", "data/kdbot.sqlite3")),
        admin_ids=admin_ids,
        riot_api_key=riot_api_key,
        riot_default_platform=riot_platform,
    )


def _parse_admin_ids(raw: str) -> frozenset[int]:
    """Parse a comma-separated list of Discord user IDs; empty means nobody is an admin."""
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            raise ConfigError(
                f"ADMIN_USER_IDS must be comma-separated numeric user IDs (got {part!r})."
            ) from None
    return frozenset(ids)
