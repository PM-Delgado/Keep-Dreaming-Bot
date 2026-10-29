"""Rank (tier) icons: bundled PNGs uploaded once as application emojis, like the role icons.

The images are the ranked emblems of the game client as published by Community Dragon, cropped
to square 128 px icons (see `docs/riot-policy.md`: a known, accepted policy gap). A tier without an
uploaded icon is shown by the caller as a generic colour dot, so a missing emoji never leaves a
bare line.
"""

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path

from bot.db import Database
from bot.features.icons.upload import IconUploadError, UploadIcon
from bot.features.ratings.rank import ALL_TIERS

log = logging.getLogger(__name__)

TIER_DIR = Path(__file__).parent / "tiers"
MAX_FAILURES = 3  # consecutive failed uploads before a sync gives up until the next run


def emoji_name(tier: str) -> str:
    return f"tier_{tier.lower()}"


class TierEmojiRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def all(self) -> dict[str, tuple[int, str]]:
        async with self.db.conn.execute(
            "SELECT tier, emoji_id, emoji_name FROM tier_emojis"
        ) as cur:
            return {r[0]: (r[1], r[2]) for r in await cur.fetchall()}

    async def save(self, tier: str, emoji_id: int, name: str) -> None:
        await self.db.conn.execute(
            "INSERT OR REPLACE INTO tier_emojis (tier, emoji_id, emoji_name) VALUES (?, ?, ?)",
            (tier, emoji_id, name),
        )
        await self.db.conn.commit()


class TierIconService:
    def __init__(
        self, repo: TierEmojiRepository, enabled: bool = True, tier_dir: Path = TIER_DIR
    ) -> None:
        self.repo = repo
        self.enabled = enabled
        self.tier_dir = tier_dir
        self._markup: dict[str, str] = {}

    async def load(self) -> None:
        self._markup = {
            tier: f"<:{name}:{emoji_id}>"
            for tier, (emoji_id, name) in (await self.repo.all()).items()
        }

    def markup(self) -> dict[str, str]:
        """Tier (upper-case, as Riot sends it) -> emoji markup; empty when icons are off."""
        return dict(self._markup) if self.enabled else {}

    async def sync(self, existing: Mapping[str, int], upload: UploadIcon) -> int:
        """Upload the icons that are missing (adopting emojis that already exist by name).

        Same rules as `RoleIconService.sync`: a saved icon only counts if its id is among
        `existing`, a failure skips that tier and three in a row stop the run. Returns how many
        were uploaded.
        """
        if not self.enabled:
            return 0
        done = await self.repo.all()
        live = set(existing.values())
        uploaded = failures = 0
        for tier in ALL_TIERS:
            name = emoji_name(tier)
            if tier in done and done[tier][0] in live:
                continue
            if name in existing:
                await self.repo.save(tier, existing[name], name)
                continue
            try:
                image = await asyncio.to_thread((self.tier_dir / f"{tier}.png").read_bytes)
                emoji_id = await upload(name, image)
            except (OSError, IconUploadError) as exc:
                failures += 1
                log.warning("Could not upload the %s icon: %s", tier, exc)
                if failures >= MAX_FAILURES:
                    break
                continue
            failures = 0
            await self.repo.save(tier, emoji_id, name)
            uploaded += 1
        await self.load()
        if uploaded:
            log.info("Uploaded %d rank icons", uploaded)
        return uploaded
