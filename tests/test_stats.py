"""/history, /stats and /leaderboard: the rules (service), the embeds (render) and the commands."""

from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from lobby_fakes import ADMIN, GUILD, LobbyStack, sides_of, start_game

from bot.features.players.repository import Player
from bot.features.players.service import PlayerError
from bot.features.ratings.ladder import DEFAULT_LADDER
from bot.features.stats.cog import Stats
from bot.features.stats.render import (
    MAX_BOARD_ROWS,
    build_board_embed,
    build_history_embed,
    build_stats_embed,
    kda_summary,
    tally_text,
)
from bot.features.stats.repository import StatsRepository
from bot.features.stats.service import (
    KDA,
    RANK,
    WIN_RATE,
    BoardEntry,
    Kda,
    StatsService,
    Tally,
    _with_positions,
)

OTHER_GUILD = GUILD + 1


@pytest_asyncio.fixture
async def stack(db):
    stack = LobbyStack(db)
    await stack.champions.start()
    await stack.register(10)
    await stack.register(1, start=ADMIN)  # the admin is registered but never plays
    return stack


@pytest_asyncio.fixture
async def service(db, stack):
    return StatsService(StatsRepository(db), stack.players, stack.ratings)


async def play(stack: LobbyStack, player_wins: bool = True) -> tuple[int, dict[str, list[int]]]:
    """One confirmed game with players 1..10; player 1's side wins or loses. Returns the sides."""
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    mine = "blue" if 1 in sides["blue"] else "red"
    other = "red" if mine == "blue" else "blue"
    await stack.lobbies.report_result(lobby_id, ADMIN, mine if player_wins else other)
    stack.clock.advance(hours=1)
    return match.id, sides


async def validated(stack: LobbyStack, match_id: int, player: int, champion: str, k, d, a):
    await stack.matches.set_stats(ADMIN, match_id, player, champion, k, d, a)


# --- /history ----


async def test_history_lists_confirmed_games_newest_first_with_the_overall_record(stack, service):
    first, _ = await play(stack, player_wins=True)
    second, _ = await play(stack, player_wins=False)
    history = await service.history(GUILD, 1)
    assert [g.match_id for g in history.lines] == [second, first]
    assert [g.won for g in history.lines] == [False, True]
    assert history.overall == Tally(1, 1)


async def test_history_limit_shows_the_latest_but_counts_every_game(stack, service):
    for _ in range(3):
        await play(stack)
    history = await service.history(GUILD, 1, limit=2)
    assert len(history.lines) == 2
    assert history.overall.games == 3


async def test_games_that_are_not_confirmed_never_show_up(stack, service):
    confirmed, _ = await play(stack)
    voided, _ = await play(stack)
    await stack.matches.void(voided, ADMIN, "teste")
    await start_game(stack)  # in progress, nobody reported
    history = await service.history(GUILD, 1)
    assert [g.match_id for g in history.lines] == [confirmed]


async def test_a_corrected_result_is_reflected_at_once(stack, service):
    match_id, sides = await play(stack, player_wins=True)
    await stack.matches.fix(match_id, ADMIN, "red" if 1 in sides["blue"] else "blue")
    history = await service.history(GUILD, 1)
    assert history.lines[0].won is False


async def test_pending_stats_are_hidden_until_an_admin_validates_them(stack, service):
    match_id, _ = await play(stack)
    await stack.matches.enter_stats(1, "kaisa", 8, 2, 11, match_id)
    line = (await service.history(GUILD, 1)).lines[0]
    assert line.pending is True and line.champion is None and line.kills is None
    await stack.matches.validate_stats(ADMIN, match_id, 1)
    line = (await service.history(GUILD, 1)).lines[0]
    assert (line.pending, line.champion, line.kills, line.deaths, line.assists) == (
        False,
        "Kaisa",
        8,
        2,
        11,
    )


async def test_the_role_actually_played_wins_over_the_assigned_one(stack, service):
    match_id, _ = await play(stack)
    assigned = (await service.history(GUILD, 1)).lines[0].role
    played = "SUPPORT" if assigned != "SUPPORT" else "TOP"
    await stack.matches.set_stats(ADMIN, match_id, 1, "kaisa", 1, 1, 1, played_role=played)
    assert (await service.history(GUILD, 1)).lines[0].role == played


async def test_another_server_sees_nothing_and_unregistered_players_are_refused(stack, service):
    await play(stack)
    assert (await service.history(OTHER_GUILD, 1)).lines == []
    with pytest.raises(PlayerError):
        await service.history(GUILD, 404)
    with pytest.raises(PlayerError):
        await service.stats(GUILD, 404)


# --- /stats ----


async def test_stats_aggregate_by_role_champion_and_champion_x_role(stack, service):
    first, _ = await play(stack)
    second, _ = await play(stack)
    await play(stack)  # no stats for the third game
    await validated(stack, first, 1, "kaisa", 8, 2, 11)
    await validated(stack, second, 1, "kaisa", 4, 0, 3)
    stats = await service.stats(GUILD, 1)
    assert stats.overall.games == 3
    assert stats.kda is not None and (stats.kda.games, stats.kda.ratio) == (2, 13.0)
    assert sum(b.tally.games for b in stats.by_role) == 3  # roles count every confirmed game
    assert [b.key for b in stats.by_champion] == [("Kaisa",)]
    assert stats.by_champion[0].tally.games == 2  # champions: validated games only
    assert len(stats.by_champion_role) >= 1
    assert sum(b.tally.games for b in stats.by_champion_role) == 2


async def test_pending_stats_do_not_count_but_are_reported(stack, service):
    match_id, _ = await play(stack)
    await stack.matches.enter_stats(1, "kaisa", 8, 2, 11, match_id)
    stats = await service.stats(GUILD, 1)
    assert stats.kda is None and stats.by_champion == [] and stats.pending == 1
    embed = build_stats_embed(stats, {}, {}, {})
    assert "1 jogo com stats por validar" in (embed.description or "")
    assert [f.name for f in embed.fields] == ["Por role"]


async def test_the_kda_ratio_uses_totals_and_floors_deaths_at_one(stack, service):
    first, _ = await play(stack)
    second, _ = await play(stack)
    await validated(stack, first, 1, "kaisa", 10, 0, 0)  # deathless game
    await validated(stack, second, 1, "kaisa", 0, 0, 2)
    stats = await service.stats(GUILD, 1)
    assert stats.kda is not None and stats.kda.ratio == 12.0  # (10 + 2) / max(1, 0)


# --- /leaderboard ----


def player(pid: int, nickname: str) -> Player:
    return Player(pid, pid, nickname, datetime(2026, 1, 1, tzinfo=UTC), None)


def test_ties_share_a_position_and_are_listed_a_to_z():
    entry = lambda pid, nick: BoardEntry(0, player(pid, nick), Tally())  # noqa: E731
    scored = [
        (2.0, entry(1, "Zé")),
        (3.0, entry(2, "Rui")),
        (2.0, entry(3, "ana")),
        (1.0, entry(4, "Bia")),
    ]
    board = _with_positions(scored)
    assert [(e.position, e.player.nickname) for e in board] == [
        (1, "Rui"),
        (2, "ana"),
        (2, "Zé"),
        (4, "Bia"),
    ]


async def test_the_rank_board_lists_placed_players_only_with_tiers_and_no_ratings(stack, service):
    for _ in range(4):
        await play(stack)
    assert await service.leaderboard(GUILD, RANK) == []  # still in placement
    await play(stack)
    board = await service.leaderboard(GUILD, RANK)
    assert len(board) == 10
    assert board[0].position == 1
    assert [e.position for e in board] == sorted(e.position for e in board)
    embed = build_board_embed(RANK, board, DEFAULT_LADDER)
    text = embed.description or ""
    assert all(e.tier_key and DEFAULT_LADDER.display_name(e.tier_key) in text for e in board)
    assert "▰" in text or "▱" in text  # every row has its progress bar
    assert "mu" not in text.lower().split()


async def test_win_rate_and_kda_boards_need_five_games(stack, service, db):
    ids = []
    for _ in range(5):
        ids.append((await play(stack))[0])
    wins = await service.leaderboard(GUILD, WIN_RATE)
    assert len(wins) == 10 and all(e.tally.games == 5 for e in wins)
    assert await service.leaderboard(GUILD, KDA) == []  # games, but no validated stats
    for match_id in ids:
        await validated(stack, match_id, 1, "kaisa", 5, 1, 5)
    for match_id in ids[:4]:
        await validated(stack, match_id, 2, "ahri", 1, 1, 1)  # only four validated games
    kda = await service.leaderboard(GUILD, KDA)
    assert [(e.position, e.player.nickname) for e in kda] == [(1, "Player 01")]


async def test_mock_players_and_players_who_left_are_never_listed(stack, service, db):
    for _ in range(5):
        await play(stack)
    await db.conn.execute("UPDATE players SET is_mock = 1 WHERE discord_id = 3")
    await db.conn.execute(
        "UPDATE players SET left_at = '2026-08-01T00:00:00+00:00' WHERE discord_id = 4"
    )
    await db.conn.commit()
    for board in (RANK, WIN_RATE):
        names = {e.player.nickname for e in await service.leaderboard(GUILD, board)}
        assert "Player 03" not in names and "Player 04" not in names
        assert len(names) == 8


async def test_an_unknown_board_is_a_programming_error(service):
    with pytest.raises(ValueError):
        await service.leaderboard(GUILD, "nope")


# --- embeds ----


NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


async def test_the_history_embed_has_icons_with_name_fallbacks_and_no_ratings(stack, service):
    first, _ = await play(stack, player_wins=True)
    second, _ = await play(stack, player_wins=False)
    await validated(stack, first, 1, "kaisa", 8, 2, 11)
    await stack.matches.enter_stats(1, "ahri", 1, 1, 1, second)
    history = await service.history(GUILD, 1)
    names = await stack.champions.names(["Kaisa"])
    plain = build_history_embed(history, NOW, {}, {}, names).description or ""
    assert "**Kai'Sa** 8/2/11" in plain and "stats por validar ⏳" in plain
    assert f"#{first}" in plain and "Últimos 2 de 2 jogos" in plain and "(50% · 2 jogos)" in plain
    sides = {history.lines[0].role: "<:role:1>"}
    icons = {"Kaisa": "<:champ_Kaisa:2>"}
    rich = build_history_embed(history, NOW, sides, icons, names).description or ""
    assert "<:champ_Kaisa:2> 8/2/11" in rich and "<:role:1>" in rich
    assert "Rank interno do grupo" in (
        build_history_embed(history, NOW, {}, {}, {}).footer.text or ""
    )


async def test_an_empty_history_and_empty_boards_say_so(stack, service):
    history = await service.history(GUILD, 1)
    assert (
        build_history_embed(history, NOW, {}, {}, {}).description
        == "Ainda não há jogos confirmados."
    )
    stats = await service.stats(GUILD, 1)
    assert build_stats_embed(stats, {}, {}, {}).description == "Ainda não há jogos confirmados."
    assert "colocação" in (build_board_embed(RANK, [], DEFAULT_LADDER).description or "")
    assert "5 jogos" in (build_board_embed(WIN_RATE, [], DEFAULT_LADDER).description or "")


def test_a_long_board_is_cut_and_stays_within_discords_limit():
    entries = [
        BoardEntry(
            i, player(i, f"Nome muito comprido {i:02d}"), Tally(i, 1), tier_key="no_box", division=1
        )
        for i in range(1, 41)
    ]
    embed = build_board_embed(RANK, entries, DEFAULT_LADDER)
    description = embed.description or ""
    assert description.count("\n") == MAX_BOARD_ROWS  # rows plus the "… e mais" line
    assert description.endswith("… e mais 10.") and len(description) < 4096


def test_nicknames_are_escaped():
    entry = BoardEntry(1, player(1, "*x*"), Tally(3, 2), tier_key="no_box", division=1)
    assert "\\*x\\*" in (build_board_embed(RANK, [entry], DEFAULT_LADDER).description or "")


# --- commands ----


class FakeInteraction:
    def __init__(self, user_id: int, guild_id: int | None = GUILD) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.sent: list[dict] = []
        outer = self

        class Response:
            async def send_message(self, content=None, **kwargs):
                outer.sent.append({"content": content, **kwargs})

        self.response = Response()


@pytest_asyncio.fixture
async def cog(stack, service):
    return Stats(SimpleNamespace(), service, stack.champions)  # type: ignore[arg-type]


async def test_history_and_stats_default_to_the_caller_and_are_public(stack, cog):
    match_id, _ = await play(stack)
    await validated(stack, match_id, 1, "kaisa", 8, 2, 11)
    ix = FakeInteraction(1)
    await cog.history.callback(cog, ix)  # type: ignore[arg-type]
    assert "ephemeral" not in ix.sent[0] and "Player 01" in ix.sent[0]["embed"].title
    assert "8/2/11" in ix.sent[0]["embed"].description
    target = SimpleNamespace(id=2)
    ix = FakeInteraction(1)
    await cog.stats.callback(cog, ix, target)  # type: ignore[arg-type]
    assert "Player 02" in ix.sent[0]["embed"].title
    assert ix.sent[0]["allowed_mentions"].users is False


async def test_the_commands_refuse_politely(stack, cog):
    for command, args in ((cog.history, ()), (cog.stats, ()), (cog.leaderboard, ())):
        ix = FakeInteraction(404)
        await command.callback(cog, ix, *args)  # type: ignore[arg-type]
        if command is cog.leaderboard:
            assert isinstance(ix.sent[0]["embed"], discord.Embed)
        else:
            assert ix.sent[0]["ephemeral"] is True and "não está registado" in ix.sent[0]["content"]
        ix = FakeInteraction(1, guild_id=None)
        await command.callback(cog, ix, *args)  # type: ignore[arg-type]
        assert ix.sent[0]["ephemeral"] is True and "servidor" in ix.sent[0]["content"]


async def test_leaderboard_picks_the_board_from_the_choice(stack, cog):
    ix = FakeInteraction(1)
    choice = SimpleNamespace(value=KDA)
    await cog.leaderboard.callback(cog, ix, choice)  # type: ignore[arg-type]
    assert "KDA" in ix.sent[0]["embed"].title
    ix = FakeInteraction(1)
    await cog.leaderboard.callback(cog, ix, None)  # type: ignore[arg-type]
    assert ix.sent[0]["embed"].title == "Ranking do grupo"


def test_a_single_game_is_not_pluralised():
    assert tally_text(Tally(1, 0)) == "1V 0D (100% · 1 jogo)"
    assert kda_summary(Kda(1, 5, 1, 5), 3).endswith("em 1 jogo")


async def test_the_rank_board_orders_a_division_by_progress_and_shows_the_bars(stack, service):
    for _ in range(5):
        await play(stack)
    ratings = stack.ratings
    for player, mu in ((1, 27.2), (2, 27.8), (3, 28.2), (4, 33.0)):
        pid = (await stack.players.require(player)).id
        await ratings.adjust(pid, mu, None, ADMIN, "test")
    board = await service.leaderboard(GUILD, RANK)
    order = [e.player.nickname for e in board]
    # KD esports first; then No Box II (28.2); then No Box III by progress: 27.8 before 27.2
    assert order.index("Player 04") < order.index("Player 03")
    assert order.index("Player 03") < order.index("Player 02") < order.index("Player 01")
    positions = [e.position for e in board]
    assert positions == list(range(1, len(board) + 1))  # the bar explains the order: no ties
    lines = (build_board_embed(RANK, board, DEFAULT_LADDER).description or "").splitlines()
    line = {name: next(x for x in lines if f"**{name}**" in x) for name in
            ("Player 04", "Player 03", "Player 02", "Player 01")}  # fmt: skip
    assert "▰" * 10 in line["Player 04"]  # the top step is a full bar
    assert "No Box II ▰▰▱▱▱▱▱▱▱▱" in line["Player 03"]
    assert "No Box III ▰▰▰▰▰▰▰▰▱▱" in line["Player 02"]
    assert "No Box III ▰▰▱▱▱▱▱▱▱▱" in line["Player 01"]
