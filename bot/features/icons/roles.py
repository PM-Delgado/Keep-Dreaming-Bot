"""Role icons (Top, Jungle, Mid, ADC, Support): bundled PNGs uploaded once as application emojis.

Everything that shows a role asks `role_prefix` for the text to put in front of a name: the icon
when it is uploaded, otherwise the role's name, so a missing emoji never leaves a bare line.
"""

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path

from bot.db import Database
from bot.features.icons.upload import IconUploadError, UploadIcon
from bot.features.players.proficiency import ROLE_NAMES_PT, ROLES

log = logging.getLogger(__name__)

ROLE_DIR = Path(__file__).parent / "roles"
MAX_FAILURES = 3  # consecutive failed uploads before a sync gives up until the next run


def emoji_name(role: str) -> str:
    return f"role_{role.lower()}"


def role_prefix(role: str, icons: Mapping[str, str] | None) -> str:
    """`"<icon> "` when the role's icon is available, else `"**Top** — "`."""
    icon = (icons or {}).get(role)
    return f"{icon} " if icon else f"**{ROLE_NAMES_PT[role]}** — "


def role_label(role: str, icons: Mapping[str, str] | None) -> str:
    """The icon alone when available, else the role's name (for use inside a sentence)."""
    return (icons or {}).get(role) or ROLE_NAMES_PT[role]


class RoleEmojiRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def all(self) -> dict[str, tuple[int, str]]:
        async with self.db.conn.execute(
            "SELECT role, emoji_id, emoji_name FROM role_emojis"
        ) as cur:
            return {r[0]: (r[1], r[2]) for r in await cur.fetchall()}

    async def save(self, role: str, emoji_id: int, name: str) -> None:
        await self.db.conn.execute(
            "INSERT OR REPLACE INTO role_emojis (role, emoji_id, emoji_name) VALUES (?, ?, ?)",
            (role, emoji_id, name),
        )
        await self.db.conn.commit()


class RoleIconService:
    def __init__(
        self, repo: RoleEmojiRepository, enabled: bool = True, role_dir: Path = ROLE_DIR
    ) -> None:
        self.repo = repo
        self.enabled = enabled
        self.role_dir = role_dir
        self._markup: dict[str, str] = {}

    async def load(self) -> None:
        self._markup = {
            role: f"<:{name}:{emoji_id}>"
            for role, (emoji_id, name) in (await self.repo.all()).items()
        }

    def markup(self) -> dict[str, str]:
        """Role -> emoji markup for the uploaded icons (empty when icons are off)."""
        return dict(self._markup) if self.enabled else {}

    async def sync(self, existing: Mapping[str, int], upload: UploadIcon) -> int:
        """Upload the icons that are missing (adopting emojis that already exist by name).

        Returns how many were uploaded. A failure skips that role; three in a row stop the run
        and the next one resumes.
        """
        if not self.enabled:
            return 0
        done = await self.repo.all()
        uploaded = failures = 0
        for role in ROLES:
            name = emoji_name(role)
            if role in done:
                continue
            if name in existing:
                await self.repo.save(role, existing[name], name)
                continue
            try:
                image = await asyncio.to_thread((self.role_dir / f"{role}.png").read_bytes)
                emoji_id = await upload(name, image)
            except (OSError, IconUploadError) as exc:
                failures += 1
                log.warning("Could not upload the %s icon: %s", role, exc)
                if failures >= MAX_FAILURES:
                    break
                continue
            failures = 0
            await self.repo.save(role, emoji_id, name)
            uploaded += 1
        await self.load()
        if uploaded:
            log.info("Uploaded %d role icons", uploaded)
        return uploaded
