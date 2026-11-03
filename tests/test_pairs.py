"""/rivalry: the pair rules (pure), the service over real games, the embeds, the tab buttons."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from lobby_fakes import ADMIN, GUILD, LobbyStack, sides_of, start_game

from bot.features.players.repository import Player
from bot.features.players.service import NOT_REGISTERED
from bot.features.stats.cog import Stats
from bot.features.stats.pair_render import (
    NO_CONFRONTATIONS,
    NO_TEAMWORK,
    NOTHING_SHARED,
    SMALL_SAMPLE,
    build_pair_embed,
    champion_ids,
    thumbnail_player,
)
from bot.features.stats.pair_views import PAIR_ID, PairTabButton, pair_view
from bot.features.stats.pairs import (
    MIN_PAIR_PERCENT,
    TAB_GAMES,
    TAB_H2H,
    TAB_SYNERGY,
    A,
    B,
    build_pair,
    head_to_head,
    initial_tab,
    shared_games,
    synergy,
)
from bot.features.stats.repository import GameRow, StatsRepository
from bot.features.stats.service import SAME_PLAYER, PairError, StatsService
from bot.features.stats.tally import Tally

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def player(pid: int, nickname: str) -> Player:
    return Player(pid, 100 + pid, nickname, NOW, None)


def row(
    match: int,
    pid: int,
    side: str,
    role: str,
    won: bool,
    *,
    status: str = "none",
    champion: str | None = None,
    kda: tuple[int, int, int] | None = None,
) -> GameRow:
    k, d, a = kda or (None, None, None)
    return GameRow(
        match, pid, side, role, won, NOW - timedelta(days=100 - match), champion, k, d, a, status
    )


def games_between(spec: list[tuple[str, bool, str, str]]):
    """`spec` is oldest first: (relation, A won, A's role, B's role). Returns newest first."""
    rows_a, rows_b = [], []
    for match, (relation, a_won, role_a, role_b) in enumerate(spec, start=1):
        side_b = "blue" if relation == "together" else "red"
        won_b = a_won if relation == "together" else not a_won
        rows_a.append(row(match, 1, "blue", role_a, a_won))
        rows_b.append(row(match, 2, side_b, role_b, won_b))
    return list(reversed(rows_a)), list(reversed(rows_b))


# --- pure rules ----


def test_teammates_and_opponents_are_told_apart_and_games_are_joined_by_match():
    rows_a, rows_b = games_between(
        [("against", True, "MID", "MID"), ("together", True, "ADC", "SUPPORT")]
    )
    rows_a.append(row(99, 1, "blue", "TOP", True))  # a game B did not play
    rows_a.sort(key=lambda r: -r.match_id)
    shared = shared_games(rows_a, rows_b)
    assert [g.match_id for g in shared] == [2, 1]
    assert [g.together for g in shared] == [True, False]


def test_head_to_head_counts_streaks_and_form():
    # oldest first: A A B A A A  -> current streak 3 (A), longest A 3, longest B 1
    spec = [("against", w, "MID", "TOP") for w in (True, True, False, True, True, True)]
    h2h = head_to_head(shared_games(*games_between(spec)))
    assert (h2h.a_wins, h2h.b_wins, h2h.games) == (5, 1, 6)
    assert (h2h.current.holder, h2h.current.length) == (A, 3)
    assert (h2h.longest_a, h2h.longest_b) == (3, 1)
    assert h2h.last == (True, True, True, False, True)  # newest first, five at most


def test_the_current_streak_belongs_to_whoever_won_last():
    spec = [("against", w, "MID", "TOP") for w in (True, True, False)]
    h2h = head_to_head(shared_games(*games_between(spec)))
    assert (h2h.current.holder, h2h.current.length) == (B, 1)
    assert (h2h.longest_a, h2h.longest_b) == (2, 1)


def test_head_to_head_ignores_teammates_and_counts_the_same_lane_from_a():
    spec = [
        ("against", True, "MID", "MID"),
        ("against", False, "MID", "MID"),
        ("against", True, "MID", "TOP"),
        ("together", True, "ADC", "SUPPORT"),
    ]
    h2h = head_to_head(shared_games(*games_between(spec)))
    assert h2h.games == 3
    assert h2h.same_lane == Tally(1, 1)


def test_synergy_groups_role_combinations_most_played_first_and_keeps_the_top_three():
    spec = [
        ("together", True, "ADC", "SUPPORT"),
        ("together", True, "ADC", "SUPPORT"),
        ("together", False, "ADC", "SUPPORT"),
        ("together", True, "MID", "JUNGLE"),
        ("together", False, "TOP", "MID"),
        ("together", False, "JUNGLE", "TOP"),
        ("against", True, "MID", "MID"),  # not teamwork
    ]
    result = synergy(shared_games(*games_between(spec)))
    assert result.tally == Tally(3, 3)
    assert [c.roles for c in result.combos] == [
        ("ADC", "SUPPORT"),
        ("MID", "JUNGLE"),
        ("TOP", "MID"),
    ]
    assert result.combos[0].tally == Tally(2, 1)


def test_the_role_actually_played_is_what_the_rows_carry():
    # GameRow.role is already `played_role` or the assigned role (the repository's COALESCE).
    spec = [("against", True, "SUPPORT", "SUPPORT")]
    assert head_to_head(shared_games(*games_between(spec))).same_lane == Tally(1, 0)


def test_initial_tab_follows_the_relation_with_more_games_and_ties_go_to_confrontations():
    def pair_of(spec):
        a, b = player(1, "A"), player(2, "B")
        return build_pair(a, b, *games_between(spec))

    against = ("against", True, "MID", "TOP")
    together = ("together", True, "ADC", "SUPPORT")
    assert initial_tab(pair_of([against, together])) == TAB_H2H
    assert initial_tab(pair_of([together, together, against])) == TAB_SYNERGY
    assert initial_tab(pair_of([])) == TAB_H2H


def test_the_history_keeps_the_ten_latest_but_counts_every_shared_game():
    pair = build_pair(
        player(1, "A"),
        player(2, "B"),
        *games_between([("against", True, "MID", "TOP")] * 13),
    )
    assert pair.total == 13 and len(pair.recent) == 10
    assert pair.recent[0].match_id == 13


# --- embeds ----

PEDRO, JOAO = player(1, "Pedro"), player(2, "João")


def pair_for(spec):
    return build_pair(PEDRO, JOAO, *games_between(spec))


def embed_for(pair, tab, roles=None, icons=None, names=None):
    return build_pair_embed(pair, tab, NOW, roles or {}, icons or {}, names or {})


def test_confrontations_show_the_record_percentages_streak_and_form():
    spec = [("against", w, "MID", "TOP") for w in (False, True, True, True, False, True)]
    text = embed_for(pair_for(spec), TAB_H2H).description or ""
    assert "# ⚔️ Rivalidade · Pedro vs João" in text
    assert "**Pedro** 4V · 2V **João** · 6 jogos" in text
    assert "Pedro 67% · João 33%" in text
    assert "🔥" not in text  # the streak is one game: not worth a line
    assert "Maior série: Pedro 3 · João 1" in text
    assert "Últimos 5 (Pedro): ✅ ❌ ✅ ✅ ✅" in text
    assert SMALL_SAMPLE not in text


def test_a_streak_of_two_or_more_gets_its_line_and_a_small_sample_hides_the_percentages():
    spec = [("against", w, "MID", "TOP") for w in (False, True, True)]
    text = embed_for(pair_for(spec), TAB_H2H).description or ""
    assert "🔥 Pedro ganhou os últimos 2." in text
    assert SMALL_SAMPLE in text and "%" not in text.replace(SMALL_SAMPLE, "")
    assert "Na mesma lane" not in text


def test_the_percentage_starts_at_the_minimum_sample():
    wins = [("against", i < 3, "MID", "TOP") for i in range(MIN_PAIR_PERCENT - 1)]
    assert "%" in (embed_for(pair_for([*wins, wins[0]]), TAB_H2H).description or "")
    assert SMALL_SAMPLE in (embed_for(pair_for(wins), TAB_H2H).description or "")


def test_same_lane_line_and_single_game_form():
    text = embed_for(pair_for([("against", True, "MID", "MID")]), TAB_H2H).description or ""
    assert "Na mesma lane: 1 jogo · Pedro 1V 0D" in text
    assert "Último (Pedro): ✅" in text


def test_synergy_lists_role_combinations_with_icons_or_names():
    spec = [("together", i != 2, "ADC", "SUPPORT") for i in range(5)] + [
        ("together", True, "MID", "JUNGLE")
    ]
    pair = pair_for(spec)
    plain = embed_for(pair, TAB_SYNERGY).description or ""
    assert "# 🤝 Sinergia · Pedro + João" in plain
    assert "Jogos: 5V 1D (83%) · 6 jogos" in plain
    assert "ADC" in plain or "Adc" in plain
    icons = embed_for(pair, TAB_SYNERGY, roles={"ADC": "<:adc:1>", "SUPPORT": "<:sup:2>"})
    assert "<:adc:1> + <:sup:2> 4V 1D (80%)" in (icons.description or "")


def test_synergy_with_a_small_sample_has_counts_and_the_note():
    text = embed_for(pair_for([("together", True, "ADC", "SUPPORT")]), TAB_SYNERGY).description
    assert "Jogos: 1V 0D · 1 jogo" in (text or "") and SMALL_SAMPLE in (text or "")


def test_empty_relations_and_no_shared_game_have_their_own_message():
    only_against = pair_for([("against", True, "MID", "TOP")])
    assert NO_TEAMWORK in (embed_for(only_against, TAB_SYNERGY).description or "")
    only_together = pair_for([("together", True, "MID", "TOP")])
    assert NO_CONFRONTATIONS in (embed_for(only_together, TAB_H2H).description or "")
    nothing = pair_for([])
    for tab in (TAB_H2H, TAB_SYNERGY, TAB_GAMES):
        assert NOTHING_SHARED in (embed_for(nothing, tab).description or "")


def test_the_games_tab_shows_both_sides_with_champions_and_pending_marks():
    rows_a, rows_b = games_between(
        [("against", True, "MID", "MID"), ("together", False, "ADC", "SUPPORT")]
    )
    rows_a[0] = row(2, 1, "blue", "ADC", False, status="validated", champion="Jinx", kda=(10, 1, 6))
    rows_b[1] = row(1, 2, "red", "MID", False, status="pending", champion="Zed", kda=(1, 1, 1))
    pair = build_pair(PEDRO, JOAO, rows_a, rows_b)
    assert champion_ids(pair) == ["Jinx"]  # pending stats are never shown
    text = embed_for(pair, TAB_GAMES, icons={"Jinx": "<:jinx:9>"}).description or ""
    assert "2 jogos:" in text
    assert "🤝 ❌ ADC <:jinx:9> 10/1/6 + Suporte sem stats" in text
    assert "stats por validar ⏳" in text and "⏳ 1 jogo por validar" in text


def test_the_games_tab_says_when_it_is_cut():
    pair = pair_for([("against", True, "MID", "TOP")] * 12)
    assert "Últimos 10 de 12 jogos:" in (embed_for(pair, TAB_GAMES).description or "")


def test_names_are_escaped_and_the_avatar_goes_to_whoever_leads():
    star = build_pair(player(1, "*A*"), JOAO, *games_between([("against", True, "MID", "TOP")]))
    assert "\\*A\\*" in (embed_for(star, TAB_H2H).description or "")
    ahead = pair_for([("against", False, "MID", "TOP")] * 2)
    assert thumbnail_player(ahead, TAB_H2H) is JOAO
    assert thumbnail_player(ahead, TAB_SYNERGY) is PEDRO
    assert build_pair_embed(
        ahead, TAB_H2H, NOW, {}, {}, {}, "https://cdn.invalid/j.png"
    ).thumbnail.url


# --- the service over real games ----


@pytest_asyncio.fixture
async def stack(db):
    stack = LobbyStack(db)
    await stack.champions.start()
    await stack.register(10)
    await stack.register(1, start=ADMIN)
    return stack


@pytest_asyncio.fixture
async def service(db, stack):
    return StatsService(StatsRepository(db), stack.players, stack.ratings)


async def play(
    stack: LobbyStack, winner_side: str | None = None
) -> tuple[int, dict[str, list[int]]]:
    """One confirmed game with players 1..10; `winner_side` blue or red (default blue)."""
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, ADMIN, winner_side or "blue")
    stack.clock.advance(hours=1)
    return match.id, sides


def side_of(sides: dict[str, list[int]], pid: int) -> str:
    return "blue" if pid in sides["blue"] else "red"


async def test_the_service_pairs_two_players_over_confirmed_games(stack, service):
    first, sides = await play(stack, "blue")
    second, _ = await play(stack, "red")
    # players 1 and 2 are teammates in some games and opponents in others: both relations add up
    pair = await service.pair(GUILD, 1, 2)
    assert pair.total == 2
    assert pair.h2h.games + pair.synergy.tally.games == 2
    assert {g.match_id for g in pair.recent} == {first, second}
    mirrored = await service.pair(GUILD, 2, 1)
    assert mirrored.h2h.a_wins == pair.h2h.b_wins and mirrored.total == 2


async def test_unfinished_and_voided_games_are_left_out_and_a_fix_flips_the_result(stack, service):
    done, sides = await play(stack, "blue")
    voided, _ = await play(stack, "blue")
    await stack.matches.void(voided, ADMIN, "teste")
    await start_game(stack)  # in progress: never counted
    assert (await service.pair(GUILD, 1, 2)).total == 1
    before = await service.pair(GUILD, 1, 2)
    await stack.matches.fix(done, ADMIN, "red")
    after = await service.pair(GUILD, 1, 2)
    assert (after.h2h.a_wins, after.h2h.b_wins) == (before.h2h.b_wins, before.h2h.a_wins)
    assert after.synergy.tally.wins == before.synergy.tally.losses


async def test_the_other_server_is_not_included(stack, service):
    await play(stack)
    assert (await service.pair(GUILD + 1, 1, 2)).total == 0


async def test_refusals_name_who_is_not_registered(stack, service):
    with pytest.raises(PairError) as same:
        await service.pair(GUILD, 1, 1)
    assert str(same.value) == SAME_PLAYER and same.value.discord_id is None
    with pytest.raises(PairError) as missing:
        await service.pair(GUILD, 1, 404)
    assert missing.value.discord_id == 404
    with pytest.raises(PairError) as caller:
        await service.pair(GUILD, 404, 1)
    assert caller.value.discord_id == 404 and str(caller.value) == NOT_REGISTERED


# --- the command and the buttons ----


class FakeInteraction:
    def __init__(self, user_id: int, guild_id: int | None = GUILD, members: dict | None = None):
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.guild = SimpleNamespace(get_member=(members or {}).get) if members else None
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        outer = self

        class Response:
            async def send_message(self, content=None, **kwargs):
                outer.sent.append({"content": content, **kwargs})

            async def edit_message(self, **kwargs):
                outer.edited.append(kwargs)

        self.response = Response()


def member(discord_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=discord_id,
        display_name=name,
        display_avatar=SimpleNamespace(url="https://cdn.invalid/x"),
    )


@pytest_asyncio.fixture
async def cog(stack, service):
    return Stats(SimpleNamespace(), service, stack.champions)  # type: ignore[arg-type]


async def test_rivalry_defaults_to_the_caller_and_replies_publicly_with_buttons(stack, cog):
    await play(stack)
    ix = FakeInteraction(1)
    await cog.rivalry.callback(cog, ix, member(2, "Dois"))  # type: ignore[arg-type]
    reply = ix.sent[0]
    assert "ephemeral" not in reply and reply["allowed_mentions"].users is False
    assert "Player 01" in (reply["embed"].description or "")
    assert [b.custom_id for b in reply["view"].children][0].startswith("pair:")
    assert sum(b.item.disabled for b in reply["view"].children) == 1  # the open tab


async def test_rivalry_compares_two_other_players_when_both_are_given(stack, cog):
    await play(stack)
    ix = FakeInteraction(1)
    await cog.rivalry.callback(cog, ix, member(3, "Três"), member(4, "Quatro"))  # type: ignore[arg-type]
    text = ix.sent[0]["embed"].description
    assert "Player 03" in text and "Player 04" in text
    assert ix.sent[0]["view"].children[0].custom_id.endswith(":1")  # owner = the caller


async def test_rivalry_without_a_shared_game_has_no_buttons(stack, cog):
    ix = FakeInteraction(1)
    await cog.rivalry.callback(cog, ix, member(2, "Dois"))  # type: ignore[arg-type]
    assert NOTHING_SHARED in ix.sent[0]["embed"].description and "view" not in ix.sent[0]


async def test_rivalry_refuses_politely(stack, cog):
    ix = FakeInteraction(1, guild_id=None)
    await cog.rivalry.callback(cog, ix, member(2, "Dois"))  # type: ignore[arg-type]
    assert ix.sent[0]["ephemeral"] is True and "servidor" in ix.sent[0]["content"]
    ix = FakeInteraction(1)
    await cog.rivalry.callback(cog, ix, member(1, "Um"))  # type: ignore[arg-type]
    assert ix.sent[0]["ephemeral"] is True and ix.sent[0]["content"] == SAME_PLAYER
    ix = FakeInteraction(404)
    await cog.rivalry.callback(cog, ix, member(2, "Dois"))  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == NOT_REGISTERED
    ix = FakeInteraction(1, members={404: member(404, "Rui *")})
    await cog.rivalry.callback(cog, ix, member(404, "Rui *"))  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == "KD Scrims: **Rui \\*** não está registado."
    assert ix.sent[0]["ephemeral"] is True


async def test_the_owner_flips_the_public_message_anybody_else_gets_a_private_copy(stack, cog):
    await play(stack)
    owner = FakeInteraction(1)
    await cog.flip_pair(owner, TAB_GAMES, 1, 2, 1)  # type: ignore[arg-type]
    assert not owner.sent and "Jogos" in owner.edited[0]["embed"].description
    assert owner.edited[0]["view"].children[2].item.disabled  # Jogos is now the open tab
    guest = FakeInteraction(5)
    await cog.flip_pair(guest, TAB_SYNERGY, 1, 2, 1)  # type: ignore[arg-type]
    assert not guest.edited and guest.sent[0]["ephemeral"] is True
    assert guest.sent[0]["view"].children[0].custom_id.endswith(":5")  # their own buttons


async def test_a_click_after_a_player_left_is_refused_privately(stack, cog):
    await play(stack)
    ix = FakeInteraction(1)
    await cog.flip_pair(ix, TAB_H2H, 1, 404, 1)  # type: ignore[arg-type]
    assert not ix.edited and ix.sent[0]["ephemeral"] is True


def test_the_button_ids_round_trip_and_fit_discord_limits():
    view = pair_view(TAB_SYNERGY, 2**62, 2**62 + 1, 2**62 + 2)
    ids = [button.custom_id for button in view.children]  # type: ignore[attr-defined]
    assert all(len(i) <= 100 for i in ids)
    import re

    parsed = [re.fullmatch(PAIR_ID, i) for i in ids]
    assert all(parsed) and [m["tab"] for m in parsed if m] == [TAB_H2H, TAB_SYNERGY, TAB_GAMES]
    button = view.children[1]
    assert isinstance(button, PairTabButton) and button.item.disabled and button.tab == TAB_SYNERGY
    assert view.timeout is None
    assert B == "b"


def test_the_cog_registers_the_tab_buttons(stack):
    class Bot:
        def __init__(self):
            self.items = []

        def add_dynamic_items(self, *items):
            self.items.extend(items)

        def remove_dynamic_items(self, *items):
            self.items = [i for i in self.items if i not in items]

    import asyncio

    bot = Bot()
    cog = Stats(bot, None, None)  # type: ignore[arg-type]
    asyncio.run(cog.cog_load())
    assert bot.items == [PairTabButton]
    asyncio.run(cog.cog_unload())
    assert bot.items == []
    assert isinstance(discord.Embed(), discord.Embed)
