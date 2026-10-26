"""Slash commands for the in-house roster: /register and /profile (view, roles, rank).

This extension also wires the rating services and the admin commands (admin.py), because the
ratings feature has no commands of its own.
"""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.features.players.admin import InhouseAdmin
from bot.features.players.choices import (
    DIVISION_CHOICES,
    PROFICIENCY_CHOICES,
    ROLE_CHOICES,
    TIER_CHOICES,
)
from bot.features.players.proficiency import ROLE_NAMES_PT
from bot.features.players.repository import PlayerRepository
from bot.features.players.service import PlayerError, PlayerService, Profile
from bot.features.ratings.ladder import LadderConfig
from bot.features.ratings.repository import RatingRepository
from bot.features.ratings.service import RatingError, RatingService

if TYPE_CHECKING:
    from bot.app import KDBot

log = logging.getLogger(__name__)

FOOTER = (
    "Rank interno do grupo: não equivale a um rank do League of Legends.\n"
    "Keep-Dreaming-Bot isn't endorsed by Riot Games and doesn't reflect the views or opinions of "
    "Riot Games or anyone officially involved in producing or managing Riot Games properties. "
    "Riot Games, and all associated properties are trademarks or registered trademarks of "
    "Riot Games, Inc."
)
SOURCE_PT = {"admin": "definido por admin", "declared": "declarado", "estimated": "estimado"}


def progress_bar(progress: float | None, width: int = 10) -> str:
    if progress is None:
        return ""
    filled = round(progress * width)
    return "▰" * filled + "▱" * (width - filled)


def in_house_rank_text(profile: Profile, ladder: LadderConfig) -> str:
    standing = profile.standing
    if not standing.placed:
        return f"Sem rank (colocação {standing.games}/{ladder.placement_games})"
    label = ladder.label(standing.tier_key or "", standing.division)
    bar = progress_bar(standing.progress)
    return f"{label} {bar}".strip()


def league_rank_text(profile: Profile) -> str:
    rank = profile.resolution.rank
    if rank is None:
        return "(a aguardar atualização)" if profile.resolution.riot_pending else "—"
    if rank.source == "riot":
        age = f"há {int(profile.rank_age_days or 0)} d"
        stale = ", desatualizado" if profile.rank_stale else ""
        return f"Riot Solo/Duo: {rank.label()} ({age}{stale})"
    return f"{rank.label()} ({SOURCE_PT.get(rank.source, 'declarado')})"


def build_profile_embed(profile: Profile, ladder: LadderConfig) -> discord.Embed:
    embed = discord.Embed(title=profile.player.nickname, color=discord.Color.blurple())
    embed.add_field(name="Rank do grupo", value=in_house_rank_text(profile, ladder), inline=False)
    embed.add_field(name="Rank League (referência)", value=league_rank_text(profile), inline=False)
    if profile.accounts:
        embed.add_field(name="Contas Riot", value="\n".join(profile.accounts), inline=False)
    roles = []
    for line in profile.roles:
        value = line.proficiency.value
        text = "—" if value is None else f"{value}/5 ({SOURCE_PT[line.proficiency.source]})"
        roles.append(f"**{ROLE_NAMES_PT[line.role]}**: {text}")
    embed.add_field(name="Roles", value="\n".join(roles), inline=False)
    embed.set_footer(text=FOOTER)
    return embed


class Players(commands.Cog):
    def __init__(self, bot: "KDBot", players: PlayerService, ratings: RatingService) -> None:
        self.bot = bot
        self.players = players
        self.ratings = ratings

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
        except (PlayerError, RatingError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=build_profile_embed(profile, self.ratings.ladder),
            allowed_mentions=discord.AllowedMentions.none(),
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
    await bot.add_cog(Players(bot, players, ratings))
    await bot.add_cog(InhouseAdmin(bot, players, ratings))
