"""Lobby commands, buttons, pings, sweeper and rendering, against a fake Discord."""

from types import SimpleNamespace
from typing import Any

import discord
import pytest_asyncio
from lobby_fakes import ADMIN, CHANNEL, GUILD, LobbyStack

from bot.app import EXTENSIONS, KDBot
from bot.config import load_settings
from bot.features.lobby.cog import Lobby
from bot.features.lobby.mock import MockPlayers
from bot.features.lobby.render import (
    band_text,
    build_lobby_embed,
    fit_note,
    ready_check_ping,
    team_text,
)
from bot.features.lobby.views import ACTIONS, VISIBLE, LobbyView

ENV = {
    "DISCORD_TOKEN": "t",
    "GUILD_ID": "123",
    "ASSETS_URL": "https://x.invalid/a/",
    "DB_PATH": ":memory:",
    "ADMIN_USER_IDS": str(ADMIN),
    "ENABLE_MOCK_PLAYERS": "true",
}


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits: list[dict] = []
        self.deleted = False

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)

    async def delete(self) -> None:
        self.deleted = True


class FakeChannel:
    def __init__(self) -> None:
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[dict] = []

    def get_partial_message(self, message_id: int) -> FakeMessage:
        return self.messages.setdefault(message_id, FakeMessage(message_id))

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        self.sent.append({"content": content, **kwargs})


class FakeBot:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel
        self.settings = load_settings(ENV)
        self.views: list[Any] = []

    def get_channel(self, _channel_id: int) -> FakeChannel:
        return self.channel

    async def fetch_channel(self, _channel_id: int) -> FakeChannel:
        return self.channel

    def add_view(self, view: Any, **_: Any) -> None:
        self.views.append(view)

    async def wait_until_ready(self) -> None:
        return None


class FakeInteraction:
    def __init__(self, user_id: int, message_id: int | None = None) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = GUILD
        self.channel_id = CHANNEL
        self.message = FakeMessage(message_id) if message_id is not None else None
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self._followups: list[dict] = []
        self._original = FakeMessage(777)
        outer = self

        class Response:
            _done = False

            async def send_message(self, content=None, **kwargs):
                self._done = True
                outer.sent.append({"content": content, **kwargs})

            async def edit_message(self, **kwargs):
                self._done = True
                outer.edited.append(kwargs)

            async def defer(self, **_):
                self._done = True

            def is_done(self):
                return self._done

        class Followup:
            async def send(self, content=None, **kwargs):
                outer._followups.append({"content": content, **kwargs})

        self.response = Response()
        self.followup = Followup()

    async def original_response(self) -> FakeMessage:
        return self._original


@pytest_asyncio.fixture
async def env(db):
    stack = LobbyStack(db)
    channel = FakeChannel()
    bot = FakeBot(channel)
    mock = MockPlayers(stack.players, stack.ratings, stack.lobbies, True)
    cog = Lobby(bot, stack.lobbies, mock)  # type: ignore[arg-type]
    return SimpleNamespace(stack=stack, channel=channel, bot=bot, cog=cog, mock=mock)


def text_of(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or "", embed.footer.text or ""]
    for field in embed.fields:
        parts += [field.name or "", field.value or ""]
    return "\n".join(parts)


async def open_as(env, user_id: int = 1) -> tuple[int, int]:
    """Open a lobby through the command; returns (lobby_id, message_id)."""
    await env.stack.register(1)
    ix = FakeInteraction(user_id)
    await env.cog.open_lobby.callback(env.cog, ix)
    lobby = await env.stack.lobbies.open_lobby(GUILD)
    assert lobby is not None
    return lobby.id, 777


# --- /lobby open, show, cancel ----


async def test_open_posts_the_lobby_message_and_remembers_it(env):
    await env.stack.register(1)
    ix = FakeInteraction(1)
    await env.cog.open_lobby.callback(env.cog, ix)
    posted = ix.sent[0]
    assert "In-house #1 · Aberta" in text_of(posted["embed"])
    assert posted["view"] is not None
    lobby = await env.stack.lobbies.lobby_for_message(777)
    assert lobby is not None  # the message id is stored so buttons can find the lobby
    assert lobby.channel_id == CHANNEL


async def test_open_twice_or_unregistered_is_an_ephemeral_error(env):
    lobby_id, _ = await open_as(env)
    ix = FakeInteraction(1)
    await env.cog.open_lobby.callback(env.cog, ix)
    assert ix.sent[0]["ephemeral"] is True
    assert "lobby" in ix.sent[0]["content"]
    ix = FakeInteraction(404)
    await env.cog.open_lobby.callback(env.cog, ix)
    assert "não está registado" in ix.sent[0]["content"]
    assert lobby_id == 1


async def test_show_reposts_the_message_and_removes_the_stale_one(env):
    await open_as(env)
    ix = FakeInteraction(5)
    ix._original = FakeMessage(888)
    await env.cog.show_lobby.callback(env.cog, ix)
    assert ix.sent[0]["embed"] is not None
    assert (await env.stack.lobbies.lobby_for_message(888)) is not None
    assert (await env.stack.lobbies.lobby_for_message(777)) is None
    assert env.channel.messages[777].deleted


async def test_cancel_command_is_for_creator_or_admin_and_updates_the_message(env):
    await open_as(env)
    await env.stack.register(2)
    ix = FakeInteraction(2)
    await env.cog.cancel_lobby.callback(env.cog, ix)
    assert "criador" in ix.sent[0]["content"]
    ix = FakeInteraction(ADMIN)
    await env.cog.cancel_lobby.callback(env.cog, ix)
    assert ix.sent[0]["content"] == "Lobby cancelada."
    edit = env.channel.messages[777].edits[-1]
    assert "Cancelada" in text_of(edit["embed"])
    assert edit["view"] is None  # a closed lobby has no buttons


# --- buttons ----


async def test_a_button_on_an_unknown_message_is_politely_refused(env):
    ix = FakeInteraction(1, message_id=424242)
    await env.cog.on_action(ix, "join")  # type: ignore[arg-type]
    assert "já não existe" in ix.sent[0]["content"]
    assert ix.edited == []


async def test_join_leave_flow_updates_the_message_in_place(env):
    _, message_id = await open_as(env)
    await env.stack.register(1, start=2)
    ix = FakeInteraction(2, message_id)
    await env.cog.on_action(ix, "join")  # type: ignore[arg-type]
    assert "Player 02" in text_of(ix.edited[0]["embed"])
    ix = FakeInteraction(2, message_id)
    await env.cog.on_action(ix, "join")  # type: ignore[arg-type]
    assert "Já estás nesta lobby" in ix.sent[0]["content"]
    ix = FakeInteraction(2, message_id)
    await env.cog.on_action(ix, "leave")  # type: ignore[arg-type]
    assert "Player 02" not in text_of(ix.edited[0]["embed"])


async def test_a_full_game_through_the_buttons_and_mock_players(env):
    lobby_id, message_id = await open_as(env)
    result = await env.mock.fill(lobby_id, 9, ADMIN)
    assert "ready_check_started" in result.events
    await env.mock.ready_all(lobby_id)

    ix = FakeInteraction(1, message_id)
    await env.cog.on_action(ix, "ready")  # type: ignore[arg-type]
    proposed = ix.edited[0]["embed"]
    assert "Equipas propostas" in text_of(proposed)
    assert [f.name for f in proposed.fields][:2] == ["🔵 Azul", "🔴 Vermelho"]
    assert all(r in text_of(proposed) for r in ("Top", "Jungle", "Mid", "ADC", "Suporte"))

    ix = FakeInteraction(3, message_id)  # a mock's id, but nobody real is 3 here
    await env.stack.register(1, start=3)
    ix = FakeInteraction(3, message_id)
    await env.cog.on_action(ix, "confirm")  # not the creator
    assert "criador" in ix.sent[0]["content"]

    ix = FakeInteraction(1, message_id)
    await env.cog.on_action(ix, "reroll")  # type: ignore[arg-type]
    assert ix.edited == []  # there is no such button any more: nothing happens

    ix = FakeInteraction(1, message_id)
    await env.cog.on_action(ix, "confirm")  # type: ignore[arg-type]
    final = ix.edited[0]
    assert "Em jogo" in text_of(final["embed"])
    assert [b.custom_id for b in final["view"].children] == ["lobby:cancel"]


async def test_the_buttons_shown_depend_on_the_state(env):
    assert set(VISIBLE) == {"OPEN", "READY_CHECK", "TEAMS_PROPOSED", "IN_PROGRESS"}
    expected = {
        "OPEN": ["lobby:join", "lobby:leave", "lobby:cancel"],
        "READY_CHECK": ["lobby:ready", "lobby:decline", "lobby:join", "lobby:cancel"],
        "TEAMS_PROPOSED": ["lobby:confirm", "lobby:leave", "lobby:cancel"],
        "IN_PROGRESS": ["lobby:cancel"],
    }
    for status, ids in expected.items():
        view = LobbyView(env.cog, status)
        assert [item.custom_id for item in view.children] == ids  # type: ignore[attr-defined]
        assert view.timeout is None  # persistent


async def test_the_registered_view_covers_every_action_for_persistence(env):
    view = LobbyView(env.cog, None)
    assert {item.custom_id for item in view.children} == {  # type: ignore[attr-defined]
        f"lobby:{action}" for action in ACTIONS
    }
    assert view.is_persistent()
    # Players cannot pick roles or ask for another split.
    assert not {"lobby:roles", "lobby:reroll"} & {item.custom_id for item in view.children}  # type: ignore[attr-defined]


# --- roles menu ----


# --- pings, sweeper ----


async def test_ready_check_pings_real_players_only_never_mocks(env):
    lobby_id, _ = await open_as(env)
    await env.stack.register(1, start=2)
    await env.stack.lobbies.join(lobby_id, 2)
    result = await env.mock.fill(lobby_id, 8, ADMIN)
    snapshot = await env.stack.lobbies.snapshot(lobby_id)
    from bot.features.lobby.service import LobbyUpdate

    await env.cog.notify(LobbyUpdate(snapshot, result.events))
    [ping] = env.channel.sent
    assert "<@1>" in ping["content"]
    assert "<@2>" in ping["content"]
    assert "<@-" not in ping["content"]  # mock ids are negative and never mentioned
    assert "<t:" in ping["content"]
    assert [u.id for u in ping["allowed_mentions"].users] == [1, 2]


async def test_notify_ignores_events_that_need_no_ping(env):
    lobby_id, _ = await open_as(env)
    from bot.features.lobby.service import LobbyUpdate

    snapshot = await env.stack.lobbies.snapshot(lobby_id)
    await env.cog.notify(LobbyUpdate(snapshot, ["joined"]))
    assert env.channel.sent == []


async def test_the_sweeper_drops_non_responders_updates_the_message_and_pings(env):
    lobby_id, _ = await open_as(env)
    await env.mock.fill(lobby_id, 9, ADMIN)
    await env.mock.ready_all(lobby_id)  # the real player (1) stays silent
    await env.stack.register(1, start=2)
    env.stack.clock.advance(seconds=125)
    await env.cog.sweeper.coro(env.cog)
    snapshot = await env.stack.lobbies.snapshot(lobby_id)
    assert snapshot.lobby.status == "OPEN"  # no reserves to backfill player 1
    edit = env.channel.messages[777].edits[-1]
    assert "Aberta" in text_of(edit["embed"])


async def test_the_sweeper_survives_an_error(env, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(env.stack.lobbies, "process_deadlines", boom)
    await env.cog.sweeper.coro(env.cog)  # logs and carries on instead of killing the loop


# --- /mock commands ----


async def test_mock_commands_run_a_whole_lobby_from_one_real_account(env):
    await env.stack.register(1)
    await env.cog.open_lobby.callback(env.cog, FakeInteraction(1))
    ix = FakeInteraction(ADMIN)
    await env.cog.mock_fill.callback(env.cog, ix, 9)  # type: ignore[arg-type]
    assert "9 jogadores de teste entraram" in ix._followups[0]["content"]
    assert "Ready-check" in text_of(env.channel.messages[777].edits[-1]["embed"])
    assert any("<@1>" in (m["content"] or "") for m in env.channel.sent)  # you get pinged

    ix = FakeInteraction(ADMIN)
    await env.cog.mock_ready.callback(env.cog, ix)  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == "Jogadores de teste prontos."
    ix = FakeInteraction(ADMIN)
    await env.cog.mock_list.callback(env.cog, ix)  # type: ignore[arg-type]
    assert "Mock 01" in ix.sent[0]["content"]
    ix = FakeInteraction(ADMIN)
    await env.cog.mock_clear.callback(env.cog, ix)  # type: ignore[arg-type]
    assert "lobbies ativas" in ix.sent[0]["content"]  # refused while the lobby is running


async def test_mock_commands_need_an_open_lobby(env):
    ix = FakeInteraction(ADMIN)
    await env.cog.mock_fill.callback(env.cog, ix, 9)  # type: ignore[arg-type]
    assert "Não há lobby aberta" in (ix._followups + ix.sent)[0]["content"]


async def test_mock_commands_refuse_when_disabled(db):
    stack = LobbyStack(db)
    bot = FakeBot(FakeChannel())
    mock = MockPlayers(stack.players, stack.ratings, stack.lobbies, False)
    cog = Lobby(bot, stack.lobbies, mock)  # type: ignore[arg-type]
    ix = FakeInteraction(ADMIN)
    await cog.mock_create.callback(cog, ix, 3, None)  # type: ignore[arg-type]
    assert "ENABLE_MOCK_PLAYERS" in ix.sent[0]["content"]


# --- rendering ----


async def test_open_lobby_embed_lists_ten_slots_and_marks_mocks(env):
    lobby_id, _ = await open_as(env)
    await env.mock.fill(lobby_id, 2, ADMIN)
    snap = await env.stack.lobbies.snapshot(lobby_id)
    text = text_of(build_lobby_embed(snap))
    assert "Jogadores (3/10)" in text
    assert "★" not in text  # no per-lobby role choices on the message
    assert "🤖" in text  # mock players are marked
    assert text.count("—") >= 7  # empty slots


async def test_ready_check_embed_shows_progress_reserves_and_a_countdown(env):
    lobby_id, _ = await open_as(env)
    await env.mock.fill(lobby_id, 10, ADMIN)  # nine in the roster... ten mocks make one reserve
    await env.mock.ready_all(lobby_id)
    snap = await env.stack.lobbies.snapshot(lobby_id)
    text = text_of(build_lobby_embed(snap))
    assert "Ready-check" in text
    assert "✅" in text
    assert "⏳" in text  # the real player has not confirmed
    assert "Reservas" in text
    assert "<t:" in text


async def test_no_raw_ratings_anywhere_in_the_player_facing_embeds(env):
    lobby_id, _ = await open_as(env)
    await env.mock.fill(lobby_id, 9, ADMIN)
    await env.mock.ready_all(lobby_id)
    await env.stack.lobbies.ready(lobby_id, 1)
    snap = await env.stack.lobbies.snapshot(lobby_id)
    text = text_of(build_lobby_embed(snap)).lower()
    for hidden in ("mu ", "sigma", "%", "rating"):
        assert hidden not in text


def test_text_helpers():
    proposal = SimpleNamespace(band="clear", favoured="red")
    assert band_text(proposal) == "Vantagem clara Vermelho"  # type: ignore[arg-type]
    assert band_text(SimpleNamespace(band="even", favoured=None)) == "Equilibrado"  # type: ignore[arg-type]
    assert "Muito desequilibrado" in band_text(SimpleNamespace(band="lopsided", favoured="blue"))  # type: ignore[arg-type]


def test_off_role_players_are_flagged_and_best_role_players_are_not():
    on_best = SimpleNamespace(player_id=1, nickname="Ana", role="MID", fit=5, best=5)
    second = SimpleNamespace(player_id=2, nickname="Rui", role="TOP", fit=4, best=5)
    unknown = SimpleNamespace(player_id=3, nickname="Eva", role="ADC", fit=None, best=3)
    no_data = SimpleNamespace(player_id=4, nickname="Zé", role="JUNGLE", fit=None, best=None)
    assert fit_note(on_best) == ""  # type: ignore[arg-type]
    assert fit_note(second) == " · 4/5"  # type: ignore[arg-type]
    assert fit_note(unknown) == " · ?/5"  # type: ignore[arg-type]
    assert fit_note(no_data) == ""  # type: ignore[arg-type]
    text = team_text([on_best, second])  # type: ignore[list-item]
    assert text.splitlines()[0] == "**Mid** — Ana"  # no note for someone on their best role
    assert "**Top** — Rui · 4/5" in text


async def test_the_proposal_embed_notes_who_plays_off_their_best_role(env):
    lobby_id, _ = await open_as(env)
    await env.mock.create(9, ADMIN, main_role="MID")  # everyone mains MID: eight must move
    await env.mock.fill(lobby_id, 9, ADMIN)
    await env.mock.ready_all(lobby_id)
    await env.stack.players.set_declared(1, "MID", 5)
    update = await env.stack.lobbies.ready(lobby_id, 1)
    text = text_of(build_lobby_embed(update.snapshot))
    assert text.count("/5") >= 6  # most players are off their best role and are flagged


def test_ping_text_helper_returns_none_when_everyone_is_ready_or_a_mock():
    lobby = SimpleNamespace(status="READY_CHECK", ready_deadline=None)
    snapshot = SimpleNamespace(lobby=lobby, roster=[])
    assert ready_check_ping(snapshot, False) is None  # type: ignore[arg-type]


# --- extension wiring ----


async def test_the_extension_registers_commands_and_a_persistent_view():
    bot = KDBot(load_settings(ENV))
    async with bot:
        await bot.db.connect()
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        cog: Any = bot.get_cog("Lobby")
        assert cog is not None
        assert cog.mock.enabled is True
        assert cog.service.params.ready_check_seconds == 120
        top = {c.name: c for c in bot.tree.get_commands()}
        lobby_commands: Any = top["lobby"]
        mock_commands: Any = top["mock"]
        assert {c.name for c in lobby_commands.commands} == {"open", "show", "cancel"}
        assert {c.name for c in mock_commands.commands} == {
            "create",
            "fill",
            "ready",
            "drop",
            "list",
            "clear",
        }
        for command in mock_commands.commands:  # every mock command is allowlist-gated
            assert any(check.__name__ == "_admin_predicate" for check in command.checks)
        cog.sweeper.cancel()
        for ext in reversed(EXTENSIONS):
            await bot.unload_extension(ext)
        await bot.db.close()
