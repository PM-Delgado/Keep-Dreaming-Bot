"""Allowlist-only commands: rating adjustments, rank overrides, proficiency overrides."""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.features.players.choices import (
    DIVISION_CHOICES,
    PROFICIENCY_CHOICES,
    ROLE_CHOICES,
    TIER_CHOICES,
)
from bot.features.players.service import AlreadyRegisteredError, PlayerError, PlayerService
from bot.features.ratings.service import Inspection, RatingError, RatingService
from bot.interaction_log import log_refusal
from bot.permissions import admin_only

if TYPE_CHECKING:
    from bot.app import KDBot

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()


def build_inspection_embed(nickname: str, ins: Inspection, ladder_label: str) -> discord.Embed:
    """Admin diagnostics: the only place raw numbers are shown."""
    embed = discord.Embed(
        description=f"# 🔬 Rating · {discord.utils.escape_markdown(nickname)}",
        color=discord.Color.dark_grey(),
    )
    s = ins.stored
    embed.add_field(
        name="H (Scrims)",
        value=f"mu {s.mu:.2f} · sigma {s.sigma:.2f} · {s.games} jogos\n{ladder_label}",
        inline=False,
    )
    rank = ins.resolution.rank
    if rank is None:
        r_text = "sem rank" + (" (a aguardar atualização)" if ins.resolution.riot_pending else "")
    else:
        age = f"{ins.rank_age_days:.0f} d" if ins.rank_age_days is not None else "?"
        r_text = f"{rank.label()} · fonte {rank.source} · {age}" + (
            " · DESATUALIZADO" if ins.rank_stale else ""
        )
    embed.add_field(name="R (rank)", value=r_text, inline=False)
    e = ins.estimate
    rank_part = "" if e.rank_mu is None else f" · R mu {e.rank_mu:.2f} · d {e.deviation:+.2f}"
    flag = "\n⚠ Divergência grande: o rank pode estar desatualizado." if e.diverged else ""
    embed.add_field(
        name="E (matchmaking)", value=f"mu {e.mu:.2f} · sigma {e.sigma:.2f}{rank_part}{flag}"
    )
    return embed


class InhouseAdmin(commands.Cog):
    def __init__(self, bot: "KDBot", players: PlayerService, ratings: RatingService) -> None:
        self.bot = bot
        self.players = players
        self.ratings = ratings

    admin = app_commands.Group(name="admin", description="Comandos de administração dos Scrims.")
    player = app_commands.Group(name="player", description="Jogadores.", parent=admin)
    rating = app_commands.Group(name="rating", description="Ratings.", parent=admin)
    roles = app_commands.Group(name="roles", description="Roles.", parent=admin)

    @player.command(name="register", description="Regista um membro nos Scrims.")
    @app_commands.describe(
        member="O membro a registar.",
        display_name="Nome nos Scrims (por defeito, o nome atual no Discord).",
    )
    @admin_only
    async def player_register(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        display_name: str | None = None,
    ) -> None:
        try:
            player = await self.players.register(
                member.id, display_name or member.display_name, registered_by=interaction.user.id
            )
        except AlreadyRegisteredError as exc:
            log_refusal(interaction, exc)
            # Non-destructive: the existing profile, rating, ranks and roles are left as they were.
            await interaction.response.send_message(
                f"ℹ️ {exc.player.nickname} já está registado. Nada foi alterado.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        except PlayerError as exc:
            log_refusal(interaction, exc)
            hint = "" if display_name else " Indica um display_name."
            await interaction.response.send_message(
                f"{exc}{hint}", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        log.info("Admin %s registered player %s", interaction.user.id, player.id)
        nickname = discord.utils.escape_markdown(player.nickname)
        await interaction.response.send_message(
            f"✅ {member.mention} registado como **{nickname}**.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @rating.command(name="set", description="Ajusta o rating dos Scrims (mu/sigma) de um jogador.")
    @app_commands.describe(mu="Novo mu (0-100).", sigma="Novo sigma (0,5-25); omite para manter.")
    @admin_only
    async def rating_set(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        mu: float,
        sigma: float | None = None,
        note: str | None = None,
    ) -> None:
        try:
            player = await self.players.require(user.id)
            stored = await self.ratings.adjust(player.id, mu, sigma, interaction.user.id, note)
        except (PlayerError, RatingError) as exc:
            log_refusal(interaction, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        log.info("Admin %s adjusted rating of player %s", interaction.user.id, player.id)
        await interaction.response.send_message(
            f"🎚️ Rating de **{player.nickname}**: mu {stored.mu:.2f}, sigma {stored.sigma:.2f}.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @rating.command(name="inspect", description="Mostra H, R e E de um jogador.")
    @admin_only
    async def rating_inspect(self, interaction: discord.Interaction, user: discord.Member) -> None:
        try:
            player = await self.players.require(user.id)
            ins = await self.ratings.inspect(player.id)
        except (PlayerError, RatingError) as exc:
            log_refusal(interaction, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        label = (
            self.ratings.ladder.label(ins.standing.tier_key or "", ins.standing.division)
            if ins.standing.placed
            else f"colocação {ins.standing.games}/{self.ratings.ladder.placement_games}"
        )
        await interaction.response.send_message(
            embed=build_inspection_embed(player.nickname, ins, label), ephemeral=True
        )

    @rating.command(name="recompute", description="Reconstrói os ratings a partir do histórico.")
    @admin_only
    async def rating_recompute(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.ratings.rebuild()
        await interaction.followup.send("🔄 Ratings reconstruídos.", ephemeral=True)

    @rating.command(
        name="rank", description="Define (ou limpa) o rank de referência de um jogador."
    )
    @app_commands.describe(tier="Omite para limpar a definição de admin.")
    @app_commands.choices(tier=TIER_CHOICES, division=DIVISION_CHOICES)
    @admin_only
    async def rating_rank(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        tier: app_commands.Choice[str] | None = None,
        division: app_commands.Choice[str] | None = None,
    ) -> None:
        try:
            player = await self.players.require(user.id)
            rank = await self.ratings.set_admin_rank(
                player.id,
                tier.value if tier else None,
                division.value if division else None,
                interaction.user.id,
            )
        except (PlayerError, RatingError) as exc:
            log_refusal(interaction, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        text = f"Rank de referência: **{rank.label()}**." if rank else "Sem rank de referência."
        await interaction.response.send_message(
            f"🎖️ {player.nickname}: {text}", ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    @roles.command(name="set", description="Define (ou limpa) a proficiência de admin numa role.")
    @app_commands.choices(role=ROLE_CHOICES)
    @app_commands.choices(proficiency=PROFICIENCY_CHOICES)
    @admin_only
    async def roles_set(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        role: app_commands.Choice[str],
        proficiency: app_commands.Choice[int],
    ) -> None:
        try:
            player = await self.players.set_admin_override(
                user.id, role.value, proficiency.value or None
            )
        except PlayerError as exc:
            log_refusal(interaction, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        done = "limpa" if not proficiency.value else f"definida para {proficiency.value}/5"
        await interaction.response.send_message(
            f"✅ Proficiência (admin) de **{player.nickname}** em {role.name} {done}.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
