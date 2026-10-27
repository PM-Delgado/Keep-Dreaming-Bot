"""/riot commands: link accounts, designate the main one, refresh, list, unlink.

Optional module: without RIOT_API_KEY the commands answer that the integration is off, and the
rest of the bot (manual ranks, proficiency, ratings) is unaffected.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.features.champions.service import ChampionService
from bot.features.icons.roles import RoleIconService
from bot.features.players.service import PlayerError
from bot.features.players.text import tier_color
from bot.features.ratings.service import RatingError
from bot.features.riot.client import RiotClient
from bot.features.riot.history import format_ago, format_line, format_summary, summarize, to_entry
from bot.features.riot.platforms import PLATFORMS
from bot.features.riot.repository import AccountRow, RiotRepository
from bot.features.riot.service import (
    CONSENT_NOTICE,
    HistoryView,
    LinkResult,
    RankOutcome,
    RiotService,
)
from bot.interaction_log import log_refusal
from bot.permissions import is_admin

if TYPE_CHECKING:
    from bot.app import KDBot

log = logging.getLogger(__name__)

PLATFORM_CHOICES = [app_commands.Choice(name=p.upper(), value=p) for p in PLATFORMS]
NOT_ADMIN = "Só admins podem gerir contas de outros jogadores."


def rank_text(outcome: RankOutcome | None) -> str:
    if outcome is None:
        return ""
    if outcome.status == "ranked":
        return f" Rank Solo/Duo atual: **{outcome.label}**."
    if outcome.status == "unranked":
        return " Sem rank de Solo/Duo (confirmado pela Riot)."
    return " Não consegui ler o rank agora; usa /riot refresh mais tarde."


def link_summary(result: LinkResult) -> str:
    role = "principal" if result.is_main else "secundária"
    star = "⭐" if result.is_main else "🔗"
    base = f"{star} Conta **{result.account.riot_id}** ligada como {role}."
    sync = " O histórico ranked está a ser sincronizado em segundo plano."
    return base + rank_text(result.rank) + sync


def build_list_embed(
    nickname: str,
    rows: list[tuple[AccountRow, int]],
    now: datetime,
    avatar_url: str | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Same look as the profile: large heading, avatar, tier colour, ★ after the main account."""
    lines = [f"# Contas Riot · {discord.utils.escape_markdown(nickname)}", ""]
    for account, games in rows:
        star = " ★" if account.is_main else ""
        if account.history_synced_at is None:
            synced = "a sincronizar"
        else:
            synced = f"sincronizado há {(now - account.history_synced_at).days} d"
        lines.append(f"**{account.riot_id}**{star} · {games} jogos ranked · {synced}")
    if not rows:
        lines.append("Nenhuma conta ligada. Usa `/riot link` para ligar a tua.")
    embed = discord.Embed(description="\n".join(lines), color=color or discord.Color.blurple())
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


def build_history_embed(
    view: HistoryView,
    now: datetime,
    champion_names: Mapping[str, str] | None = None,
    champion_icons: Mapping[str, str] | None = None,
    role_icons: Mapping[str, str] | None = None,
    avatar_url: str | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Presentation of /riot history. Restyle here; the numbers come from history.py.

    The champion maps are keyed by Riot's champion id, the role icons by role; both are optional.
    """
    entries = [to_entry(g) for g in view.games]
    lines = [
        format_line(
            e,
            now,
            show_account=view.multiple_accounts,
            champion_icon=(champion_icons or {}).get(e.champion, ""),
            role_icon=(role_icons or {}).get(e.role or "", ""),
            champion_name=(champion_names or {}).get(e.champion),
        )
        for e in entries
    ]
    scope = f" · {view.account.riot_id}" if view.account else ""
    if view.last_synced is None:
        note = "O histórico ainda está a sincronizar; pode estar incompleto."
    else:
        note = (
            f"Guardado no último sync ({format_ago(view.last_synced, now)}). "
            "Usa /riot refresh para atualizar."
        )
    heading = f"# Solo/Duo · {discord.utils.escape_markdown(view.nickname)}{scope}"
    embed = discord.Embed(
        description="\n".join(
            [heading, "", format_summary(summarize(entries)), "", *lines, "", f"-# {note}"]
        ),
        color=color or discord.Color.blurple(),
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


class Riot(commands.Cog):
    def __init__(
        self,
        bot: "KDBot",
        service: RiotService,
        icons: RoleIconService | None = None,
        champions: ChampionService | None = None,
    ) -> None:
        self.bot = bot
        self.service = service
        self.icons = icons  # role icons
        self.champions = champions  # champion names and icons

    riot = app_commands.Group(name="riot", description="Contas Riot ligadas aos jogadores.")

    async def cog_unload(self) -> None:
        if self.service.client is not None:
            await self.service.client.close()

    async def _color(self, player_id: int) -> discord.Color:
        """The side bar has the colour of the player's League Solo/Duo tier, as on the profile."""
        return tier_color((await self.service.ratings.resolve(player_id)).rank)

    def _target(self, interaction: discord.Interaction, member: discord.Member | None) -> int:
        """Players manage their own accounts; admins can manage anyone's."""
        if member is None or member.id == interaction.user.id:
            return interaction.user.id
        if not is_admin(self.bot.settings, interaction.user.id):
            raise PlayerError(NOT_ADMIN)
        return member.id

    async def _account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        raw = getattr(interaction.namespace, "player", None)
        target = int(getattr(raw, "id", raw)) if raw is not None else interaction.user.id
        if target != interaction.user.id and not is_admin(self.bot.settings, interaction.user.id):
            return []
        try:
            rows = await self.service.accounts(target)
        except PlayerError:
            return []
        wanted = current.casefold()
        return [
            app_commands.Choice(name=a.riot_id, value=a.riot_id)
            for a, _ in rows
            if wanted in a.riot_id.casefold()
        ][:25]

    @riot.command(name="link", description="Liga uma conta Riot a um jogador.")
    @app_commands.describe(
        riot_id="O Riot ID, no formato Nome#TAG.",
        platform="A plataforma da conta (por defeito, a do servidor).",
        player="Só admins: o jogador a quem ligar a conta.",
    )
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def link(
        self,
        interaction: discord.Interaction,
        riot_id: str,
        platform: app_commands.Choice[str] | None = None,
        player: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            target = self._target(interaction, player)
            result = await self.service.link(
                target, riot_id, platform.value if platform else None, interaction.user.id
            )
        except (PlayerError, RatingError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        log.info("Riot account %s linked to player of user %s", result.account.id, target)
        await interaction.followup.send(
            f"{link_summary(result)}\n\n{CONSENT_NOTICE}", ephemeral=True
        )
        if target != interaction.user.id:
            # Linked on someone's behalf: tell the channel so the owner sees it and can unlink.
            await interaction.followup.send(
                f"<@{target}>: um admin ligou a conta **{result.account.riot_id}** ao teu perfil.\n"
                f"{CONSENT_NOTICE}\nSe não concordas, usa /riot unlink.",
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=target)]),
            )

    @riot.command(name="main", description="Define a conta principal (usada para o rank).")
    @app_commands.describe(riot_id="Uma das contas ligadas.", player="Só admins.")
    @app_commands.autocomplete(riot_id=_account_autocomplete)
    async def main(
        self, interaction: discord.Interaction, riot_id: str, player: discord.Member | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            account, outcome = await self.service.set_main(
                self._target(interaction, player), riot_id, interaction.user.id
            )
        except (PlayerError, RatingError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"⭐ **{account.riot_id}** é agora a conta principal.{rank_text(outcome)}",
            ephemeral=True,
        )

    @riot.command(name="unlink", description="Remove uma conta ligada e os dados dela.")
    @app_commands.describe(riot_id="Uma das contas ligadas.", player="Só admins.")
    @app_commands.autocomplete(riot_id=_account_autocomplete)
    async def unlink(
        self, interaction: discord.Interaction, riot_id: str, player: discord.Member | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)  # promoting a new main calls Riot
        try:
            result = await self.service.unlink(
                self._target(interaction, player), riot_id, interaction.user.id
            )
        except (PlayerError, RatingError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        text = f"🗑️ Conta **{result.removed.riot_id}** removida, com o histórico e os ranks dela."
        if result.new_main is not None:
            text += (
                f" **{result.new_main.riot_id}** é agora a conta principal."
                f"{rank_text(result.rank)} Muda com /riot main, se preferires outra."
            )
        elif result.removed.is_main:
            text += " Já não há contas ligadas, por isso também não há conta principal."
        await interaction.followup.send(text, ephemeral=True)

    @riot.command(name="list", description="Mostra as contas Riot ligadas a um jogador.")
    @app_commands.describe(player="Por defeito, tu.")
    async def list_accounts(
        self, interaction: discord.Interaction, player: discord.Member | None = None
    ) -> None:
        target = player or interaction.user
        try:
            profile_player = await self.service.players.require(target.id)
            rows = await self.service.accounts(target.id)
        except (PlayerError, RatingError) as exc:
            log_refusal(interaction, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=build_list_embed(
                profile_player.nickname,
                rows,
                datetime.now(UTC),
                target.display_avatar.url,
                await self._color(profile_player.id),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _history_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Accounts are public, so anyone may pick any player's account here."""
        raw = getattr(interaction.namespace, "player", None)
        target = int(getattr(raw, "id", raw)) if raw is not None else interaction.user.id
        try:
            rows = await self.service.accounts(target)
        except PlayerError:
            return []
        wanted = current.casefold()
        return [
            app_commands.Choice(name=a.riot_id, value=a.riot_id)
            for a, _ in rows
            if wanted in a.riot_id.casefold()
        ][:25]

    @riot.command(name="history", description="Mostra os últimos jogos ranked Solo/Duo.")
    @app_commands.describe(
        player="O jogador (por defeito, tu).",
        riot_id="Só os jogos desta conta (por defeito, todas as contas).",
    )
    @app_commands.autocomplete(riot_id=_history_autocomplete)
    async def history(
        self,
        interaction: discord.Interaction,
        player: discord.Member | None = None,
        riot_id: str | None = None,
    ) -> None:
        target = player or interaction.user
        try:
            view = await self.service.recent_history(target.id, riot_id=riot_id)
        except (PlayerError, RatingError) as exc:
            log_refusal(interaction, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        ids = sorted({g.champion for g in view.games if g.champion})
        names = await self.champions.names(ids) if self.champions else {}
        icons = await self.champions.icon_markup(ids) if self.champions else {}
        await interaction.response.send_message(
            embed=build_history_embed(
                view,
                datetime.now(UTC),
                names,
                icons,
                self.icons.markup() if self.icons else None,
                target.display_avatar.url,
                await self._color((await self.service.players.require(target.id)).id),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @riot.command(name="refresh", description="Atualiza nomes, rank e histórico ranked.")
    @app_commands.describe(player="Só admins.")
    async def refresh(
        self, interaction: discord.Interaction, player: discord.Member | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            target = self._target(interaction, player)
            outcome = await self.service.refresh(
                target, interaction.user.id, force=is_admin(self.bot.settings, interaction.user.id)
            )
        except (PlayerError, RatingError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"🔄 Atualizado.{rank_text(outcome)} O histórico ranked continua em segundo plano.",
            ephemeral=True,
        )


async def setup(bot: "KDBot") -> None:
    from bot.features.players.cog import Players

    players_cog = bot.get_cog("Players")
    if not isinstance(players_cog, Players):
        raise RuntimeError("bot.features.players.cog must be loaded before bot.features.riot.cog")
    settings = bot.settings
    client = RiotClient(settings.riot_api_key) if settings.riot_api_key else None
    if client is None:
        log.info("RIOT_API_KEY is not set: the Riot integration is disabled.")
    service = RiotService(
        RiotRepository(bot.db),
        players_cog.ratings,
        players_cog.players,
        client,
        settings.riot_default_platform,
    )
    # Plug the optional module into the core services (the core never imports it).
    players_cog.ratings.main_puuid_of = service.main_puuid_of
    players_cog.players.role_estimator = service.role_estimates
    players_cog.players.account_labeler = service.account_labels
    players_cog.players.champion_pooler = service.champion_pool
    await bot.add_cog(
        Riot(
            bot,
            service,
            getattr(bot.get_cog("Icons"), "roles", None),
            getattr(bot.get_cog("Champions"), "service", None),
        )
    )
    resumed = await service.resume_unfinished_syncs()
    if resumed:
        log.info("Resuming the ranked-history sync of %d account(s).", resumed)
