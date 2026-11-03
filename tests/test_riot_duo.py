"""`/riot duo`: the pure rules (`duo.py`), the stored rows they read and `RiotService.duo`."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from riot_fakes import NOW, FakeRiot, Stack, days_ago

from bot.features.ratings.repository import from_db
from bot.features.riot.client import RankedGame
from bot.features.riot.duo import (
    MIN_COMPARE,
    DuoRow,
    DuoStats,
    build_duo,
    same_team,
    shared_games,
)
from bot.features.riot.service import SAME_PLAYER, DuoError
from bot.features.stats.tally import Tally

ADMIN = 99
A, B = 1, 2  # player ids in the pure tests
START = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)


def row(
    player: int,
    match: str,
    when: datetime,
    *,
    win: bool = True,
    team: int | None = None,
    position: str | None = "BOTTOM",
    champion: str | None = "Jinx",
    kda: tuple[int, int, int] = (5, 2, 7),
    duration: int = 1800,
    early: bool = False,
    account: int | None = None,
) -> DuoRow:
    return DuoRow(
        player_id=player,
        account_id=account if account is not None else player * 10,
        riot_id=f"P{player}#EUW",
        is_main=True,
        match_id=match,
        played_at=when,
        team_position=position,
        win=win,
        kills=kda[0],
        deaths=kda[1],
        assists=kda[2],
        duration_s=duration,
        early_surrender=early,
        champion=champion,
        team_id=team,
    )


def duo_game(
    match: str,
    when: datetime,
    *,
    win: bool = True,
    roles: tuple[str, str] = ("BOTTOM", "UTILITY"),
    champions: tuple[str, str] = ("Jinx", "Thresh"),
    kda_a: tuple[int, int, int] = (5, 2, 7),
    kda_b: tuple[int, int, int] = (1, 4, 15),
    team: int | None = 100,
) -> tuple[DuoRow, DuoRow]:
    """Both players' rows of one game they played on the same team."""
    return (
        row(
            A, match, when, win=win, team=team, position=roles[0], champion=champions[0], kda=kda_a
        ),
        row(
            B, match, when, win=win, team=team, position=roles[1], champion=champions[1], kda=kda_b
        ),
    )


def stats_of(games: list[tuple[DuoRow, DuoRow]], **kwargs) -> DuoStats:
    return build_duo([g[0] for g in games], [g[1] for g in games], **kwargs)


def day(n: int) -> datetime:
    return START + timedelta(days=n)


# --- who was on which team -------------------------------------------------------------------


def test_the_team_id_decides_when_both_rows_have_it():
    assert same_team(row(A, "m", START, team=100), row(B, "m", START, team=100))
    assert not same_team(row(A, "m", START, team=100), row(B, "m", START, team=200))
    # A contradiction between teamId and win is settled in favour of the teamId.
    assert same_team(
        row(A, "m", START, team=100, win=True), row(B, "m", START, team=100, win=False)
    )


def test_win_decides_when_a_row_has_no_team_id():
    assert same_team(row(A, "m", START, win=True), row(B, "m", START, win=True))
    assert not same_team(row(A, "m", START, win=True), row(B, "m", START, win=False))
    assert not same_team(row(A, "m", START, win=True, team=100), row(B, "m", START, win=False))


def test_opponents_are_not_teammates_and_not_in_without_either():
    together = duo_game("m1", day(0))
    rows_a = [together[0], row(A, "vs", day(1), win=True, team=100)]
    rows_b = [together[1], row(B, "vs", day(1), win=False, team=200)]
    games, ambiguous = shared_games(rows_a, rows_b)
    assert [g.match_id for g in games] == ["m1"] and ambiguous == 0
    stats = build_duo(rows_a, rows_b, coverage=day(-1))
    assert (
        stats.compare_a is not None and stats.compare_a.without.games == 0
    )  # "vs" is not "without"
    assert stats.compare_a.with_ == Tally(1, 0)


def test_remakes_never_count():
    short = duo_game("m1", day(0))
    early = (
        row(A, "m2", day(1), team=100, early=True),
        row(B, "m2", day(1), team=100, early=True),
    )
    tiny = (
        row(A, "m3", day(2), team=100, duration=200),
        row(B, "m3", day(2), team=100, duration=200),
    )
    stats = stats_of([short, early, tiny])
    assert stats.games == 1


def test_a_match_where_a_player_has_two_rows_is_skipped_and_counted():
    ok = duo_game("m1", day(0))
    mine = row(A, "m2", day(1), team=100, account=11)
    other_account = row(A, "m2", day(1), team=100, account=12)
    theirs = row(B, "m2", day(1), team=100)
    stats = build_duo([ok[0], mine, other_account], [ok[1], theirs], coverage=None)
    assert stats.games == 1 and stats.ambiguous == 1


def test_one_players_two_accounts_are_merged_and_each_match_counts_once():
    first = duo_game("m1", day(0))
    second = duo_game("m2", day(1))
    rows_a = [
        replace(first[0], account_id=11, riot_id="Main#EUW"),
        replace(second[0], account_id=12, riot_id="Smurf#EUW", is_main=False),
    ]
    stats = build_duo(rows_a, [first[1], second[1]], coverage=None)
    assert stats.games == 2
    assert {g.a.riot_id for g in stats.recent} == {"Main#EUW", "Smurf#EUW"}


def test_nothing_in_common_gives_empty_stats():
    stats = build_duo([row(A, "x", START, team=100)], [row(B, "y", START, team=100)], day(-5))
    assert stats.games == 0 and stats.recent == () and stats.first_game is None
    assert stats.compare_a is not None and stats.compare_a.without.games == 1


# --- streaks, pairs, records ----------------------------------------------------------------


def results(*wins: bool) -> list[tuple[DuoRow, DuoRow]]:
    """Games a day apart, oldest first, with these results."""
    return [duo_game(f"m{i}", day(i), win=w) for i, w in enumerate(wins)]


def test_streaks_current_and_longest():
    stats = stats_of(results(True, True, False, True, True, True, False))
    assert (stats.current.won, stats.current.length) == (False, 1)
    assert stats.longest_win is not None and stats.longest_win.length == 3
    assert stats.longest_win.first_at == day(3) and stats.longest_win.last_at == day(5)
    assert stats.longest_loss is not None and stats.longest_loss.length == 1
    ending = stats_of(results(False, True, True))
    assert (ending.current.won, ending.current.length) == (True, 2)


def test_no_loss_means_no_loss_record():
    stats = stats_of(results(True, True))
    assert stats.longest_loss is None and stats.longest_win is not None


def test_role_pairs_are_ordered_by_games_then_wins_then_role_order():
    games = (
        [duo_game(f"a{i}", day(i), roles=("BOTTOM", "UTILITY")) for i in range(3)]
        + [duo_game(f"b{i}", day(10 + i), roles=("MIDDLE", "JUNGLE")) for i in range(3)]
        + [duo_game("c0", day(20), roles=("TOP", "JUNGLE"), win=False)]
    )
    pairs = [(p.key, p.tally.games) for p in stats_of(games).role_pairs]
    # The two 3-game pairs tie on games and wins: the role order (TOP, JUNGLE, MID, ADC, SUPPORT).
    assert pairs == [(("MID", "JUNGLE"), 3), (("ADC", "SUPPORT"), 3), (("TOP", "JUNGLE"), 1)]


def test_a_game_without_a_role_or_champion_is_left_out_of_that_pair_list_only():
    games = [
        duo_game("m1", day(0), roles=("BOTTOM", "UTILITY")),
        (
            row(A, "m2", day(1), team=100, position=None, champion=None),
            row(B, "m2", day(1), team=100),
        ),
    ]
    stats = stats_of(games)
    assert stats.games == 2
    assert sum(p.tally.games for p in stats.role_pairs) == 1
    assert sum(p.tally.games for p in stats.champion_pairs) == 1


def test_best_pairs_need_a_minimum_of_games():
    few = [duo_game(f"m{i}", day(i)) for i in range(4)]  # 4 role games, 4 champion games
    stats = stats_of(few)
    assert stats.best_role_pair is None  # needs 5
    assert stats.best_champion_pair is not None  # needs 3
    five = stats_of([*few, duo_game("m4", day(4))])
    assert five.best_role_pair is not None and five.best_role_pair.key == ("ADC", "SUPPORT")


def test_the_best_pair_is_the_highest_win_rate_then_the_most_games():
    games = [
        duo_game(f"x{i}", day(i), champions=("Jinx", "Thresh"), win=i < 3) for i in range(4)
    ] + [duo_game(f"y{i}", day(10 + i), champions=("Kaisa", "Nautilus")) for i in range(3)]
    best = stats_of(games).best_champion_pair
    assert best is not None and best.key == ("Kaisa", "Nautilus")  # 3/3 beats 3/4


def test_kda_together_and_the_best_game_of_each_player():
    games = [
        duo_game("m1", day(0), kda_a=(10, 0, 5), kda_b=(0, 5, 5)),
        duo_game("m2", day(1), kda_a=(2, 2, 2), kda_b=(1, 1, 9)),
    ]
    stats = stats_of(games)
    assert (stats.kda_a.kills, stats.kda_a.deaths, stats.kda_a.assists) == (12, 2, 7)
    assert stats.kda_a.ratio == pytest.approx(19 / 2)
    assert stats.kda_a.average() == (6.0, 1.0, 3.5)
    assert stats.best_a is not None and stats.best_a.game.match_id == "m1"
    assert stats.best_b is not None and stats.best_b.game.match_id == "m2"  # 10.0 beats 2.0


def test_the_busiest_day_is_counted_in_lisbon_time():
    late = datetime(2026, 6, 1, 23, 30, tzinfo=UTC)  # 00:30 on 2 June in Lisbon (summer time)
    games = [
        duo_game("m1", late),
        duo_game("m2", late + timedelta(hours=1)),
        duo_game("m3", late - timedelta(days=3)),
    ]
    assert stats_of(games).busiest_day == ("2026-06-02", 2)


def test_recent_games_are_the_newest_ten_first_and_first_game_is_the_oldest():
    stats = stats_of([duo_game(f"m{i}", day(i)) for i in range(12)])
    assert [g.match_id for g in stats.recent] == [f"m{i}" for i in range(11, 1, -1)]
    assert stats.first_game == day(0)


# --- with and without, coverage -------------------------------------------------------------


def test_with_and_without_count_only_from_the_coverage_date():
    with_games = [duo_game(f"w{i}", day(10 + i), win=i < 8) for i in range(MIN_COMPARE)]
    alone_a = [row(A, f"a{i}", day(10 + i), win=i < 5, team=100) for i in range(MIN_COMPARE)]
    alone_b = [row(B, f"b{i}", day(10 + i), win=i < 2, team=100) for i in range(MIN_COMPARE)]
    old = [row(A, "old", day(-30), team=100)]  # before the coverage date: ignored
    stats = build_duo(
        [g[0] for g in with_games] + alone_a + old,
        [g[1] for g in with_games] + alone_b,
        coverage=day(0),
    )
    assert stats.compare_a is not None and stats.compare_b is not None
    assert stats.compare_a.with_ == Tally(8, 2) and stats.compare_a.without == Tally(5, 5)
    assert stats.compare_b.with_ == Tally(8, 2) and stats.compare_b.without == Tally(2, 8)
    assert stats.compare_a.shown and stats.compare_b.shown
    assert stats.before_coverage is False  # every shared game is after day 0


def test_the_comparison_is_hidden_below_the_minimum_on_either_side():
    with_games = [duo_game(f"w{i}", day(i)) for i in range(MIN_COMPARE)]
    stats = build_duo([g[0] for g in with_games], [g[1] for g in with_games], coverage=day(-1))
    assert stats.compare_a is not None
    assert stats.compare_a.with_.games == MIN_COMPARE
    assert stats.compare_a.without.games == 0 and not stats.compare_a.shown


def test_without_a_coverage_date_there_is_no_comparison_and_a_sync_is_reported():
    stats = stats_of([duo_game("m1", day(0))], coverage=None, syncing=("Alpha#EUW",))
    assert stats.compare_a is None and stats.compare_b is None
    assert stats.syncing == ("Alpha#EUW",) and stats.before_coverage is False


def test_games_older_than_the_coverage_date_are_flagged_but_still_counted():
    stats = stats_of([duo_game("old", day(0)), duo_game("new", day(20))], coverage=day(10))
    assert stats.games == 2 and stats.before_coverage is True
    assert stats.compare_a is not None and stats.compare_a.with_.games == 1


def test_a_remake_of_the_player_alone_is_not_a_without_game():
    alone = [row(A, "r", day(1), team=100, early=True)]
    stats = build_duo(alone, [], coverage=day(0))
    assert stats.compare_a is not None and stats.compare_a.without.games == 0


# --- the service ----------------------------------------------------------------------------


@pytest.fixture
def world() -> FakeRiot:
    w = FakeRiot()
    w.add_account("Alpha", "EUW", "puuid-alpha")
    w.add_account("Beta", "EUW", "puuid-beta")
    w.add_account("Smurf", "EUW", "puuid-smurf")
    return w


@pytest_asyncio.fixture
async def stack(db, world) -> Stack:
    s = Stack(db, world)
    await s.register(1, "Ana")
    await s.register(2, "Rui")
    await s.register(3, "Eva")
    return s


async def link_both(stack: Stack):
    alpha = (await stack.riot.link(1, "Alpha#EUW", None, ADMIN)).account
    beta = (await stack.riot.link(2, "Beta#EUW", None, ADMIN)).account
    await stack.riot.wait_for_background()
    return alpha, beta


def add_shared(world: FakeRiot, match: str, days: float, win: bool = True, team: int = 100):
    world.add_game("puuid-alpha", match, "BOTTOM", days_ago(NOW, days), win=win, team=team)
    world.add_game("puuid-beta", match, "UTILITY", days_ago(NOW, days), win=win, team=team)


async def test_the_command_reads_only_what_is_stored_and_asks_riot_for_nothing(stack, world):
    alpha, beta = await link_both(stack)
    add_shared(world, "S1", 3)
    add_shared(world, "S2", 2, win=False)
    await stack.riot.sync_history(alpha)
    await stack.riot.sync_history(beta)
    world.calls.clear()
    report = await stack.riot.duo(1, 2)
    assert world.calls == []
    assert (report.a.nickname, report.b.nickname) == ("Ana", "Rui")
    assert report.stats.tally == Tally(1, 1)
    assert [g.match_id for g in report.stats.recent] == ["S2", "S1"]


async def test_it_works_without_a_riot_key(db, world):
    s = Stack(db, world, enabled=False)
    ana, rui = await s.register(1, "Ana"), await s.register(2, "Rui")
    for player_id, puuid, name in ((ana, "puuid-alpha", "Alpha"), (rui, "puuid-beta", "Beta")):
        account = await s.riot_repo.add_account(player_id, puuid, name, "EUW", "euw1", True, 1, NOW)
        await s.riot_repo.mark_synced(account.id, NOW)
        await s.riot_repo.add_game(
            account.id,
            RankedGame(
                "S1", days_ago(NOW, 1), "BOTTOM", "BOTTOM", True, 4, 1, 6, 1800, False, "Jinx"
            ),
        )
    assert not s.riot.enabled
    assert (await s.riot.duo(1, 2)).stats.games == 1


async def test_refusals(stack, world):
    with pytest.raises(DuoError, match=SAME_PLAYER) as same:
        await stack.riot.duo(1, 1)
    assert same.value.discord_id is None
    with pytest.raises(DuoError) as unknown:
        await stack.riot.duo(1, 404)
    assert unknown.value.discord_id == 404
    with pytest.raises(DuoError, match="Ana não tem contas Riot ligadas"):
        await stack.riot.duo(1, 2)
    await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    with pytest.raises(DuoError, match="Rui não tem contas Riot ligadas"):
        await stack.riot.duo(1, 2)


async def test_no_shared_game_is_an_empty_answer_not_an_error(stack, world):
    alpha, _ = await link_both(stack)
    world.add_game("puuid-alpha", "only-alpha", "BOTTOM", days_ago(NOW, 1))
    await stack.riot.sync_history(alpha)
    report = await stack.riot.duo(1, 2)
    assert report.stats.games == 0


async def test_an_unfinished_first_sync_is_reported_and_has_no_coverage(stack, world):
    await link_both(stack)
    add_shared(world, "S1", 1)
    await stack.rows("UPDATE riot_accounts SET history_synced_at = NULL WHERE puuid = 'puuid-beta'")
    await stack.db.conn.commit()
    stats = (await stack.riot.duo(1, 2)).stats
    assert stats.syncing == ("Beta#EUW",) and stats.coverage is None
    assert stats.compare_a is None


async def test_coverage_is_when_every_account_is_complete(stack, world):
    alpha, beta = await link_both(stack)
    stack.riot.history = replace(stack.riot.history, max_games_per_account=2)
    for i in range(3):
        world.add_game("puuid-alpha", f"A{i}", "BOTTOM", days_ago(NOW, i + 1))
    add_shared(world, "S1", 0.5)
    await stack.riot.sync_history(alpha)
    await stack.riot.sync_history(beta)
    stored = await stack.rows(
        "SELECT played_at FROM riot_role_games WHERE riot_account_id = ? ORDER BY played_at DESC",
        alpha.id,
    )
    expected = from_db(stored[1][0])  # alpha's 2nd newest: older games may have been dropped
    stats = (await stack.riot.duo(1, 2)).stats
    assert stats.coverage == expected  # beta has fewer than 2 games: complete since the window
    assert stats.coverage > NOW - timedelta(days=stack.riot.history.window_days)


async def test_an_account_of_the_same_player_in_the_match_is_skipped_and_reported(stack, world):
    alpha, beta = await link_both(stack)
    smurf = (await stack.riot.link(1, "Smurf#EUW", None, ADMIN)).account
    await stack.riot.wait_for_background()
    add_shared(world, "S1", 3)
    add_shared(world, "S2", 2)
    world.add_game("puuid-smurf", "S2", "MIDDLE", days_ago(NOW, 2), team=100)  # Ana twice
    for account in (alpha, beta, smurf):
        await stack.riot.sync_history(account)
    stats = (await stack.riot.duo(1, 2)).stats
    assert stats.ambiguous == 1
    assert [g.match_id for g in stats.recent] == ["S1"]


async def test_unlinking_an_account_removes_its_games_from_the_stats(stack, world):
    alpha, beta = await link_both(stack)
    smurf = (await stack.riot.link(1, "Smurf#EUW", None, ADMIN)).account
    await stack.riot.wait_for_background()
    add_shared(world, "S1", 3)
    world.add_game("puuid-smurf", "S2", "BOTTOM", days_ago(NOW, 2), team=100)
    world.add_game("puuid-beta", "S2", "UTILITY", days_ago(NOW, 2), team=100)
    for account in (alpha, beta, smurf):
        await stack.riot.sync_history(account)
    assert (await stack.riot.duo(1, 2)).stats.games == 2
    await stack.riot.unlink(1, "Smurf#EUW")
    assert (await stack.riot.duo(1, 2)).stats.games == 1
    await stack.riot.unlink(1, "Alpha#EUW")
    with pytest.raises(DuoError, match="não tem contas Riot ligadas"):
        await stack.riot.duo(1, 2)
