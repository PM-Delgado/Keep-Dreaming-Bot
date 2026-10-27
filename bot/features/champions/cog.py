"""Keeps the champion list current: seeded at load, refreshed from Data Dragon at start and daily.

After each refresh the champion icons that are not yet application emojis are uploaded (a few
minutes the first time, then only new champions). No commands: the stats commands live in the
lobby cog (next to /match) and use `service`.
"""

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from bot.features.champions.repository import ChampionRepository
from bot.features.champions.service import ChampionService, IconUploadError

if TYPE_CHECKING:
    from bot.app import KDBot

log = logging.getLogger(__name__)

REFRESH_HOURS = 24


class Champions(commands.Cog):
    def __init__(self, bot: "KDBot", service: ChampionService) -> None:
        self.bot = bot
        self.service = service

    async def cog_load(self) -> None:
        await self.service.start()
        self.refresher.start()

    async def cog_unload(self) -> None:
        self.refresher.cancel()

    @tasks.loop(hours=REFRESH_HOURS)
    async def refresher(self) -> None:
        await self.service.try_refresh()
        await self.sync_icons()

    @refresher.before_loop
    async def _before_refresher(self) -> None:
        await self.bot.wait_until_ready()

    async def sync_icons(self) -> None:
        """Best effort: whatever fails is retried at the next refresh."""
        if not self.service.icons:
            return
        try:
            existing = {e.name: e.id for e in await self.bot.fetch_application_emojis()}

            async def upload(name: str, image: bytes) -> int:
                try:
                    return (await self.bot.create_application_emoji(name=name, image=image)).id
                except discord.HTTPException as exc:
                    raise IconUploadError(f"Discord answered {exc.status}") from exc

            await self.service.sync_icons(existing, upload)
        except Exception:
            log.exception("Champion icon sync failed")


async def setup(bot: "KDBot") -> None:
    service = ChampionService(ChampionRepository(bot.db), icons=bot.settings.champion_icons)
    await bot.add_cog(Champions(bot, service))
