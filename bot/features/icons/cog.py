"""Role icons: loaded from the database at start-up and uploaded (once) when missing.

No commands. Loaded before the cogs that show roles, which pick the service up with
`bot.get_cog("Icons").roles`.
"""

import logging
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

from bot.features.icons.roles import RoleEmojiRepository, RoleIconService
from bot.features.icons.tiers import TierEmojiRepository, TierIconService
from bot.features.icons.upload import application_emojis

if TYPE_CHECKING:
    from bot.app import KDBot

log = logging.getLogger(__name__)

SYNC_HOURS = 24


class Icons(commands.Cog):
    def __init__(self, bot: "KDBot", roles: RoleIconService, tiers: TierIconService) -> None:
        self.bot = bot
        self.roles = roles
        self.tiers = tiers

    async def cog_load(self) -> None:
        await self.roles.load()
        await self.tiers.load()
        self.syncer.start()

    async def cog_unload(self) -> None:
        self.syncer.cancel()

    @tasks.loop(hours=SYNC_HOURS)
    async def syncer(self) -> None:
        await self.sync()

    @syncer.before_loop
    async def _before_syncer(self) -> None:
        await self.bot.wait_until_ready()

    async def sync(self) -> None:
        """Best effort: whatever fails is retried at the next run."""
        if not self.roles.enabled:
            return
        try:
            existing, upload = await application_emojis(self.bot)
            await self.roles.sync(existing, upload)
            await self.tiers.sync(existing, upload)
        except Exception:
            log.exception("Role and rank icon sync failed")


async def setup(bot: "KDBot") -> None:
    enabled = bot.settings.champion_icons
    roles = RoleIconService(RoleEmojiRepository(bot.db), enabled=enabled)
    tiers = TierIconService(TierEmojiRepository(bot.db), enabled=enabled)
    await bot.add_cog(Icons(bot, roles, tiers))
