"""/history, /stats and /leaderboard: the rules (service), the embeds (render) and the commands."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from lobby_fakes import ADMIN, GUILD, LobbyStack, sides_of, start_game

from bot.features.players.profile_embed import NOT_REGISTERED
from bot.features.players.repository import Player
from bot.features.players.service import PlayerError
from bot.features.ratings.ladder import DEFAULT_LADDER
from bot.features.stats.cog import Stats
from bot.features.stats.render import (
    MAX_BOARD_ROWS,
    board_line,
    build_board_embed,
    build_history_embed,
    build_stats_embed,
    tally_line,
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
    assert "⏳ 1 jogo por validar (ainda não contam)" in (embed.description or "")
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
    assert f"#{first}" in plain and "Jogos: 1V 1D (50%) · 2 jogos" in plain
    assert "Últimos" not in plain  # nothing was cut, so no "last n" note
    sides = {history.lines[0].role: "<:role:1>"}
    icons = {"Kaisa": "<:champ_Kaisa:2>"}
    rich = build_history_embed(history, NOW, sides, icons, names).description or ""
    assert "<:champ_Kaisa:2> 8/2/11" in rich and "<:role:1>" in rich
    assert "🔵" not in rich and "🔴" not in rich  # the team side is not shown
    assert "# Histórico · " in plain  # large heading
    embed = build_history_embed(history, NOW, {}, {}, {}, "https://cdn.invalid/a.png")
    assert embed.footer.text is None  # no footer
    assert embed.thumbnail.url == "https://cdn.invalid/a.png"
    assert embed.color == discord.Color.blurple()  # no color given: no League rank
    gold = discord.Color(0xCD8837)
    assert build_history_embed(history, NOW, {}, {}, {}, None, gold).color == gold
    cut = replace(history, overall=Tally(6, 4))  # more games than the lines shown
    assert "Últimos 2 jogos:" in (build_history_embed(cut, NOW, {}, {}, {}).description or "")


async def test_an_empty_history_and_empty_boards_say_so(stack, service):
    history = await service.history(GUILD, 1)
    assert (build_history_embed(history, NOW, {}, {}, {}).description or "").endswith(
        "Ainda não há jogos confirmados."
    )
    stats = await service.stats(GUILD, 1)
    assert (build_stats_embed(stats, {}, {}, {}).description or "").endswith(
        "Ainda não há jogos confirmados."
    )
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
    # heading, rule and blank line, the rows, and the "… e mais" line
    assert description.count("\n") == MAX_BOARD_ROWS + 3
    assert description.endswith("… e mais 10.") and len(description) < 4096


def test_nicknames_are_escaped():
    entry = BoardEntry(1, player(1, "*x*"), Tally(3, 2), tier_key="no_box", division=1)
    assert "\\*x\\*" in (build_board_embed(RANK, [entry], DEFAULT_LADDER).description or "")


# --- commands ----


class FakeInteraction:
    def __init__(self, user_id: int, guild_id: int | None = GUILD) -> None:
        self.user = SimpleNamespace(
            id=user_id, display_avatar=SimpleNamespace(url="https://cdn.invalid/a.png")
        )
        self.guild_id = guild_id
        self.guild = None  # tests that need the leader's avatar set a fake guild
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
    embed = ix.sent[0]["embed"]
    assert "ephemeral" not in ix.sent[0] and "Player 01" in embed.description
    assert "8/2/11" in embed.description
    assert embed.thumbnail.url == "https://cdn.invalid/a.png"
    target = SimpleNamespace(id=2, display_avatar=SimpleNamespace(url="https://cdn.invalid/b.png"))
    ix = FakeInteraction(1)
    await cog.stats.callback(cog, ix, target)  # type: ignore[arg-type]
    assert "Player 02" in ix.sent[0]["embed"].description
    assert ix.sent[0]["allowed_mentions"].users is False


async def test_the_commands_refuse_politely(stack, cog):
    for command, args in ((cog.history, ()), (cog.stats, ()), (cog.leaderboard, ())):
        ix = FakeInteraction(404)
        await command.callback(cog, ix, *args)  # type: ignore[arg-type]
        if command is cog.leaderboard:
            assert isinstance(ix.sent[0]["embed"], discord.Embed)
        else:
            assert ix.sent[0]["ephemeral"] is True
            expected = NOT_REGISTERED if command is cog.history else None
            if expected:
                assert ix.sent[0]["content"] == expected
            else:
                assert "registado" in ix.sent[0]["content"]
        ix = FakeInteraction(1, guild_id=None)
        await command.callback(cog, ix, *args)  # type: ignore[arg-type]
        assert ix.sent[0]["ephemeral"] is True and "servidor" in ix.sent[0]["content"]


async def test_leaderboard_picks_the_board_from_the_choice(stack, cog):
    ix = FakeInteraction(1)
    choice = SimpleNamespace(value=KDA)
    await cog.leaderboard.callback(cog, ix, choice)  # type: ignore[arg-type]
    assert "KDA Scrims" in ix.sent[0]["embed"].description
    ix = FakeInteraction(1)
    await cog.leaderboard.callback(cog, ix, None)  # type: ignore[arg-type]
    assert "Rank Scrims" in ix.sent[0]["embed"].description


def test_a_single_game_is_not_pluralised():
    assert tally_line(Tally(1, 0)) == "Jogos: 1V 0D (100%) · 1 jogo"
    assert tally_line(Tally()) == "Jogos: 0V 0D (—) · 0 jogos"


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


async def test_the_stats_embed_follows_the_profile_layout(stack, service):
    first, _ = await play(stack, player_wins=True)
    await validated(stack, first, 1, "kaisa", 8, 2, 11)
    stats = await service.stats(GUILD, 1)
    roles = {"TOP": "<:top:1>", "JUNGLE": "<:jg:2>", "MID": "<:mid:3>"}
    roles |= {"ADC": "<:adc:4>", "SUPPORT": "<:sup:5>"}
    icons = {"Kaisa": "<:champ_Kaisa:2>"}
    embed = build_stats_embed(
        stats, roles, icons, {}, "https://cdn.invalid/a.png", discord.Color(1)
    )
    description = embed.description or ""
    assert description.startswith("# Estatísticas · Player 01")
    assert (
        "Jogos: 1V 0D (100%) · 1 jogo" in description and "KDA: 9.50 (8.0/2.0/11.0)" in description
    )
    by_role = next(f.value for f in embed.fields if f.name == "Por role") or ""
    assert len(by_role.splitlines()) == 5  # always the five roles
    assert by_role.count("0V 0D") == 4
    pairs = next(f.value for f in embed.fields if f.name == "Campeões Scrims por role") or ""
    assert pairs.endswith("<:champ_Kaisa:2> 1V 0D · KDA 9.50") and pairs.startswith(
        "<:"
    )  # role first
    assert embed.footer.text is None and embed.color == discord.Color(1)
    assert embed.thumbnail.url == "https://cdn.invalid/a.png"


async def test_history_and_stats_carry_the_league_tier_colour(stack, service, cog):
    await stack.ratings.set_self_reported_rank(1, "GOLD", "II", 1)
    for command in (cog.history, cog.stats):
        ix = FakeInteraction(1)
        await command.callback(cog, ix)  # type: ignore[arg-type]
        assert ix.sent[0]["embed"].color == discord.Color(0xCD8837)


def test_the_leaderboard_has_no_footer():
    assert build_board_embed(RANK, [], DEFAULT_LADDER).footer.text is None


def test_board_rows_have_medals_for_the_podium_and_follow_each_boards_numbers():
    ana, bia, cai, dan = (player(i, n) for i, n in enumerate(("Ana", "Bia", "Cai", "Dan"), 1))
    kda = Kda(5, 30, 10, 20)
    win = [BoardEntry(1, ana, Tally(5, 2)), BoardEntry(2, bia, Tally(3, 4))]
    assert board_line(WIN_RATE, win[0], DEFAULT_LADDER) == "🥇 **Ana** · 71% · 5V 2D · 7 jogos"
    assert board_line(WIN_RATE, win[1], DEFAULT_LADDER) == "🥈 **Bia** · 43% · 3V 4D · 7 jogos"
    third = BoardEntry(3, cai, Tally(5, 0), kda)
    assert board_line(KDA, third, DEFAULT_LADDER) == "🥉 **Cai** · 5.00 (6.0/2.0/4.0) · 5 jogos"
    fourth = BoardEntry(4, dan, Tally(5, 0), kda)
    assert board_line(KDA, fourth, DEFAULT_LADDER).startswith("4. **Dan**")


def test_boards_have_a_heading_a_rule_a_colour_each_and_the_leaders_avatar():
    entries = [BoardEntry(1, player(1, "Ana"), Tally(5, 2), Kda(5, 30, 10, 20))]
    colours = set()
    for board in (RANK, WIN_RATE, KDA):
        embed = build_board_embed(board, entries, DEFAULT_LADDER, "https://cdn.invalid/a.png")
        lines = (embed.description or "").splitlines()
        assert lines[0].startswith("# ") and lines[1].startswith("-# 1 jogadores · ")
        assert embed.footer.text is None and embed.title is None
        assert embed.thumbnail.url == "https://cdn.invalid/a.png"
        colours.add(embed.color)
    assert len(colours) == 3
    empty = build_board_embed(WIN_RATE, [], DEFAULT_LADDER, "https://cdn.invalid/a.png")
    assert (empty.description or "").endswith("Ainda ninguém tem 5 jogos.")
    assert empty.thumbnail.url is None  # nobody to show


async def test_the_leaderboard_shows_the_leaders_avatar_when_the_member_is_reachable(stack, cog):
    for _ in range(5):
        await play(stack)

    class Guild:
        def __init__(self, found: bool) -> None:
            self.found = found

        def get_member(self, discord_id: int):
            avatar = SimpleNamespace(url=f"https://cdn.invalid/{discord_id}.png")
            return SimpleNamespace(display_avatar=avatar) if self.found else None

        async def fetch_member(self, discord_id: int):
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "unknown member")  # type: ignore[arg-type]

    ix = FakeInteraction(1)
    ix.guild = Guild(True)  # type: ignore[assignment]
    await cog.leaderboard.callback(cog, ix, None)  # type: ignore[arg-type]
    assert ix.sent[0]["embed"].thumbnail.url.startswith("https://cdn.invalid/")
    ix = FakeInteraction(1)
    ix.guild = Guild(False)  # type: ignore[assignment]  # left the server: no picture, no error
    await cog.leaderboard.callback(cog, ix, None)  # type: ignore[arg-type]
    assert ix.sent[0]["embed"].thumbnail.url is None
