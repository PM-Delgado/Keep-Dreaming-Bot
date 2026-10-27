"""Owner-only maintenance commands."""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.app import KDBot

log = logging.getLogger(__name__)


async def _is_owner(interaction: discord.Interaction) -> bool:
    return await interaction.client.is_owner(interaction.user)  # type: ignore[attr-defined]


class Admin(commands.Cog):
    def __init__(self, bot: "KDBot") -> None:
        self.bot = bot

    @app_commands.command(name="sync", description="Re-sincroniza os comandos com o servidor.")
    @app_commands.check(_is_owner)
    async def sync(self, interaction: discord.Interaction) -> None:
        guild = discord.Object(id=self.bot.settings.guild_id)
        self.bot.tree.copy_global_to(guild=guild)
        synced = await self.bot.tree.sync(guild=guild)
        log.info("Synced %d commands via /sync", len(synced))
        await interaction.response.send_message(
            f"🔄 Sincronizados {len(synced)} comandos.", ephemeral=True
        )


async def setup(bot: "KDBot") -> None:
    await bot.add_cog(Admin(bot))
