"""/lobby commands, the lobby buttons' behaviour, the deadline sweeper, and /mock test commands."""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.features.lobby.mock import MockPlayers
from bot.features.lobby.render import (
    build_list_embed,
    build_lobby_embed,
    ready_check_ping,
    scheduled_ping,
)
from bot.features.lobby.repository import ACTIVE, LobbyRepository, LobbyRow
from bot.features.lobby.schedule import EXAMPLES
from bot.features.lobby.service import LobbyParams, LobbyService, LobbySnapshot, LobbyUpdate
from bot.features.lobby.views import LobbyView
from bot.features.players.choices import ROLE_CHOICES
from bot.features.players.service import PlayerError
from bot.permissions import admin_only, is_admin

if TYPE_CHECKING:
    from bot.app import KDBot

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
SWEEP_SECONDS = 5


class Lobby(commands.Cog):
    def __init__(self, bot: "KDBot", service: LobbyService, mock: MockPlayers) -> None:
        self.bot = bot
        self.service = service
        self.mock = mock

    lobby = app_commands.Group(name="lobby", description="Lobbies de in-house.")
    mock_group = app_commands.Group(
        name="mock", description="Jogadores de teste (só com ENABLE_MOCK_PLAYERS)."
    )

    async def cog_load(self) -> None:
        # One registered view answers every lobby message, even those posted before a restart.
        self.bot.add_view(LobbyView(self, None))
        self.sweeper.start()

    async def cog_unload(self) -> None:
        self.sweeper.cancel()

    # --- time-driven transitions ----

    @tasks.loop(seconds=SWEEP_SECONDS)
    async def sweeper(self) -> None:
        try:
            updates = [
                *await self.service.process_schedules(),
                *await self.service.process_deadlines(),
                *await self.service.expire_idle(),
            ]
        except Exception:
            log.exception("Lobby sweeper failed")
            return
        for update in updates:
            await self.refresh_message(update.snapshot)
            await self.notify(update)

    @sweeper.before_loop
    async def _before_sweeper(self) -> None:
        await self.bot.wait_until_ready()

    # --- messages ----

    def message_kwargs(self, snapshot: LobbySnapshot) -> dict:
        view = LobbyView(self, snapshot.lobby.status)
        return {"embed": build_lobby_embed(snapshot), "view": view if view.children else None}

    async def refresh_message(self, snapshot: LobbySnapshot) -> None:
        """Re-render the public lobby message (best effort: it may have been deleted)."""
        lobby = snapshot.lobby
        if lobby.message_id is None:
            return
        try:
            channel = self.bot.get_channel(lobby.channel_id) or await self.bot.fetch_channel(
                lobby.channel_id
            )
            await channel.get_partial_message(lobby.message_id).edit(  # type: ignore[union-attr]
                **self.message_kwargs(snapshot)
            )
        except (discord.HTTPException, AttributeError):
            log.warning("Could not update the message of lobby %s", lobby.id, exc_info=True)

    async def _send_ping(self, lobby: LobbyRow, text: str, discord_ids: list[int]) -> None:
        users = [discord.Object(id=i) for i in discord_ids]
        try:
            channel = self.bot.get_channel(lobby.channel_id) or await self.bot.fetch_channel(
                lobby.channel_id
            )
            await channel.send(  # type: ignore[union-attr]
                text, allowed_mentions=discord.AllowedMentions(users=users)
            )
        except (discord.HTTPException, AttributeError):
            log.warning("Could not ping lobby %s", lobby.id, exc_info=True)

    async def notify(self, update: LobbyUpdate) -> None:
        """Ping real players (never mock ones): ready-check (re)starts, scheduled reminders."""
        events = set(update.events)
        lobby = update.snapshot.lobby
        if {"ready_check_started", "roster_changed", "timeout"} & events:
            text = ready_check_ping(update.snapshot, "ready_check_started" not in events)
            if text is not None:
                await self._send_ping(
                    lobby, text, [e.discord_id for e in update.snapshot.roster if not e.is_mock]
                )
        for event in ("reminder", "short_of_players"):
            if event in events and (ping := scheduled_ping(update.snapshot, event)) is not None:
                text, recipients = ping
                await self._send_ping(lobby, text, [e.discord_id for e in recipients])

    # --- button handlers ----

    async def on_action(self, interaction: discord.Interaction, action: str) -> None:
        message = interaction.message
        lobby = await self.service.lobby_for_message(message.id) if message else None
        if lobby is None:
            await interaction.response.send_message(
                "Esta lobby já não existe. Usa /lobby open.", ephemeral=True
            )
            return
        user_id = interaction.user.id
        try:
            if action == "join":
                update = await self.service.join(lobby.id, user_id)
            elif action in ("leave", "decline"):
                update = await self.service.leave(lobby.id, user_id)
            elif action == "ready":
                update = await self.service.ready(lobby.id, user_id)
            elif action == "confirm":
                update = await self.service.confirm(lobby.id, user_id)
            elif action == "cancel":
                update = await self.service.cancel(lobby.id, user_id, "cancelada")
            else:
                return
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(**self.message_kwargs(update.snapshot))
        await self.notify(update)

    # --- /lobby ----

    @lobby.command(name="open", description="Abre uma lobby de in-house neste canal.")
    async def open_lobby(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("Usa isto num servidor.", ephemeral=True)
            return
        try:
            update = await self.service.open(
                interaction.guild_id, interaction.channel_id, interaction.user.id
            )
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(**self.message_kwargs(update.snapshot))
        sent = await interaction.original_response()
        await self.service.attach_message(update.snapshot.lobby.id, sent.id, interaction.channel_id)

    async def _resolve(self, interaction: discord.Interaction, lobby_id: int | None):
        """The lobby a command is about: the given id, else the server's open lobby."""
        if interaction.guild_id is None:
            return None
        if lobby_id is None:
            return await self.service.open_lobby(interaction.guild_id)
        row = await self.service.repo.get(lobby_id)
        ok = row is not None and row.guild_id == interaction.guild_id and row.status in ACTIVE
        return row if ok else None

    @lobby.command(name="schedule", description="Agenda uma lobby para mais tarde.")
    @app_commands.describe(
        when=f"Quando (hora de Lisboa): {EXAMPLES}.", note="Uma nota opcional (ex.: noite de flex)."
    )
    async def schedule_lobby(
        self, interaction: discord.Interaction, when: str, note: str | None = None
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("Usa isto num servidor.", ephemeral=True)
            return
        try:
            update = await self.service.schedule(
                interaction.guild_id, interaction.channel_id, interaction.user.id, when, note
            )
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(**self.message_kwargs(update.snapshot))
        sent = await interaction.original_response()
        await self.service.attach_message(update.snapshot.lobby.id, sent.id, interaction.channel_id)

    @lobby.command(name="list", description="Mostra a lobby aberta e as agendadas.")
    async def list_lobbies(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Usa isto num servidor.", ephemeral=True)
            return
        open_row = await self.service.open_lobby(interaction.guild_id)
        open_snapshot = await self.service.snapshot(open_row.id) if open_row else None
        scheduled = await self.service.list_scheduled(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_list_embed(open_snapshot, scheduled), allowed_mentions=NO_MENTIONS
        )

    @lobby.command(name="show", description="Volta a mostrar uma lobby neste canal.")
    @app_commands.describe(lobby="O número da lobby (por defeito, a aberta).")
    async def show_lobby(self, interaction: discord.Interaction, lobby: int | None = None) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("Usa isto num servidor.", ephemeral=True)
            return
        lobby_row = await self._resolve(interaction, lobby)
        if lobby_row is None:
            await interaction.response.send_message(
                "Não encontrei essa lobby. Usa /lobby list.", ephemeral=True
            )
            return
        lobby = lobby_row  # type: ignore[assignment]
        snapshot = await self.service.snapshot(lobby_row.id)
        await interaction.response.send_message(**self.message_kwargs(snapshot))
        sent = await interaction.original_response()
        old_channel, old_message = lobby_row.channel_id, lobby_row.message_id
        await self.service.attach_message(lobby_row.id, sent.id, interaction.channel_id)
        if old_message is not None:  # remove the stale copy so there is one live message
            try:
                channel = self.bot.get_channel(old_channel) or await self.bot.fetch_channel(
                    old_channel
                )
                await channel.get_partial_message(old_message).delete()  # type: ignore[union-attr]
            except (discord.HTTPException, AttributeError):
                pass

    @lobby.command(name="cancel", description="Cancela uma lobby (criador ou admin).")
    @app_commands.describe(lobby="O número da lobby (por defeito, a aberta).")
    async def cancel_lobby(
        self, interaction: discord.Interaction, lobby: int | None = None
    ) -> None:
        row = await self._resolve(interaction, lobby)
        if row is None:
            await interaction.response.send_message("Não encontrei essa lobby.", ephemeral=True)
            return
        try:
            update = await self.service.cancel(row.id, interaction.user.id, "cancelada")
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Lobby cancelada.", ephemeral=True)
        await self.refresh_message(update.snapshot)

    # --- /mock (admin, testing) ----

    async def _current_lobby(
        self, interaction: discord.Interaction, lobby_id: int | None = None
    ) -> int:
        lobby = await self._resolve(interaction, lobby_id)
        if lobby is None:
            raise PlayerError("Não há lobby aberta. Usa /lobby open primeiro.")
        return lobby.id

    @mock_group.command(name="create", description="Cria jogadores de teste.")
    @app_commands.describe(
        count="Quantos (1 a 30).", main_role="Todos têm esta role como main (para testar colisões)."
    )
    @app_commands.choices(main_role=ROLE_CHOICES)
    @admin_only
    async def mock_create(
        self,
        interaction: discord.Interaction,
        count: int = 9,
        main_role: app_commands.Choice[str] | None = None,
    ) -> None:
        try:
            created = await self.mock.create(
                count, interaction.user.id, main_role.value if main_role else None
            )
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        names = ", ".join(p.nickname for p in created)
        await interaction.response.send_message(f"Criados: {names}.", ephemeral=True)

    @mock_group.command(name="fill", description="Mete jogadores de teste na lobby aberta.")
    @app_commands.describe(
        count="Quantos (por defeito 9: tu + 9 = 10).",
        lobby="O número da lobby (por defeito, a aberta; usa para agendadas).",
    )
    @admin_only
    async def mock_fill(
        self, interaction: discord.Interaction, count: int = 9, lobby: int | None = None
    ) -> None:
        try:
            lobby_id = await self._current_lobby(interaction, lobby)
            await interaction.response.defer(ephemeral=True)
            result = await self.mock.fill(lobby_id, count, interaction.user.id)
        except PlayerError as exc:
            send = interaction.followup if interaction.response.is_done() else None
            if send:
                await send.send(str(exc), ephemeral=True)
            else:
                await interaction.response.send_message(str(exc), ephemeral=True)
            return
        snapshot = await self.service.snapshot(lobby_id)
        await self.refresh_message(snapshot)
        await self.notify(LobbyUpdate(snapshot, result.events))
        await interaction.followup.send(
            f"{len(result.players)} jogadores de teste entraram.", ephemeral=True
        )

    @mock_group.command(name="ready", description="Confirma (Pronto) todos os jogadores de teste.")
    @admin_only
    async def mock_ready(self, interaction: discord.Interaction, lobby: int | None = None) -> None:
        try:
            update = await self.mock.ready_all(await self._current_lobby(interaction, lobby))
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Jogadores de teste prontos.", ephemeral=True)
        await self.refresh_message(update.snapshot)

    @mock_group.command(name="drop", description="Os jogadores de teste saem da lobby.")
    @admin_only
    async def mock_drop(self, interaction: discord.Interaction, lobby: int | None = None) -> None:
        try:
            update = await self.mock.drop_all(await self._current_lobby(interaction, lobby))
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Jogadores de teste saíram.", ephemeral=True)
        await self.refresh_message(update.snapshot)

    @mock_group.command(name="list", description="Lista os jogadores de teste.")
    @admin_only
    async def mock_list(self, interaction: discord.Interaction) -> None:
        try:
            players = await self.mock.all_players()
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        lines = [await self.mock.describe(p) for p in players]
        text = chr(10).join(lines) or "Nenhum."
        await interaction.response.send_message(text, ephemeral=True)

    @mock_group.command(name="clear", description="Apaga todos os jogadores de teste.")
    @admin_only
    async def mock_clear(self, interaction: discord.Interaction) -> None:
        try:
            removed = await self.mock.clear()
        except PlayerError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"{removed} jogadores de teste apagados.", ephemeral=True
        )


async def setup(bot: "KDBot") -> None:
    players_cog = bot.get_cog("Players")
    if players_cog is None:
        raise RuntimeError("bot.features.players.cog must be loaded before bot.features.lobby.cog")
    settings = bot.settings
    service = LobbyService(
        LobbyRepository(bot.db),
        players_cog.players,  # type: ignore[attr-defined]
        players_cog.ratings,  # type: ignore[attr-defined]
        is_admin=lambda user_id: is_admin(settings, user_id),
        params=LobbyParams(ready_check_seconds=settings.ready_check_seconds),
    )
    mock = MockPlayers(
        players_cog.players,  # type: ignore[attr-defined]
        players_cog.ratings,  # type: ignore[attr-defined]
        service,
        settings.mock_players,
    )
    await bot.add_cog(Lobby(bot, service, mock))
    if settings.mock_players:
        log.warning("ENABLE_MOCK_PLAYERS is on: /mock commands are available (testing only).")
