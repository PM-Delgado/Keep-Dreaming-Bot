"""Recent form: the last few results versus what the bot predicted, nudging only the matchmaking
estimate E (never the in-house rating H or the visible tier)."""

from dataclasses import replace

import pytest
import pytest_asyncio
from lobby_fakes import ADMIN, LobbyStack, sides_of, start_game

from bot.db import Database
from bot.features.ratings.matchmaking import FormGame, form_adjustment
from bot.features.ratings.params import MatchmakingParams

ON = MatchmakingParams(form_weight=4.0, form_games=5, form_cap=2.0)


def games(*results: tuple[bool, float]) -> list[FormGame]:
    return [FormGame(won, expected) for won, expected in results]


def test_form_is_off_by_default():
    assert MatchmakingParams().form_weight == 0
    assert form_adjustment(games((True, 0.5)) * 5, MatchmakingParams()) == 0.0


def test_no_games_no_adjustment():
    assert form_adjustment([], ON) == 0.0


def test_a_win_streak_lifts_the_estimate_and_a_losing_streak_lowers_it():
    up = form_adjustment(games(*[(True, 0.5)] * 5), ON)
    down = form_adjustment(games(*[(False, 0.5)] * 5), ON)
    assert up == pytest.approx(2.0)  # 4.0 * 0.5, right at the cap
    assert down == pytest.approx(-2.0)


def test_beating_a_weak_prediction_counts_more_than_a_win_that_was_expected():
    surprise = form_adjustment(games(*[(True, 0.3)] * 5), ON)
    expected = form_adjustment(games(*[(True, 0.7)] * 5), ON)
    assert surprise > expected > 0


def test_the_adjustment_is_capped():
    big = replace(ON, form_weight=100.0)
    assert form_adjustment(games(*[(True, 0.5)] * 5), big) == pytest.approx(2.0)
    assert form_adjustment(games(*[(False, 0.5)] * 5), big) == pytest.approx(-2.0)


def test_only_the_most_recent_games_count_and_a_short_history_counts_less():
    old_then_new = games(*[(False, 0.5)] * 10, *[(True, 0.5)] * 5)
    assert form_adjustment(old_then_new, ON) == pytest.approx(2.0)  # the losses are forgotten
    two = form_adjustment(games((True, 0.5), (True, 0.5)), replace(ON, form_cap=10.0))
    five = form_adjustment(games(*[(True, 0.5)] * 5), replace(ON, form_cap=10.0))
    assert two == pytest.approx(five * 2 / 5)


def test_even_results_cancel_out():
    mixed = games((True, 0.5), (False, 0.5), (True, 0.5), (False, 0.5))
    assert form_adjustment(mixed, ON) == pytest.approx(0.0)


@pytest_asyncio.fixture
async def stack(db: Database) -> LobbyStack:
    return LobbyStack(db)


async def play(stack: LobbyStack, winner: str) -> tuple[int, dict[str, list[int]]]:
    lobby_id, match = await start_game(stack)
    await stack.register(1, start=ADMIN)
    await stack.lobbies.report_result(lobby_id, ADMIN, winner)
    return match.id, await sides_of(stack, match.id)


async def test_recent_results_are_oldest_first_with_the_prediction_for_the_players_side(stack):
    first, sides = await play(stack, "blue")
    second, _ = await play(stack, "red")
    player = sides["blue"][0]
    results = await stack.match_repo.recent_results(player, 5)
    assert [r.won for r in results] == [True, results[1].won]
    assert len(results) == 2
    match = await stack.match_repo.get(first)
    assert match is not None
    assert results[0].expected == pytest.approx(match.expected_blue_win)  # a blue-side player


async def test_only_confirmed_games_count_and_the_limit_is_respected(stack):
    _, sides = await play(stack, "blue")
    lobby_id, _ = await start_game(stack)  # started but never reported
    assert len(await stack.match_repo.recent_results(sides["blue"][0], 5)) == 1
    await stack.lobbies.cancel(lobby_id, 1)
    for _ in range(3):
        await play(stack, "blue")
    assert len(await stack.match_repo.recent_results(sides["blue"][0], 2)) == 2


async def test_form_moves_e_but_not_the_in_house_rating(stack):
    _, sides = await play(stack, "blue")
    winner, loser = sides["blue"][0], sides["red"][0]
    plain = await stack.ratings.inspect(winner)
    stack.ratings.params = replace(stack.ratings.params, form_weight=4.0)
    with_form = await stack.ratings.inspect(winner)
    assert with_form.estimate.form > 0
    assert with_form.estimate.mu == pytest.approx(plain.estimate.mu + with_form.estimate.form)
    assert with_form.stored.mu == plain.stored.mu  # H untouched
    assert with_form.standing == plain.standing  # and so is the visible rank
    assert (await stack.ratings.inspect(loser)).estimate.form < 0


async def test_with_form_off_nothing_changes(stack):
    _, sides = await play(stack, "blue")
    inspection = await stack.ratings.inspect(sides["blue"][0])
    assert inspection.estimate.form == 0.0
