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


# --- role-aware update ----


def test_no_losses_or_all_zero_losses_is_exactly_the_plain_update():
    plain = ENGINE.update(team(26, 2.0), team(24, 2.0))
    assert ENGINE.update(team(26, 2.0), team(24, 2.0), None, None) == plain
    zeros = [0.0] * 5
    assert ENGINE.update(team(26, 2.0), team(24, 2.0), zeros, zeros) == plain


def test_a_loss_on_the_losing_side_is_expected_so_it_costs_less():
    # The same two teams; the losers' strongest player lost 3 points on the role he played.
    plain_w, plain_l = ENGINE.update(team(26, 2.0), team(26, 2.0))
    aware_w, aware_l = ENGINE.update(team(26, 2.0), team(26, 2.0), None, [3.0, 0, 0, 0, 0])
    # With him weaker the loss was less of a surprise: his rating drops less, winners gain less.
    assert aware_l[0].mu > plain_l[0].mu
    assert aware_w[0].mu < plain_w[0].mu
    # The change is applied to his real rating: it stays near 26, not near 23.
    assert 25 < aware_l[0].mu < 26


def test_autofilled_winners_gain_more():
    plain_w, _ = ENGINE.update(team(26, 2.0), team(26, 2.0))
    aware_w, _ = ENGINE.update(team(26, 2.0), team(26, 2.0), [3.0, 3.0, 0, 0, 0], None)
    assert aware_w[0].mu > plain_w[0].mu  # won although weaker on the role: more credit


def test_wrong_number_of_losses_is_refused():
    with pytest.raises(ValueError):
        ENGINE.update(team(25), team(25), [0.0], None)


def test_role_aware_changes_the_algo_version_only_when_on():
    off = algo_version(EngineParams())
    on = algo_version(EngineParams(role_aware=True))
    assert on != off and on.startswith(off)
    assert algo_version(EngineParams(role_aware=True)) == on  # stable
    assert (
        algo_version(EngineParams(role_aware=True, role_loss={5: 0, 4: 1, 3: 2, 2: 3, 1: 4})) != on
    )
    # A different table with the feature off does not change the version.
    assert algo_version(EngineParams(role_loss={5: 0, 4: 1, 3: 2, 2: 3, 1: 4})) == off


def test_loss_for_uses_the_table_and_the_unknown_value():
    params = EngineParams()
    assert params.loss_for(5) == 0.0
    assert params.loss_for(1) == 3.0
    assert params.loss_for(None) == params.unknown_role_loss == 1.0


def test_the_balancer_and_the_update_share_one_loss_table():
    from bot.features.balance.model import BalanceParams

    assert dict(BalanceParams().prof_loss) == dict(EngineParams().role_loss)
    assert BalanceParams().unknown_prof_loss == EngineParams().unknown_role_loss
