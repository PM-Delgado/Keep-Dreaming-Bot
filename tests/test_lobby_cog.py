"""Lobby commands, buttons, pings, sweeper and rendering, against a fake Discord."""

from types import SimpleNamespace
from typing import Any

import discord
import pytest_asyncio
from lobby_fakes import ADMIN, CHANNEL, GUILD, LobbyStack, sides_of, start_game

from bot.app import EXTENSIONS, KDBot
from bot.config import load_settings
from bot.features.lobby.cog import Lobby
from bot.features.lobby.mock import MockPlayers
from bot.features.lobby.render import (
    band_text,
    build_list_embed,
    build_lobby_embed,
    fit_note,
    ready_check_ping,
    result_summary,
    scheduled_ping,
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
    assert [b.custom_id for b in final["view"].children] == [
        "lobby:report_blue",
        "lobby:report_red",
        "lobby:cancel",
    ]


async def test_the_buttons_shown_depend_on_the_state(env):
    assert set(VISIBLE) == {
        "OPEN",
        "READY_CHECK",
        "TEAMS_PROPOSED",
        "IN_PROGRESS",
        "REPORTED",
        "CONTESTED",
    }
    expected = {
        "OPEN": ["lobby:join", "lobby:leave", "lobby:cancel"],
        "READY_CHECK": ["lobby:ready", "lobby:decline", "lobby:join", "lobby:cancel"],
        "TEAMS_PROPOSED": ["lobby:confirm", "lobby:leave", "lobby:cancel"],
        "IN_PROGRESS": ["lobby:report_blue", "lobby:report_red", "lobby:cancel"],
        "REPORTED": [
            "lobby:confirm_result",
            "lobby:contest",
            "lobby:report_blue",
            "lobby:report_red",
            "lobby:cancel",
        ],
        "CONTESTED": [
            "lobby:report_blue",
            "lobby:report_red",
            "lobby:confirm_result",
            "lobby:cancel",
        ],
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


# --- scheduled lobbies ----

WHEN = "23:00"  # 22:00 UTC; the fake clock starts at 20:00 UTC


async def schedule_as(env, user_id: int = 1, when: str = WHEN, note: str | None = None) -> int:
    await env.stack.register(1)
    ix = FakeInteraction(user_id)
    await env.cog.schedule_lobby.callback(env.cog, ix, when, note)
    [lobby] = await env.stack.lobbies.list_scheduled(GUILD)
    return lobby.lobby.id


async def test_schedule_posts_a_scheduled_message_and_remembers_it(env):
    lobby_id = await schedule_as(env, note="noite de flex")
    row = await env.stack.lobbies.repo.get(lobby_id)
    assert row is not None
    assert row.message_id == 777
    assert await env.stack.lobbies.open_lobby(GUILD) is None  # does not take the "open" slot


async def test_schedule_rejects_bad_times_with_an_ephemeral_hint(env):
    await env.stack.register(1)
    ix = FakeInteraction(1)
    await env.cog.schedule_lobby.callback(env.cog, ix, "blah", None)
    assert ix.sent[0]["ephemeral"] is True
    assert "21:00" in ix.sent[0]["content"]  # examples are shown
    assert await env.stack.lobbies.list_scheduled(GUILD) == []


async def test_the_scheduled_embed_shows_the_time_the_note_and_the_sign_ups(env):
    lobby_id = await schedule_as(env, note="noite de flex")
    snap = await env.stack.lobbies.snapshot(lobby_id)
    text = text_of(build_lobby_embed(snap))
    assert "Agendada" in text
    assert "noite de flex" in text
    assert f"<t:{int(snap.lobby.scheduled_for.timestamp())}:F>" in text
    assert "Jogadores (1/10)" in text
    assert "hora já passou" not in text


async def test_after_the_time_the_embed_says_it_waits_for_ten_players(env):
    lobby_id = await schedule_as(env)
    env.stack.clock.advance(hours=3)
    await env.cog.service.process_schedules()
    text = text_of(build_lobby_embed(await env.stack.lobbies.snapshot(lobby_id)))
    assert "hora já passou" in text


async def test_list_shows_the_open_lobby_and_the_scheduled_ones(env):
    await open_as(env)
    await env.stack.register(2, start=1)
    ix = FakeInteraction(2)
    ix._original = FakeMessage(888)  # a different Discord message
    await env.cog.schedule_lobby.callback(env.cog, ix, "amanhã 21:00", "x")
    ix = FakeInteraction(1)
    await env.cog.list_lobbies.callback(env.cog, ix)
    text = text_of(ix.sent[0]["embed"])
    assert "Aberta" in text
    assert "1/10" in text
    assert text.count("#") == 2
    assert "x" in text


def test_the_list_embed_is_friendly_when_there_is_nothing():
    assert "Não há" in (build_list_embed(None, []).description or "")


async def test_show_and_cancel_accept_a_lobby_number(env):
    lobby_id = await schedule_as(env)
    ix = FakeInteraction(1)
    await env.cog.show_lobby.callback(env.cog, ix, lobby_id)
    assert "Agendada" in text_of(ix.sent[0]["embed"])
    ix = FakeInteraction(1)
    await env.cog.show_lobby.callback(env.cog, ix, 4242)
    assert "Não encontrei" in ix.sent[0]["content"]
    ix = FakeInteraction(1)
    await env.cog.cancel_lobby.callback(env.cog, ix, lobby_id)
    row = await env.stack.lobbies.repo.get(lobby_id)
    assert row is not None
    assert row.status == "CANCELLED"


async def test_a_lobby_of_another_server_cannot_be_reached_by_number(env):
    lobby_id = await schedule_as(env)
    ix = FakeInteraction(1)
    ix.guild_id = GUILD + 1
    await env.cog.cancel_lobby.callback(env.cog, ix, lobby_id)
    assert "Não encontrei" in ix.sent[0]["content"]
    row = await env.stack.lobbies.repo.get(lobby_id)
    assert row is not None
    assert row.status == "OPEN"


async def test_the_sweeper_sends_the_reminder_once_and_pings_real_players_only(env):
    lobby_id = await schedule_as(env)
    await env.mock.fill(lobby_id, 3, ADMIN)
    env.stack.clock.advance(minutes=95)  # 21:35 UTC: 25 minutes before the start
    await env.cog.sweeper.coro(env.cog)
    await env.cog.sweeper.coro(env.cog)
    [ping] = env.channel.sent
    assert "Lembrete" in ping["content"]
    assert "<@1>" in ping["content"]
    assert "<@-" not in ping["content"]
    assert "4/10" in ping["content"]
    assert [u.id for u in ping["allowed_mentions"].users] == [1]
    assert env.channel.messages[777].edits  # the message was refreshed too


async def test_a_short_lobby_at_the_time_pings_everyone_signed_up(env):
    await schedule_as(env)
    env.stack.clock.advance(hours=2, minutes=1)
    await env.cog.sweeper.coro(env.cog)
    pings = [m["content"] for m in env.channel.sent]
    assert any("só há 1/10" in text for text in pings)


async def test_mock_fill_can_target_a_scheduled_lobby_and_it_starts_at_the_time(env):
    lobby_id = await schedule_as(env)
    ix = FakeInteraction(ADMIN)
    await env.cog.mock_fill.callback(env.cog, ix, 9, lobby_id)  # type: ignore[arg-type]
    snap = await env.stack.lobbies.snapshot(lobby_id)
    assert snap.lobby.status == "OPEN"  # ten sign-ups, but the time has not come
    assert len(snap.roster) == 10
    env.stack.clock.advance(hours=2, minutes=1)
    await env.cog.sweeper.coro(env.cog)
    snap = await env.stack.lobbies.snapshot(lobby_id)
    assert snap.lobby.status == "READY_CHECK"


def test_scheduled_ping_is_none_without_real_players():
    lobby = SimpleNamespace(id=1, scheduled_for=None)
    assert scheduled_ping(SimpleNamespace(lobby=lobby, roster=[], reserves=[]), "reminder") is None  # type: ignore[arg-type]


# --- results: buttons, pings, summaries, /match, mock helpers ----


async def started_game(env) -> tuple[int, dict[str, list[int]]]:
    """Ten real players (1..10) with confirmed teams; the lobby message is message 777."""
    lobby_id, match = await start_game(env.stack)
    await env.stack.lobbies.attach_message(lobby_id, 777, CHANNEL)
    return lobby_id, await sides_of(env.stack, match.id)


def button_ids(interaction: FakeInteraction) -> list[str]:
    return [b.custom_id for b in interaction.edited[-1]["view"].children]


async def test_reporting_through_the_buttons_asks_the_other_team_to_confirm(env):
    lobby_id, sides = await started_game(env)
    ix = FakeInteraction(sides["blue"][0], 777)
    await env.cog.on_action(ix, "report_blue")  # type: ignore[arg-type]
    text = text_of(ix.edited[0]["embed"])
    assert "reportou" in text
    assert "Azul ganhou" in text
    assert button_ids(ix)[:2] == ["lobby:confirm_result", "lobby:contest"]
    [ping] = env.channel.sent
    for pid in sides["red"]:
        assert f"<@{pid}>" in ping["content"]
    assert not any(f"<@{pid}>" in ping["content"] for pid in sides["blue"])
    assert "Confirmem" in ping["content"]


async def test_the_other_team_confirms_and_the_summary_shows_placement_progress(env):
    lobby_id, sides = await started_game(env)
    await env.cog.on_action(FakeInteraction(sides["blue"][0], 777), "report_blue")  # type: ignore[arg-type]
    env.channel.sent.clear()
    ix = FakeInteraction(sides["red"][0], 777)
    await env.cog.on_action(ix, "confirm_result")  # type: ignore[arg-type]
    embed_text = text_of(ix.edited[0]["embed"])
    assert "Terminada" in embed_text
    assert "Azul ganhou" in embed_text
    assert ix.edited[0]["view"] is None  # no buttons on a finished game
    [summary] = env.channel.sent
    assert "Azul ganhou" in summary["content"]
    assert "Em colocação" in summary["content"]  # nobody has a tier yet
    assert "1/5" in summary["content"]
    for hidden in ("mu", "sigma", "rating"):
        assert hidden not in summary["content"].lower()


async def test_strangers_and_the_reporting_team_get_a_clear_refusal(env):
    lobby_id, sides = await started_game(env)
    await env.stack.register(1, start=60)
    ix = FakeInteraction(60, 777)
    await env.cog.on_action(ix, "report_blue")  # type: ignore[arg-type]
    assert "quem jogou" in ix.sent[0]["content"]
    await env.cog.on_action(FakeInteraction(sides["blue"][0], 777), "report_blue")  # type: ignore[arg-type]
    ix = FakeInteraction(sides["blue"][1], 777)
    await env.cog.on_action(ix, "confirm_result")  # type: ignore[arg-type]
    assert "outra equipa" in ix.sent[0]["content"]
    assert ix.edited == []


async def test_contesting_changes_the_message_and_the_buttons(env):
    lobby_id, sides = await started_game(env)
    await env.cog.on_action(FakeInteraction(sides["blue"][0], 777), "report_blue")  # type: ignore[arg-type]
    ix = FakeInteraction(sides["red"][0], 777)
    await env.cog.on_action(ix, "contest")  # type: ignore[arg-type]
    assert "contestado" in text_of(ix.edited[0]["embed"])
    assert "lobby:contest" not in button_ids(ix)
    assert "lobby:confirm_result" in button_ids(ix)


async def test_the_sweeper_auto_confirms_refreshes_the_message_and_posts_the_summary(env):
    lobby_id, sides = await started_game(env)
    await env.cog.on_action(FakeInteraction(sides["blue"][0], 777), "report_red")  # type: ignore[arg-type]
    env.channel.sent.clear()
    env.stack.clock.advance(minutes=31)
    await env.cog.sweeper.coro(env.cog)
    edit = env.channel.messages[777].edits[-1]
    assert "confirmado automaticamente" in text_of(edit["embed"])
    [summary] = env.channel.sent
    assert "Vermelho ganhou" in summary["content"]
    assert "automaticamente" in summary["content"]


async def test_the_sweeper_nudges_unreported_games_once(env):
    await started_game(env)
    env.stack.clock.advance(minutes=91)
    await env.cog.sweeper.coro(env.cog)
    await env.cog.sweeper.coro(env.cog)
    [ping] = env.channel.sent
    assert "ainda não tem resultado" in ping["content"]
    assert all(f"<@{pid}>" in ping["content"] for pid in range(1, 11))


async def test_a_promotion_is_announced_once_placement_is_done(env):
    await env.stack.register(1, start=ADMIN)
    last = None
    for game in range(5):
        lobby_id, match = await start_game(env.stack)
        await env.stack.lobbies.attach_message(lobby_id, 700 + game, CHANNEL)
        last = await env.stack.lobbies.report_result(lobby_id, ADMIN, "blue")
    assert last is not None
    names = {
        p.id: p.nickname
        for p in (await env.stack.players.repo.get_many(list(range(1, 11)))).values()
    }
    summary = result_summary(
        last.snapshot, last.events, last.changes, names, env.stack.ratings.ladder
    )
    assert summary is not None
    assert summary.count("terminou a colocação") == 10
    assert "Em colocação" not in summary
    for hidden in ("mu", "sigma"):
        assert hidden not in summary.lower().replace("muito", "")


async def test_match_show_lists_teams_and_history(env):
    lobby_id, sides = await started_game(env)
    await env.cog.on_action(FakeInteraction(sides["blue"][0], 777), "report_blue")  # type: ignore[arg-type]
    ix = FakeInteraction(1)
    await env.cog.match_show.callback(env.cog, ix, None)  # type: ignore[arg-type]
    text = text_of(ix.sent[0]["embed"])
    assert "Resultado por confirmar" in text
    assert "Vencedor: **Azul**" in text
    assert "Player 01" in text
    assert "reportou" in text
    ix = FakeInteraction(1)
    await env.cog.match_show.callback(env.cog, ix, 999)  # type: ignore[arg-type]
    assert "Não encontrei" in ix.sent[0]["content"]


async def test_match_fix_and_void_are_for_admins_and_update_the_message(env):
    lobby_id, sides = await started_game(env)
    await env.stack.register(1, start=ADMIN)
    await env.cog.on_action(FakeInteraction(ADMIN, 777), "report_blue")  # type: ignore[arg-type]
    match = await env.stack.match_repo.get_by_lobby(lobby_id)
    assert match is not None
    env.channel.sent.clear()
    blue = SimpleNamespace(value="red")
    ix = FakeInteraction(ADMIN)
    await env.cog.match_fix.callback(env.cog, ix, match.id, blue)  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == "Feito."
    assert "Vermelho ganhou" in text_of(env.channel.messages[777].edits[-1]["embed"])
    assert "corrigido" in env.channel.sent[0]["content"]
    ix = FakeInteraction(ADMIN)
    await env.cog.match_void.callback(env.cog, ix, match.id, "erro de registo")  # type: ignore[arg-type]
    assert "Lobby encerrada (erro de registo)" in text_of(
        env.channel.messages[777].edits[-1]["embed"]
    )
    assert "anulada" in env.channel.sent[-1]["content"]
    ix = FakeInteraction(1)
    await env.cog.match_void.callback(env.cog, ix, match.id, "x")  # type: ignore[arg-type]
    assert "admin" in ix.sent[0]["content"] or "anulado" in ix.sent[0]["content"]


async def test_mock_players_can_report_and_confirm_so_one_account_can_test_the_flow(env):
    lobby_id, _ = await open_as(env)
    await env.mock.fill(lobby_id, 9, ADMIN)
    await env.mock.ready_all(lobby_id)
    await env.stack.lobbies.ready(lobby_id, 1)
    await env.stack.lobbies.confirm(lobby_id, 1)

    ix = FakeInteraction(ADMIN)
    await env.cog.mock_report.callback(env.cog, ix, SimpleNamespace(value="blue"))  # type: ignore[arg-type]
    assert "reportado" in ix.sent[0]["content"]
    assert "reportou" in text_of(env.channel.messages[777].edits[-1]["embed"])
    ix = FakeInteraction(ADMIN)
    await env.cog.mock_confirm.callback(env.cog, ix)  # type: ignore[arg-type]
    assert "confirmado" in ix.sent[0]["content"]
    assert "Terminada" in text_of(env.channel.messages[777].edits[-1]["embed"])
    stored = await env.stack.ratings.repo.get_stored(1)
    assert stored is not None
    assert stored.games == 1


async def test_clearing_mock_players_removes_their_games_from_real_ratings(env):
    lobby_id, _ = await open_as(env)
    await env.mock.fill(lobby_id, 9, ADMIN)
    await env.mock.ready_all(lobby_id)
    await env.stack.lobbies.ready(lobby_id, 1)
    await env.stack.lobbies.confirm(lobby_id, 1)
    await env.mock.report(lobby_id, "blue")
    await env.mock.confirm(lobby_id)
    before = await env.stack.ratings.repo.get_stored(1)
    assert before is not None
    assert before.games == 1
    removed = await env.mock.clear()
    assert removed == 9
    after = await env.stack.ratings.repo.get_stored(1)
    assert after is not None
    assert after.games == 0
    assert await env.stack.match_repo.get_by_lobby(lobby_id) is None


async def test_mock_report_needs_a_started_game(env):
    lobby_id, _ = await open_as(env)
    ix = FakeInteraction(ADMIN)
    await env.cog.mock_report.callback(env.cog, ix, SimpleNamespace(value="blue"))  # type: ignore[arg-type]
    assert "Não" in ix.sent[0]["content"]


# --- /match stats, set, validate, pending ----


async def confirmed_game_with_admin(env) -> tuple[int, dict[str, list[int]]]:
    """A confirmed game (admin report) with the bundled champion list loaded; returns the sides."""
    await env.stack.champions.start()
    lobby_id, sides = await started_game(env)
    await env.stack.register(1, start=ADMIN)
    await env.stack.lobbies.report_result(lobby_id, ADMIN, "blue")
    match = await env.stack.match_repo.get_by_lobby(lobby_id)
    assert match is not None
    return match.id, sides


async def test_the_result_summary_points_players_to_match_stats(env):
    lobby_id, sides = await started_game(env)
    await env.stack.register(1, start=ADMIN)
    update = await env.stack.lobbies.report_result(lobby_id, ADMIN, "blue")
    match_id = update.snapshot.match.id  # type: ignore[union-attr]
    summary = result_summary(
        update.snapshot, update.events, update.changes, {}, env.stack.ratings.ladder
    )
    assert summary is not None
    assert f"/match stats` (jogo #{match_id})" in summary


async def test_match_stats_records_pending_stats_and_match_show_marks_them(env):
    match_id, sides = await confirmed_game_with_admin(env)
    player = sides["blue"][0]
    ix = FakeInteraction(player)
    await env.cog.match_stats.callback(env.cog, ix, "kaisa", 8, 2, 11, match_id)  # type: ignore[arg-type]
    assert ix.sent[0]["ephemeral"] is True
    assert f"Jogo #{match_id}" in ix.sent[0]["content"] and "Kai'Sa 8/2/11" in ix.sent[0]["content"]
    ix = FakeInteraction(1)
    await env.cog.match_show.callback(env.cog, ix, match_id)  # type: ignore[arg-type]
    text = text_of(ix.sent[0]["embed"])
    assert "Kai'Sa 8/2/11 ⏳" in text
    assert "registou stats de" in text
    await env.stack.matches.validate_stats(ADMIN, match_id)
    ix = FakeInteraction(1)
    await env.cog.match_show.callback(env.cog, ix, match_id)  # type: ignore[arg-type]
    assert "Kai'Sa 8/2/11 ✅" in text_of(ix.sent[0]["embed"])


async def test_match_show_puts_the_champion_icon_next_to_the_player_and_the_riot_notice(env):
    match_id, sides = await confirmed_game_with_admin(env)
    await env.stack.matches.enter_stats(sides["blue"][0], "Kaisa", 8, 2, 11, match_id)

    ix = FakeInteraction(1)
    await env.cog.match_show.callback(env.cog, ix, match_id)  # type: ignore[arg-type]
    embed = ix.sent[0]["embed"]
    assert "Kai'Sa 8/2/11" in text_of(embed) and "<:champ_" not in text_of(embed)
    assert not embed.footer.text  # no Riot image on screen, no notice needed

    async def upload(name: str, image: bytes) -> int:
        return 4242

    async def png(url: str) -> bytes:
        return b"PNG"

    env.stack.champions.get_bytes = png
    await env.stack.champions.sync_icons({}, upload)
    ix = FakeInteraction(1)
    await env.cog.match_show.callback(env.cog, ix, match_id)  # type: ignore[arg-type]
    embed = ix.sent[0]["embed"]
    assert " · <:champ_Kaisa:4242> Kai'Sa 8/2/11 ⏳" in text_of(embed)
    assert "isn't endorsed by Riot Games" in embed.footer.text

    env.stack.champions.icons = False  # the switch hides them again
    ix = FakeInteraction(1)
    await env.cog.match_show.callback(env.cog, ix, match_id)  # type: ignore[arg-type]
    assert "<:champ_" not in text_of(ix.sent[0]["embed"]) and not ix.sent[0]["embed"].footer.text


async def test_match_stats_errors_are_private_and_explain_themselves(env):
    match_id, sides = await confirmed_game_with_admin(env)
    ix = FakeInteraction(sides["blue"][0])
    await env.cog.match_stats.callback(env.cog, ix, "nobody", 1, 2, 3, match_id)  # type: ignore[arg-type]
    assert ix.sent[0]["ephemeral"] is True and "Não conheço" in ix.sent[0]["content"]
    ix = FakeInteraction(404)
    await env.cog.match_stats.callback(env.cog, ix, "Ahri", 1, 2, 3, match_id)  # type: ignore[arg-type]
    assert "não está registado" in ix.sent[0]["content"]


async def test_the_champion_autocomplete_offers_the_matching_champions(env):
    await env.stack.champions.start()
    ix = FakeInteraction(1)
    choices = await env.cog._champion_autocomplete(ix, "kai")
    assert ("Kai'Sa", "Kaisa") in [(c.name, c.value) for c in choices]
    assert len(await env.cog._champion_autocomplete(ix, "")) == 25


async def test_match_set_validate_and_pending_are_for_admins_and_private(env):
    match_id, sides = await confirmed_game_with_admin(env)
    blue, red = sides["blue"][0], sides["red"][0]
    await env.stack.matches.enter_stats(blue, "Ahri", 1, 2, 3, match_id)
    ix = FakeInteraction(ADMIN)
    await env.cog.match_pending.callback(env.cog, ix)  # type: ignore[arg-type]
    pending = text_of(ix.sent[0]["embed"])
    assert ix.sent[0]["ephemeral"] is True
    assert f"#{match_id}" in pending
    assert "Ahri 1/2/3" in pending
    member = SimpleNamespace(id=red, display_name="Red One")
    ix = FakeInteraction(ADMIN)
    await env.cog.match_set.callback(
        env.cog,
        ix,
        match_id,
        member,
        "Zed",
        5,
        6,
        7,
        SimpleNamespace(value="MID"),  # type: ignore[arg-type]
    )
    assert "Zed 5/6/7 (Mid)" in ix.sent[0]["content"] and "Red One" in ix.sent[0]["content"]
    ix = FakeInteraction(ADMIN)
    await env.cog.match_validate.callback(env.cog, ix, match_id, None)  # type: ignore[arg-type]
    assert "1 stats validadas" in ix.sent[0]["content"]
    ix = FakeInteraction(ADMIN)
    await env.cog.match_pending.callback(env.cog, ix)  # type: ignore[arg-type]
    assert "Não há stats por validar" in text_of(ix.sent[0]["embed"])
    ix = FakeInteraction(ADMIN)
    await env.cog.match_validate.callback(env.cog, ix, match_id, None)  # type: ignore[arg-type]
    assert "Não há stats por validar" in ix.sent[0]["content"]
    ix = FakeInteraction(blue)  # a plain player is refused by the service even if the check slipped
    await env.cog.match_pending.callback(env.cog, ix)  # type: ignore[arg-type]
    assert "admin" in ix.sent[0]["content"]


async def test_mock_players_can_enter_and_validate_stats_for_a_test_game(env):
    await env.stack.champions.start()
    lobby_id, _ = await open_as(env)
    await env.mock.fill(lobby_id, 9, ADMIN)
    await env.mock.ready_all(lobby_id)
    await env.stack.lobbies.ready(lobby_id, 1)
    await env.stack.lobbies.confirm(lobby_id, 1)
    await env.mock.report(lobby_id, "blue")
    await env.mock.confirm(lobby_id)
    await env.stack.register(1, start=ADMIN)
    ix = FakeInteraction(ADMIN)
    await env.cog.mock_stats.callback(env.cog, ix, None, False)  # type: ignore[arg-type]
    assert "9 jogadores de teste com stats (por validar)" in ix.sent[0]["content"]
    pending = await env.stack.matches.pending_stats(GUILD, ADMIN)
    assert len(pending) == 9 and all(p.player_id != 1 for p in pending)
    ix = FakeInteraction(ADMIN)
    await env.cog.mock_stats.callback(env.cog, ix, None, True)  # type: ignore[arg-type]
    assert "(validadas)" in ix.sent[0]["content"]
    assert await env.stack.matches.pending_stats(GUILD, ADMIN) == []
    assert await env.mock.clear() == 9  # test games, stats included, leave no trace
    assert await env.stack.match_repo.get_by_lobby(lobby_id) is None


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
        assert {c.name for c in lobby_commands.commands} == {
            "open",
            "schedule",
            "list",
            "show",
            "cancel",
        }
        match_commands: Any = top["match"]
        assert {c.name for c in match_commands.commands} == {
            "show",
            "void",
            "fix",
            "stats",
            "set",
            "validate",
            "pending",
        }
        for name in ("void", "fix", "set", "validate", "pending"):  # admin-only
            command = next(c for c in match_commands.commands if c.name == name)
            assert any(check.__name__ == "_admin_predicate" for check in command.checks), name
        assert {c.name for c in mock_commands.commands} == {
            "create",
            "fill",
            "ready",
            "drop",
            "list",
            "clear",
            "report",
            "confirm",
            "stats",
        }
        for command in mock_commands.commands:  # every mock command is allowlist-gated
            assert any(check.__name__ == "_admin_predicate" for check in command.checks)
        cog.sweeper.cancel()
        for ext in reversed(EXTENSIONS):
            await bot.unload_extension(ext)
        await bot.db.close()
