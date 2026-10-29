"""/riot commands: link accounts, designate the main one, refresh, list, unlink.

Optional module: without RIOT_API_KEY the commands answer that the integration is off, and the
rest of the bot (manual ranks, proficiency, ratings) is unaffected.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.features.champions.service import ChampionService
from bot.features.icons.roles import RoleIconService
from bot.features.icons.tiers import TierIconService
from bot.features.players.service import PlayerError
from bot.features.players.text import tier_color
from bot.features.ratings.service import RatingError
from bot.features.riot.card import CardData, build_card_embed
from bot.features.riot.client import RiotClient
from bot.features.riot.history import format_ago, format_line, format_summary, summarize, to_entry
from bot.features.riot.leaderboard import LISBON
from bot.features.riot.platforms import PLATFORMS
from bot.features.riot.repository import AccountRow, RiotRepository
from bot.features.riot.service import (
    CONSENT_NOTICE,
    HistoryView,
    LinkResult,
    RankOutcome,
    RiotService,
)
from bot.features.riot.views import LeaderboardView, leaderboard_message
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
        note = f"Última atualização: {format_ago(view.last_synced, now)}."
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


REFRESH_HOURS = 6  # the timer that refreshes every linked account (a restart does not run it)
LEADERBOARD_TIME = dt.time(hour=10, minute=0, tzinfo=LISBON)  # the daily leaderboard post
NOT_ADMIN_POST = "Só admins podem publicar o leaderboard no canal."
NOT_ADMIN_CARD = "Só admins podem pré-visualizar cartões de jogo."
NO_FEED_CHANNEL = (
    "Não há canal do feed: define `RIOT_FEED_CHANNEL_ID` (e confirma que o bot lá pode escrever)."
)
KEY_LOG_EVERY = timedelta(hours=1)
KEY_DM_EVERY = timedelta(hours=24)
KEY_ALERT = (
    "⚠️ A Riot rejeitou a chave da API (`RIOT_API_KEY`). Enquanto não for corrigida, a "
    "atualização automática de ranks e histórico não funciona e o `/riot` falha. "
    "Verifica a chave no Railway. (Só te aviso uma vez por dia.)"
)


class KeyAlert:
    """Told every time Riot rejects the key: logs it (at most hourly) and messages the admins
    (at most daily), so an expired or revoked key does not silently freeze the data."""

    def __init__(
        self, bot: "KDBot", clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.bot = bot
        self.clock = clock
        self._logged: datetime | None = None
        self._messaged: datetime | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def rejected(self) -> None:
        now = self.clock()
        if self._logged is None or now - self._logged >= KEY_LOG_EVERY:
            self._logged = now
            log.warning("Riot rejected the API key: lookups fail until it is fixed.")
        if self._messaged is None or now - self._messaged >= KEY_DM_EVERY:
            self._messaged = now
            try:
                task = asyncio.get_running_loop().create_task(self._message_admins())
            except RuntimeError:  # no event loop: nothing can be sent
                return
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _message_admins(self) -> None:
        for admin_id in sorted(self.bot.settings.admin_ids):
            try:
                user = await self.bot.fetch_user(admin_id)
                await user.send(KEY_ALERT)
                log.info("Told admin %s that Riot rejected the API key.", admin_id)
            except discord.HTTPException as exc:  # DMs closed, unknown user, ...
                log.warning(
                    "Could not message admin %s about the Riot key: %s", admin_id, exc.status
                )


class Riot(commands.Cog):
    def __init__(
        self,
        bot: "KDBot",
        service: RiotService,
        icons: RoleIconService | None = None,
        champions: ChampionService | None = None,
        tiers: TierIconService | None = None,
    ) -> None:
        self.bot = bot
        self.service = service
        self.icons = icons  # role icons
        self.champions = champions  # champion names and icons
        self.tiers = tiers  # rank (tier) icons

    riot = app_commands.Group(name="riot", description="Contas Riot ligadas aos jogadores.")

    async def cog_load(self) -> None:
        self.refresher.start()
        # One registered view answers every leaderboard message, even those posted before a restart.
        self.bot.add_view(LeaderboardView(self.service, self.tiers))
        if self.bot.settings.riot_feed_channel_id is not None and self.service.enabled:
            self.daily_leaderboard.start()

    async def cog_unload(self) -> None:
        self.refresher.cancel()
        self.daily_leaderboard.cancel()
        if self.service.client is not None:
            await self.service.client.close()

    @tasks.loop(hours=REFRESH_HOURS)
    async def refresher(self) -> None:
        if self.refresher.current_loop == 0:
            return  # a restart is not a reason to ask Riot again; profiles and lobbies refresh
        try:
            await self.service.refresh_all()
        except Exception:
            log.exception("Riot refresh cycle crashed")

    @refresher.before_loop
    async def _before_refresher(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(time=LEADERBOARD_TIME)
    async def daily_leaderboard(self) -> None:
        try:
            await self.post_leaderboard(snapshot=True)
        except Exception:
            log.exception("The daily leaderboard post crashed")

    @daily_leaderboard.before_loop
    async def _before_daily_leaderboard(self) -> None:
        await self.bot.wait_until_ready()

    async def post_leaderboard(self, snapshot: bool) -> bool:
        """Look up the mains' ranks and post the leaderboard in the feed channel. `snapshot`
        (the daily post) also records the day's ranks, which tomorrow's LP trend is measured
        against. False when there is no usable channel; Riot problems only mean older ranks."""
        channel = self._feed_channel()
        if channel is None:
            log.warning("Riot feed channel not found: leaderboard not posted.")
            return False
        await self.service.refresh_main_ranks()
        if snapshot:
            await self.service.save_daily_snapshot()
        try:
            await channel.send(**await leaderboard_message(self.service, self.tiers))
        except discord.HTTPException as exc:
            log.warning("Could not post the leaderboard: %s", exc.status)
            return False
        log.info("Posted the Solo/Duo leaderboard in the feed channel")
        return True

    def _feed_channel(self) -> discord.abc.Messageable | None:
        """The channel of the leaderboard and the game cards, None when unset or not visible."""
        channel_id = self.bot.settings.riot_feed_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id is not None else None
        return channel if isinstance(channel, discord.abc.Messageable) else None

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
        if target == interaction.user.id:
            await interaction.followup.send(
                f"{link_summary(result)}\n\n{CONSENT_NOTICE}", ephemeral=True
            )
        else:
            # An admin linking for someone has that person's consent already: no notice, and
            # nothing is posted in the channel or sent to them.
            await interaction.followup.send(link_summary(result), ephemeral=True)

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
        send = interaction.response.send_message
        if await self.service.history_refresh_due(target.id, riot_id):
            # Ask Riot for new games first. Discord wants an answer within 3 s, so acknowledge now
            # (the wait is a few seconds at most), then read the games again.
            await interaction.response.defer()
            await self.service.refresh_history_for_view(target.id, riot_id)
            send = interaction.followup.send
            try:
                view = await self.service.recent_history(target.id, riot_id=riot_id)
            except (PlayerError, RatingError) as exc:
                log_refusal(interaction, exc)
                await send(str(exc))
                return
        ids = sorted({g.champion for g in view.games if g.champion})
        names = await self.champions.names(ids) if self.champions else {}
        icons = await self.champions.icon_markup(ids) if self.champions else {}
        await send(
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

    @riot.command(name="leaderboard", description="Classificação Solo/Duo das contas principais.")
    @app_commands.describe(post="Só admins: publicar também no canal do feed (atualiza os ranks).")
    async def leaderboard(self, interaction: discord.Interaction, post: bool = False) -> None:
        if post:
            await self._post_leaderboard_command(interaction)
            return
        send = interaction.response.send_message
        if await self.service.leaderboard_stale():
            await interaction.response.defer()
            await self.service.refresh_ranks_for_view()
            send = interaction.followup.send
        await send(**await leaderboard_message(self.service, self.tiers))

    async def _post_leaderboard_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(self.bot.settings, interaction.user.id):
            await interaction.response.send_message(NOT_ADMIN_POST, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            posted = await self.post_leaderboard(snapshot=False)
        except PlayerError as exc:
            log_refusal(interaction, exc)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            "📨 Leaderboard publicado no canal do feed." if posted else NO_FEED_CHANNEL,
            ephemeral=True,
        )

    async def build_card(self, target: discord.abc.User, riot_id: str | None) -> discord.Embed:
        """The post-game card of `target`'s newest stored game (see `RiotService.game_card`)."""
        source = await self.service.game_card(target.id, riot_id)
        game = source.game
        ids = [c for c in (game.champion, game.opponent_champion) if c]
        names = await self.champions.names(ids) if self.champions else {}
        champion = game.champion or "?"
        opponent = game.opponent_champion
        icon = (
            await self.champions.icon_link(champion) if self.champions and game.champion else None
        )
        card = CardData(
            nickname=source.nickname,
            riot_id=source.riot_id,
            game=game,
            champion_name=names.get(champion, champion),
            laning=source.laning,
            opponent_name=names.get(opponent, opponent) if opponent else None,
            rank=source.rank,
            lp_delta=None,  # only known for games the feed saw live
            champion_icon_url=icon,
            avatar_url=target.display_avatar.url,
        )
        return build_card_embed(
            card,
            self.icons.markup() if self.icons else None,
            self.tiers.markup() if self.tiers else None,
        )

    @riot.command(name="card", description="Pré-visualiza o cartão do último jogo (admins).")
    @app_commands.describe(
        player="O jogador (por defeito, tu).",
        riot_id="Só os jogos desta conta (por defeito, todas as contas).",
        post="Publicar também no canal do feed.",
    )
    @app_commands.autocomplete(riot_id=_history_autocomplete)
    async def card(
        self,
        interaction: discord.Interaction,
        player: discord.Member | None = None,
        riot_id: str | None = None,
        post: bool = False,
    ) -> None:
        if not is_admin(self.bot.settings, interaction.user.id):
            await interaction.response.send_message(NOT_ADMIN_CARD, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            embed = await self.build_card(player or interaction.user, riot_id)
        except (PlayerError, RatingError) as exc:
            log_refusal(interaction, exc)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if not post:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        channel = self._feed_channel()
        if channel is None:
            await interaction.followup.send(NO_FEED_CHANNEL, ephemeral=True)
            return
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("Could not post the card: %s", exc.status)
            await interaction.followup.send("Não consegui publicar no canal.", ephemeral=True)
            return
        await interaction.followup.send("📨 Cartão publicado no canal do feed.", ephemeral=True)

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
    client = (
        RiotClient(settings.riot_api_key, on_auth_error=KeyAlert(bot).rejected)
        if settings.riot_api_key
        else None
    )
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
    players_cog.players.view_refresh_due = service.view_refresh_due
    players_cog.players.view_refresher = service.refresh_for_view
    await bot.add_cog(
        Riot(
            bot,
            service,
            getattr(bot.get_cog("Icons"), "roles", None),
            getattr(bot.get_cog("Champions"), "service", None),
            getattr(bot.get_cog("Icons"), "tiers", None),
        )
    )
    resumed = await service.resume_unfinished_syncs()
    if resumed:
        log.info("Resuming the ranked-history sync of %d account(s).", resumed)
