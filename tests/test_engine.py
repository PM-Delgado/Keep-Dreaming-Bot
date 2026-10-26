import pytest

from bot.features.ratings.engine import Rating, RatingEngine
from bot.features.ratings.params import EngineParams, algo_version

ENGINE = RatingEngine()


def team(mu, sigma=25 / 3, n=5):
    return [Rating(mu, sigma) for _ in range(n)]


def test_win_raises_mu_and_loss_lowers_it_and_uncertainty_shrinks():
    winners, losers = ENGINE.update(team(25), team(25))
    assert all(w.mu > 25 for w in winners)
    assert all(loser.mu < 25 for loser in losers)
    assert all(r.sigma < 25 / 3 for r in (*winners, *losers))


def test_even_match_is_symmetric():
    winners, losers = ENGINE.update(team(25), team(25))
    assert winners[0].mu - 25 == pytest.approx(25 - losers[0].mu)


def test_upset_moves_ratings_more_than_expected_result():
    expected_w, _ = ENGINE.update(team(30, 4), team(20, 4))
    upset_w, _ = ENGINE.update(team(20, 4), team(30, 4))
    assert upset_w[0].mu - 20 > expected_w[0].mu - 30


def test_uncertain_players_move_more_than_established_ones():
    new_w, _ = ENGINE.update(team(25, 8), team(25, 8))
    old_w, _ = ENGINE.update(team(25, 2), team(25, 2))
    assert new_w[0].mu - 25 > old_w[0].mu - 25


def test_sigma_never_exceeds_its_starting_value_in_normal_play():
    a, b = team(25), team(25)
    for i in range(30):
        if i % 2 == 0:
            a, b = ENGINE.update(a, b)
        else:
            b, a = ENGINE.update(b, a)
        assert all(r.sigma <= 25 / 3 for r in (*a, *b))


def test_update_is_deterministic_and_does_not_mutate_inputs():
    a, b = team(26, 6), team(24, 6)
    first = ENGINE.update(a, b)
    second = ENGINE.update(a, b)
    assert first == second
    assert a == team(26, 6)


def test_win_probability_favours_the_stronger_team():
    assert ENGINE.win_probability(team(25), team(25)) == pytest.approx(0.5)
    assert ENGINE.win_probability(team(30), team(25)) > 0.5
    assert ENGINE.win_probability(team(25), team(30)) < 0.5


def test_algo_version_changes_with_engine_params():
    assert algo_version() != algo_version(EngineParams(beta=5.0))
    assert algo_version().startswith("openskill-")
