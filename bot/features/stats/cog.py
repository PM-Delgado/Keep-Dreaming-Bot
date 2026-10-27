"""/history, /stats and /leaderboard: public numbers from confirmed games (read-only)."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.features.champions.service import ChampionService
from bot.features.icons.roles import RoleIconService
from bot.features.players.repository import Player
from bot.features.players.service import NOT_REGISTERED, PlayerError
from bot.features.players.text import tier_color
from bot.features.stats.render import build_board_embed, build_history_embed, build_stats_embed
from bot.features.stats.repository import StatsRepository
from bot.features.stats.service import KDA, MIN_BOARD_GAMES, RANK, WIN_RATE, StatsService

if TYPE_CHECKING:
    from bot.app import KDBot

NO_MENTIONS = discord.AllowedMentions.none()
BOARD_CHOICES = [
    app_commands.Choice(name="Rank Scrims", value=RANK),
    app_commands.Choice(name=f"Win Rate Scrims (mín. {MIN_BOARD_GAMES} jogos)", value=WIN_RATE),
    app_commands.Choice(
        name=f"KDA Scrims (mín. {MIN_BOARD_GAMES} jogos com stats validadas)", value=KDA
    ),
]


class Stats(commands.Cog):
    def __init__(
        self,
        bot: "KDBot",
        service: StatsService,
        champions: ChampionService,
        icons: RoleIconService | None = None,
    ) -> None:
        self.bot = bot
        self.service = service
        self.champions = champions
        self.icons = icons  # role icons; None or empty means role names only

    def _roles(self) -> Mapping[str, str]:
        return self.icons.markup() if self.icons else {}

    async def _champion_maps(self, ids: list[str]) -> tuple[dict[str, str], dict[str, str]]:
        """(icon markup, display names) of the champions that appear in a reply."""
        unique = sorted(set(ids))
        return await self.champions.icon_markup(unique), await self.champions.names(unique)

    async def _color(self, player: Player) -> discord.Color:
        """The side bar has the colour of the player's League Solo/Duo tier, as on the profile."""
        return tier_color((await self.service.ratings.resolve(player.id)).rank)

    async def _avatar(self, guild: discord.Guild | None, player: Player) -> str | None:
        """The Discord avatar of a player (the board's leader), or None if not reachable."""
        if guild is None:
            return None
        member = guild.get_member(player.discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(player.discord_id)
            except discord.HTTPException:
                return None
        return member.display_avatar.url

    @app_commands.command(
        name="history", description="Mostra os últimos jogos de Scrims de um jogador."
    )
    @app_commands.describe(user="De quem queres ver o histórico (por defeito, o teu).")
    async def history(self, interaction: discord.Interaction, user: discord.Member | None = None):
        if interaction.guild_id is None:
            await interaction.response.send_message("Usa isto num servidor.", ephemeral=True)
            return
        target = user or interaction.user
        try:
            history = await self.service.history(interaction.guild_id, target.id)
        except PlayerError:
            await interaction.response.send_message(NOT_REGISTERED, ephemeral=True)
            return
        icons, names = await self._champion_maps([g.champion for g in history.lines if g.champion])
        embed = build_history_embed(
            history,
            datetime.now(UTC),
            self._roles(),
            icons,
            names,
            target.display_avatar.url,
            await self._color(history.player),
        )
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)

    @app_commands.command(
        name="stats", description="Mostra as estatísticas de Scrims de um jogador."
    )
    @app_commands.describe(user="De quem queres ver as estatísticas (por defeito, as tuas).")
    async def stats(self, interaction: discord.Interaction, user: discord.Member | None = None):
        if interaction.guild_id is None:
            await interaction.response.send_message("Usa isto num servidor.", ephemeral=True)
            return
        target = user or interaction.user
        try:
            stats = await self.service.stats(interaction.guild_id, target.id)
        except PlayerError:
            await interaction.response.send_message(NOT_REGISTERED, ephemeral=True)
            return
        icons, names = await self._champion_maps(
            [b.key[0] for b in (*stats.by_champion, *stats.by_champion_role)]
        )
        embed = build_stats_embed(
            stats,
            self._roles(),
            icons,
            names,
            target.display_avatar.url,
            await self._color(stats.player),
        )
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="leaderboard", description="Classificações do grupo.")
    @app_commands.describe(board="Qual a classificação (por defeito, o rank do grupo).")
    @app_commands.choices(board=BOARD_CHOICES)
    async def leaderboard(
        self, interaction: discord.Interaction, board: app_commands.Choice[str] | None = None
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message("Usa isto num servidor.", ephemeral=True)
            return
        kind = board.value if board else RANK
        entries = await self.service.leaderboard(interaction.guild_id, kind)
        avatar = await self._avatar(interaction.guild, entries[0].player) if entries else None
        embed = build_board_embed(kind, entries, self.service.ratings.ladder, avatar)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)


async def setup(bot: "KDBot") -> None:
    players_cog = bot.get_cog("Players")
    if players_cog is None:
        raise RuntimeError("bot.features.players.cog must be loaded before bot.features.stats.cog")
    champions_cog = bot.get_cog("Champions")
    if champions_cog is None:
        raise RuntimeError(
            "bot.features.champions.cog must be loaded before bot.features.stats.cog"
        )
    service = StatsService(
        StatsRepository(bot.db),
        players_cog.players,  # type: ignore[attr-defined]
        players_cog.ratings,  # type: ignore[attr-defined]
    )
    # The profile (players cog) shows in-house numbers and champion icons from here.
    players_cog.stats = service  # type: ignore[attr-defined]
    players_cog.champions = champions_cog.service  # type: ignore[attr-defined]
    icons_cog = bot.get_cog("Icons")
    await bot.add_cog(
        Stats(
            bot,
            service,
            champions_cog.service,  # type: ignore[attr-defined]
            getattr(icons_cog, "roles", None),
        )
    )
