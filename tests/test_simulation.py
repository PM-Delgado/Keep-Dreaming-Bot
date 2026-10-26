"""Matchmaking simulations: synthetic players, named scenarios, population regressions, a league.

Tests marked `slow` run many lobbies (each balance takes ~0.3 s); skip them in a quick loop with
`pytest -m "not slow"`.

Run `python -m bot.features.simulation ...` to *look* at the teams; these tests lock in what we
agreed they should look like, so a tweak to the balancer (or later to ratings and ranks) that
makes lobbies worse fails here instead of on Discord.
"""

import random
from dataclasses import replace

import pytest

from bot.features.balance.engine import balance
from bot.features.balance.model import BalanceParams
from bot.features.players.proficiency import ROLES
from bot.features.simulation.__main__ import main as cli
from bot.features.simulation.league import League, spearman
from bot.features.simulation.metrics import (
    aggregate,
    evaluate,
    true_blue_win_prob,
    true_role_strength,
)
from bot.features.simulation.players import (
    STYLES,
    make_player,
    random_player,
    random_roster,
)
from bot.features.simulation.runner import (
    format_aggregate,
    format_outcome,
    run_lobby,
    run_population,
    run_scenario,
)
from bot.features.simulation.scenarios import build_scenarios

SCENARIOS = build_scenarios()
KEY_SCENARIOS = ["five_junglers", "two_masters_mid", "three_masters", "one_tricks", "role_scarcity"]


# --- synthetic players ----


def test_make_player_builds_a_profile_and_keeps_the_hidden_truth_separate():
    p = make_player(1, 27.0, main="MID", second="TOP", third="ADC", rest=2, true_mu=29.0)
    assert [p.proficiency[r] for r in ROLES] == [4, 2, 5, 3, 2]
    assert (p.mu, p.true_mu) == (27.0, 29.0)
    assert p.best_level == 5
    assert p.best_roles == ["MID"]
    assert make_player(2, 25).true_mu == 25  # truth defaults to the belief
    unknown = make_player(3, 25, rest=None)
    assert unknown.best_level is None
    assert unknown.to_input().proficiency == {r: None for r in ROLES}


def test_random_players_are_reproducible_and_within_range():
    for style in STYLES:
        a = random_roster(random.Random(5), 10, style)
        b = random_roster(random.Random(5), 10, style)
        assert a == b
        for p in a:
            assert 15 <= p.mu <= 35
            assert 3 <= p.sigma <= 7
            assert p.best_level == 5  # everybody has a main role
            assert all(v in (None, 1, 2, 3, 4, 5) for v in p.proficiency.values())
    with pytest.raises(ValueError):
        random_player(random.Random(1), 1, "nonsense")


def test_styles_do_what_they_say():
    rng = random.Random(9)
    stack = [random_player(rng, i, "role_stack") for i in range(200)]
    junglers = sum(p.proficiency["JUNGLE"] == 5 for p in stack)
    assert junglers > 80  # about half main jungle
    wide = [random_player(rng, i, "wide").mu for i in range(200)]
    assert max(wide) - min(wide) > 15
    unknown = random_roster(rng, 100, "unknown_heavy")
    normal = random_roster(rng, 100, "realistic")
    count = lambda ps: sum(v is None for p in ps for v in p.proficiency.values())  # noqa: E731
    assert count(unknown) > 2 * count(normal)


def test_estimate_noise_separates_belief_from_truth():
    exact = random_roster(random.Random(2), 10)
    assert all(p.mu == p.true_mu for p in exact)
    noisy = random_roster(random.Random(2), 10, estimate_noise=1.5)
    assert any(abs(p.mu - p.true_mu) > 0.5 for p in noisy)


# --- metrics ----


def test_true_strength_uses_its_own_formula_not_the_balancers():
    p = make_player(1, 30, main="MID", third="TOP", rest=1)
    assert true_role_strength(p, "MID") == 30
    assert true_role_strength(p, "TOP") == 29  # level 3: two levels below 5
    assert true_role_strength(p, "ADC") == 28  # level 1: four levels below 5
    assert true_role_strength(make_player(2, 30, rest=None), "MID") == 28.75  # unknown ~ 2.5


def test_metrics_for_a_known_proposal():
    roles = ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"]
    players = [make_player(i, 25, main=roles[(i - 1) % 5], rest=1) for i in range(1, 11)]
    proposal = balance([p.to_input() for p in players], rng=random.Random(1)).proposals[0]
    m = evaluate(proposal, players)
    assert m.off_best == 0
    assert m.bad_fit == 0
    assert m.avg_fit == 5
    assert m.true_edge == pytest.approx(0.0, abs=0.02)
    assert m.lane_gap == pytest.approx(0.0, abs=1e-9)
    assert 0 <= m.true_p_blue <= 1
    assert true_blue_win_prob(proposal, {p.player_id: p for p in players}) == m.true_p_blue


def test_aggregate_summarises_many_proposals_and_rejects_nothing():
    outcomes = [run_lobby(random_roster(random.Random(s)), seed=s) for s in (1, 2)]
    agg = aggregate([o.metrics for o in outcomes])
    assert agg.count == 2
    assert 0 <= agg.share_lopsided <= 1
    assert agg.max_true_edge >= agg.mean_true_edge
    assert "2 lobbies" in format_aggregate(agg)
    with pytest.raises(ValueError):
        aggregate([])


# --- named scenarios ----


def test_the_catalogue_contains_the_agreed_scenarios():
    assert set(SCENARIOS) >= {
        "balanced_mains",
        "five_junglers",
        "four_junglers",
        "all_mid",
        "two_masters_mid",
        "master_among_golds",
        "three_masters",
        "masters_slumping",
        "unknown_profiles",
        "one_tricks",
        "role_scarcity",
        "wide_skill_spread",
        "newcomers_with_high_uncertainty",
        "star_role_collision",
    }
    for scenario in SCENARIOS.values():
        assert len(scenario.players) == 10
        assert scenario.description


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_each_scenario_lands_as_expected(name):
    outcome, problems = run_scenario(SCENARIOS[name], seed=1)
    assert not problems, f"{name}: {problems}\n{format_outcome(outcome)}"


@pytest.mark.parametrize("seed", [2, 3])
@pytest.mark.parametrize("name", KEY_SCENARIOS)
def test_key_scenarios_hold_for_other_tie_breaks_too(name, seed):
    outcome, problems = run_scenario(SCENARIOS[name], seed=seed)
    assert not problems, f"{name} (seed {seed}): {problems}\n{format_outcome(outcome)}"


def test_scenario_outcomes_are_reproducible():
    first, _ = run_scenario(SCENARIOS["five_junglers"], seed=4)
    again, _ = run_scenario(SCENARIOS["five_junglers"], seed=4)
    assert first.proposal == again.proposal


def test_a_failing_expectation_is_reported_with_the_teams():
    scenario = SCENARIOS["two_masters_mid"]
    lazy = replace(
        BalanceParams(), w_lane=0.0, w_role=0.0, w_topk=0.0, w_spread=0.0, elite_penalty=0.0
    )
    outcome, problems = run_scenario(scenario, lazy, seed=1)
    text = format_outcome(outcome)
    assert "AZUL" in text
    assert "VERMELHO" in text
    assert "predicted blue win" in text
    assert isinstance(problems, list)


# --- population regressions (balancer quality with exact ratings) ----


@pytest.mark.slow
def test_realistic_lobbies_are_balanced_and_well_placed():
    agg, _ = run_population(5, "realistic", seed=3)
    assert agg.mean_true_edge <= 0.04
    assert agg.max_true_edge <= 0.08
    assert agg.share_lopsided == 0
    assert agg.mean_avg_fit >= 4.2
    assert agg.share_bad_fit_players <= 0.15
    assert agg.share_elite_split >= 0.9


@pytest.mark.slow
def test_lobbies_with_a_popular_role_still_work():
    agg, outcomes = run_population(6, "role_stack", seed=3)
    assert agg.mean_true_edge <= 0.04
    assert agg.mean_avg_fit >= 3.7
    assert agg.share_bad_fit_players <= 0.28
    for outcome in outcomes:  # one of each role per side, always
        for side in (outcome.proposal.blue, outcome.proposal.red):
            assert [s.role for s in side] == list(ROLES)


@pytest.mark.slow
def test_wide_rating_gaps_are_still_balanced():
    agg, _ = run_population(5, "wide", seed=3)
    assert agg.mean_true_edge <= 0.04


@pytest.mark.slow
def test_missing_role_data_does_not_break_balance():
    agg, _ = run_population(5, "unknown_heavy", seed=3)
    assert agg.mean_true_edge <= 0.04
    assert agg.max_true_edge <= 0.08


@pytest.mark.slow
def test_the_role_terms_earn_their_keep():
    """Ablation: switching role fit and lane matching off must make lobbies clearly worse."""
    on, _ = run_population(6, "role_stack", seed=3)
    off_params = replace(BalanceParams(), w_role=0.0, w_lane=0.0)
    off, _ = run_population(6, "role_stack", seed=3, params=off_params)
    assert on.mean_avg_fit > off.mean_avg_fit + 0.3
    assert on.share_bad_fit_players < off.share_bad_fit_players
    assert on.mean_lane_gap < 0.7 * off.mean_lane_gap


@pytest.mark.slow
def test_noisy_ratings_cost_fairness_no_matter_what_the_balancer_does():
    exact, _ = run_population(6, "realistic", seed=3)
    noisy, _ = run_population(6, "realistic", seed=3, estimate_noise=1.5)
    # The balancer sees the same (believed) ratings, so its own view is just as good; reality
    # is worse because the ratings themselves are wrong. That is a rating-quality problem.
    assert noisy.mean_true_edge > 3 * exact.mean_true_edge
    assert noisy.mean_true_edge > 0.06


# --- league: matches, results and ratings ----


def test_spearman_rank_correlation():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3], [5, 5, 5]) == 0.0  # no spread, no correlation
    assert spearman([1, 2, 2, 4], [1, 2, 3, 4]) > 0.9  # ties share their average rank


def test_a_league_needs_ten_players():
    with pytest.raises(ValueError):
        League(random_roster(random.Random(1), 9))


@pytest.mark.slow
def test_league_matches_move_ratings_the_right_way():
    league = League(random_roster(random.Random(2), 12), seed=2)
    report = league.run(3)
    assert len(report.records) == 3
    for record in report.records:
        winners = record.blue_ids if record.winner == "blue" else record.red_ids
        losers = record.red_ids if record.winner == "blue" else record.blue_ids
        assert all(record.mu_change[i] > 0 for i in winners)  # a win always raises the rating
        assert all(record.mu_change[i] < 0 for i in losers)  # a loss always lowers it
        assert set(record.mu_change) == set(winners) | set(losers)
    games = sum(p.games for p in league.players.values())
    assert games == 30
    assert sum(p.wins for p in league.players.values()) == 15  # one winning team per match
    assert all(len(p.mu_history) == p.games for p in league.players.values())


@pytest.mark.slow
def test_league_is_deterministic_and_reports_tiers_and_correlation():
    def run():
        return League(random_roster(random.Random(5), 11), seed=5).run(3)

    a, b = run(), run()
    assert [r.winner for r in a.records] == [r.winner for r in b.records]
    assert a.end_correlation == b.end_correlation
    assert sum(a.tiers.values()) == 11
    assert -1 <= a.start_correlation <= 1
    assert "(colocação)" in a.tiers  # nobody has played the 5 placement games yet


@pytest.mark.slow
def test_league_matchmaking_stays_balanced_while_ratings_move():
    league = League(random_roster(random.Random(7), 14), seed=7)
    report = league.run(4)
    for record in report.records:
        assert abs(record.metrics.p_blue - 0.5) < 0.1  # the balancer's own view stays even
        assert record.metrics.elite_split


def test_the_matchmaking_estimate_follows_the_rank_anchor_in_the_league():
    league = League(random_roster(random.Random(3), 10), seed=3)
    lp = next(iter(league.players.values()))
    start = league.estimate(lp)
    assert start.mu == pytest.approx(lp.rank_mu)  # zero games: E equals the rank estimate R
    lp.h = replace(lp.h, mu=lp.rank_mu - 12)  # a catastrophic slump
    slumped = league.estimate(lp)
    assert slumped.mu >= lp.rank_mu - league.params.d_down  # E cannot fall further than d_down


# --- the command-line report ----


def test_cli_lists_and_runs_scenarios(capsys):
    assert cli(["list"]) == 0
    assert "five_junglers" in capsys.readouterr().out
    assert cli(["scenario", "five_junglers", "--seed", "1"]) == 0
    out = capsys.readouterr().out
    assert "AZUL" in out
    assert "as expected" in out
    assert cli(["scenario", "nope"]) == 2


@pytest.mark.slow
def test_cli_population_and_league(capsys):
    assert cli(["population", "--style", "realistic", "--lobbies", "2", "--seed", "1"]) == 0
    assert "2 lobbies" in capsys.readouterr().out
    assert cli(["league", "--players", "10", "--lobbies", "2", "--seed", "1"]) == 0
    out = capsys.readouterr().out
    assert "rank correlation" in out
    assert "(colocação)" in out
