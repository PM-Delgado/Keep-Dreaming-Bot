"""Bot class: owns settings and the database, loads feature extensions, syncs commands."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot import interaction_log
from bot.config import Settings
from bot.db import Database

log = logging.getLogger(__name__)

EXTENSIONS = (
    "bot.features.nameday.cog",
    "bot.features.admin.cog",
    "bot.features.icons.cog",  # role icons; before every cog that shows roles
    "bot.features.champions.cog",  # champion list and icons; before riot and lobby
    "bot.features.players.cog",
    "bot.features.riot.cog",  # optional; must load after players (plugs into its services)
    "bot.features.lobby.cog",  # must load after players and champions
    "bot.features.stats.cog",  # read-only; after players and champions
)


def _on_off(flag: object) -> str:
    return "on" if flag else "off"


class KDBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        # Slash commands only: no prefix commands, so no privileged intents are needed.
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.settings = settings
        self.db = Database(settings.db_path)
        self.tree.on_error = self.on_tree_error
        self.tree.interaction_check = self.on_tree_check

    def describe_settings(self) -> str:
        """The switches that change what the bot does, for the startup log (never the secrets)."""
        s = self.settings
        return (
            f"guild={s.guild_id} admins={len(s.admin_ids)} riot={_on_off(s.riot_api_key)}"
            f" mock_players={_on_off(s.mock_players)} icons={_on_off(s.champion_icons)}"
            f" ready_check={s.ready_check_seconds}s log_level={s.log_level}"
            f" scrim_channel={s.scrim_channel_id or 'off'} scrim_role={_on_off(s.scrim_role_id)}"
        )

    async def setup_hook(self) -> None:
        log.info("Starting KD Bot: %s", self.describe_settings())
        await self.db.connect()
        log.info("Database ready: %s", await self.db.describe())
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
        guild = self.get_guild(self.settings.guild_id)
        if guild is None:
            log.warning(
                "Logged in as %s, but the bot is not in guild %d: commands will not appear.",
                self.user,
                self.settings.guild_id,
            )
            return
        log.info(
            "Ready: logged in as %s, serving %s (%d members)",
            self.user,
            guild.name,
            guild.member_count,
        )

    async def close(self) -> None:
        log.info("Shutting down.")
        await super().close()
        await self.db.close()
        log.info("Database closed. Bye.")

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        interaction_log.log_received(interaction)

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> None:
        interaction_log.log_completed(interaction)

    async def on_tree_check(self, interaction: discord.Interaction) -> bool:
        interaction_log.mark_received(interaction)
        return True

    async def on_tree_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        interaction_log.log_failed(interaction, error)
        if isinstance(error, app_commands.CheckFailure):
            message = "⛔ Não tens permissão para usar este comando."
        else:
            message = "💥 Ocorreu um erro inesperado. Tenta novamente mais tarde."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
