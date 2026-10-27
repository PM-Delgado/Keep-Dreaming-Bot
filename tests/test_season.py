"""A season through the real services: lobby -> teams -> result -> ratings -> ladder, in memory.

These are the automatic version of "open a lobby with mock players and click through it": no
Discord, but every service call the buttons make. Skip the longer ones with `-m "not slow"`.
"""

import random

import pytest

from bot.features.simulation.__main__ import main as cli
from bot.features.simulation.players import random_roster
from bot.features.simulation.season import Season, format_season, rank_of


@pytest.mark.parametrize(
    ("rank_mu", "expected"),
    [
        (20.0, ("IRON", "IV")),
        (24.8, ("GOLD", "IV")),
        (28.0, ("EMERALD", "IV")),
        (30.8, ("DIAMOND", "I")),
        (5.0, ("IRON", "IV")),  # below the scale: clamped
        (31.2, ("MASTER", None)),
        (40.0, ("MASTER", None)),  # Grandmaster/Challenger are not simulated
    ],
)
def test_starting_ranks_follow_the_rank_scale(rank_mu, expected):
    assert rank_of(rank_mu) == expected


async def season_of(players: int, seed: int, games: int, rank_noise: float = 2.5):
    roster = random_roster(random.Random(seed), players)
    season = await Season.create(roster, seed, rank_noise)
    try:
        return await season.run(games)
    finally:
        await season.close()


async def test_a_few_games_run_through_every_real_step_and_keep_the_invariants():
    report = await season_of(11, 1, 4)
    assert len(report.games) == 4
    assert report.problems == []
    assert sum(line.games for line in report.lines) == 40  # ten players per game
    assert all(g.winner in ("blue", "red") for g in report.games)
    assert all(0 < g.predicted_blue < 1 and 0 < g.true_blue < 1 for g in report.games)


async def test_nobody_has_a_visible_tier_before_the_fifth_game():
    report = await season_of(10, 3, 4)  # ten players: all play every game, four games
    assert all(line.games == 4 for line in report.lines)
    assert set(report.tiers) == {"(colocação)"}
    assert all(line.standing == "Colocação 4/5" for line in report.lines)


async def test_the_fifth_game_places_everyone_and_tiers_appear():
    report = await season_of(10, 3, 5)
    assert report.problems == []
    assert "(colocação)" not in report.tiers
    assert sum(report.tiers.values()) == 10
    assert all("Colocação" not in line.standing for line in report.lines)


async def test_winners_gain_and_losers_lose_rating_game_by_game():
    roster = random_roster(random.Random(4), 10)
    season = await Season.create(roster, 4)
    try:
        for _ in range(3):
            before = {r.player_id: r.mu for r in await season.ratings.repo.list_stored()}
            game = await season.play_game()
            after = {r.player_id: r.mu for r in await season.ratings.repo.list_stored()}
            people = await season.match_repo.participants(game.match_id)
            for p in people:
                moved = after[p.player_id] - before[p.player_id]
                assert (moved > 0) == (p.side == game.winner), (game.index, p.player_id)
    finally:
        await season.close()


async def test_a_season_is_reproducible_from_its_seed():
    a, b = await season_of(12, 5, 3), await season_of(12, 5, 3)
    assert [g.winner for g in a.games] == [g.winner for g in b.games]
    assert [g.blue for g in a.games] == [g.blue for g in b.games]
    assert [line.h_mu for line in a.lines] == [line.h_mu for line in b.lines]


def test_the_report_text_has_the_headlines():
    import asyncio

    report = asyncio.run(season_of(10, 2, 2))
    text = format_season(report)
    assert "2 games played" in text
    assert "rank correlation" in text
    assert "invariants: all hold" in text


@pytest.mark.slow
async def test_a_longer_season_places_everyone_and_learns_who_is_better():
    report = await season_of(14, 2, 40)
    assert report.problems == []
    assert "(colocação)" not in report.tiers  # 40 games x 10 of 14 players: everyone has 5+
    ordered = sorted(report.lines, key=lambda line: line.true_mu)
    weakest, strongest = ordered[:4], ordered[-4:]
    assert sum(line.h_mu for line in strongest) > sum(line.h_mu for line in weakest)
    assert report.end_correlation > 0.5


@pytest.mark.slow
async def test_balance_stays_reasonable_while_ratings_move():
    # With starting ranks close to the truth. With sloppy ranks (--rank-noise 2.5) 20-55% of
    # games are clearly lopsided on the hidden skills and 30 games do not fix it: a tuning
    # question for the ratings, not something the balancer can repair.
    report = await season_of(14, 6, 30, rank_noise=0.5)
    assert report.mean_true_edge < 0.15
    assert report.lopsided_share < 0.4


@pytest.mark.slow
def test_the_season_command_prints_a_report_and_succeeds(capsys):
    assert cli(["season", "--players", "12", "--games", "6", "--seed", "1"]) == 0
    out = capsys.readouterr().out
    assert "invariants: all hold" in out
    assert "visible rank" in out
