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
    db_path: Path
    admin_ids: frozenset[int] = frozenset()  # Discord user IDs allowed to run admin-only commands
    # Optional: without a key the Riot integration is disabled and everything works manually.
    riot_api_key: str | None = field(default=None, repr=False)
    riot_default_platform: str = "euw1"
    # Lobbies. Mock players are fake players for testing on a server with too few real people;
    # keep them off in production.
    mock_players: bool = False
    ready_check_seconds: int = 120
    # Champion and role icons (Riot images) shown as application emojis. The name is historical:
    # it also switches the role icons. On by default; off stops uploading and showing them.
    champion_icons: bool = True
    # Logging: console always; also a daily rotating file in `log_dir` (empty LOG_DIR = no file).
    log_level: str = "INFO"
    log_dir: Path | None = Path("logs")
    # Scrim lobbies: opened/scheduled only in this channel, and announced by tagging this role
    # (inside a spoiler). Unset (the default, e.g. a development server) = any channel, no tag.
    scrim_channel_id: int | None = None
    scrim_role_id: int | None = None


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

    admin_ids = _parse_admin_ids(env.get("ADMIN_USER_IDS", ""))
    riot_api_key = env.get("RIOT_API_KEY", "").strip() or None
    try:
        riot_platform = normalise_platform(env.get("RIOT_DEFAULT_PLATFORM", "euw1"))
    except ValueError:
        raise ConfigError("RIOT_DEFAULT_PLATFORM is not a Riot platform ID (e.g. euw1).") from None

    return Settings(
        token=token,
        guild_id=guild_id,
        db_path=Path(env.get("DB_PATH", "data/kdbot.sqlite3")),
        admin_ids=admin_ids,
        riot_api_key=riot_api_key,
        riot_default_platform=riot_platform,
        mock_players=env.get("ENABLE_MOCK_PLAYERS", "").strip().lower()
        in ("1", "true", "yes", "on"),
        ready_check_seconds=_parse_ready_seconds(env.get("READY_CHECK_SECONDS", "")),
        champion_icons=_truthy(env.get("CHAMPION_ICONS", "true")),
        log_level=_parse_log_level(env.get("LOG_LEVEL", "")),
        log_dir=_parse_log_dir(env.get("LOG_DIR", "logs")),
        scrim_channel_id=_parse_optional_id(env, "SCRIM_CHANNEL_ID"),
        scrim_role_id=_parse_optional_id(env, "SCRIM_ROLE_ID"),
    )


LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _parse_optional_id(env: Mapping[str, str], name: str) -> int | None:
    """A Discord ID from the environment; empty or unset means "not configured"."""
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a numeric Discord ID (got {raw!r}).") from None


def _parse_log_level(raw: str) -> str:
    level = raw.strip().upper() or "INFO"
    if level not in LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}.")
    return level


def _parse_log_dir(raw: str) -> Path | None:
    raw = raw.strip()
    return Path(raw) if raw else None


def _parse_ready_seconds(raw: str) -> int:
    raw = raw.strip()
    if not raw:
        return 120
    try:
        seconds = int(raw)
    except ValueError:
        raise ConfigError("READY_CHECK_SECONDS must be a whole number of seconds.") from None
    if not 10 <= seconds <= 900:
        raise ConfigError("READY_CHECK_SECONDS must be between 10 and 900.")
    return seconds


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


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")
