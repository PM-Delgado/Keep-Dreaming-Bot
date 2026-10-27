"""Slash commands for the in-house roster: /register and /profile (view, roles, rank).

This extension also wires the rating services and the admin commands (admin.py), because the
ratings feature has no commands of its own.
"""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.features.champions.service import ChampionService
from bot.features.icons.roles import RoleIconService
from bot.features.players.admin import InhouseAdmin
from bot.features.players.choices import (
    DIVISION_CHOICES,
    PROFICIENCY_CHOICES,
    ROLE_CHOICES,
    TIER_CHOICES,
)
from bot.features.players.profile_embed import NOT_REGISTERED, build_profile_embed
from bot.features.players.repository import PlayerRepository
from bot.features.players.service import PlayerError, PlayerService
from bot.features.ratings.repository import RatingRepository
from bot.features.ratings.service import RatingError, RatingService

if TYPE_CHECKING:
    from bot.app import KDBot
    from bot.features.stats.service import StatsService

log = logging.getLogger(__name__)


class Players(commands.Cog):
    def __init__(
        self,
        bot: "KDBot",
        players: PlayerService,
        ratings: RatingService,
        icons: RoleIconService | None = None,
    ) -> None:
        self.bot = bot
        self.players = players
        self.ratings = ratings
        self.icons = icons  # role icons; None or empty means role names only
        # Set by the stats extension (loaded after this one): in-house numbers and champion icons.
        self.stats: StatsService | None = None
        self.champions: ChampionService | None = None

    profile = app_commands.Group(name="profile", description="O teu perfil in-house.")

    @app_commands.command(name="register", description="Regista-te nos in-houses.")
    @app_commands.describe(nickname="O nome que aparece nos in-houses (2 a 24 caracteres).")
    async def register(self, interaction: discord.Interaction, nickname: str) -> None:
        try:
            player = await self.players.register(interaction.user.id, nickname)
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Registado como **{discord.utils.escape_markdown(player.nickname)}**! "
            "Define as tuas roles com /profile roles e o teu rank com /profile rank.",
            ephemeral=True,
        )

    @profile.command(name="view", description="Mostra um perfil in-house.")
    @app_commands.describe(user="De quem queres ver o perfil (por defeito, o teu).")
    async def view(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        try:
            profile = await self.players.profile(target.id)
        except PlayerError:
            await interaction.response.send_message(NOT_REGISTERED, ephemeral=True)
            return
        except RatingError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        stats = None
        if self.stats is not None and interaction.guild_id is not None:
            stats = await self.stats.stats(interaction.guild_id, target.id)
        ids = {champion for champion, _ in profile.champion_pool}
        if stats is not None:
            ids |= {b.key[0] for b in stats.by_champion}
        ids = sorted(ids)
        icons = await self.champions.icon_markup(ids) if self.champions else {}
        names = await self.champions.names(ids) if self.champions else {}
        embed = build_profile_embed(
            profile,
            self.ratings.ladder,
            self.icons.markup() if self.icons else None,
            stats,
            icons,
            names,
            target.display_avatar.url,
        )
        await interaction.response.send_message(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @profile.command(name="roles", description="Define o quão à vontade estás numa role.")
    @app_commands.describe(role="A role.", proficiency="De 1 a 5, ou Limpar.")
    @app_commands.choices(role=ROLE_CHOICES)
    @app_commands.choices(proficiency=PROFICIENCY_CHOICES)
    async def roles(
        self,
        interaction: discord.Interaction,
        role: app_commands.Choice[str],
        proficiency: app_commands.Choice[int],
    ) -> None:
        value = proficiency.value or None
        try:
            await self.players.set_declared(interaction.user.id, role.value, value)
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        done = "limpa" if value is None else f"definida para {value}/5"
        await interaction.response.send_message(
            f"Proficiência em **{role.name}** {done}.", ephemeral=True
        )

    @profile.command(name="rank", description="Diz-nos o teu rank de Solo/Duo no League.")
    @app_commands.describe(tier="O teu tier.", division="A divisão (ignorada em Master+).")
    @app_commands.choices(tier=TIER_CHOICES, division=DIVISION_CHOICES)
    async def rank(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        division: app_commands.Choice[str] | None = None,
    ) -> None:
        try:
            player = await self.players.require(interaction.user.id)
            rank = await self.ratings.set_self_reported_rank(
                player.id, tier.value, division.value if division else None, interaction.user.id
            )
        except (PlayerError, RatingError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        label = rank.label() if rank else tier.name
        await interaction.response.send_message(
            f"Rank de referência: **{label}**. Serve só como ponto de partida; "
            "o rank do grupo vem dos jogos in-house.",
            ephemeral=True,
        )


async def setup(bot: "KDBot") -> None:
    ratings = RatingService(RatingRepository(bot.db))
    players = PlayerService(PlayerRepository(bot.db), ratings)
    if await ratings.rebuild_if_stale():
        log.info("Rating caches rebuilt (algorithm or ladder version changed).")
    await bot.add_cog(Players(bot, players, ratings, getattr(bot.get_cog("Icons"), "roles", None)))
    await bot.add_cog(InhouseAdmin(bot, players, ratings))
