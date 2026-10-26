"""Bot class: owns settings and the database, loads feature extensions, syncs commands."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import Settings
from bot.db import Database

log = logging.getLogger(__name__)

EXTENSIONS = (
    "bot.features.nameday.cog",
    "bot.features.admin.cog",
)


class KDBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        # Slash commands only: no prefix commands, so no privileged intents are needed.
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.settings = settings
        self.db = Database(settings.db_path)
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self) -> None:
        await self.db.connect()
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Loaded extension %s", extension)

        # Commands are defined globally but only ever synced to the configured server.
        guild = discord.Object(id=self.settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info(
            "Synced %d commands to guild %d: %s", len(synced), guild.id, [c.name for c in synced]
        )

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def close(self) -> None:
        await super().close()
        await self.db.close()

    async def on_tree_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "Não tens permissão para usar este comando."
        else:
            log.error("Unhandled error in command %s", interaction.command, exc_info=error)
            message = "Ocorreu um erro inesperado. Tenta novamente mais tarde."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
