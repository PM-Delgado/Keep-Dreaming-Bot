"""The tuning lab: realistic populations, setting overrides, and what a small experiment reports."""

import random

import pytest

from bot.features.simulation.population import (
    RANK_WEIGHTS,
    describe_population,
    rank_mu_of,
    realistic_population,
)
from bot.features.simulation.season import Season, SeasonOptions
from bot.features.simulation.tuning import (
    Experiment,
    World,
    apply_overrides,
    format_summaries,
    known_settings,
    run_experiments,
    run_seed,
)


def test_a_group_of_twenty_spans_master_to_bronze_in_the_agreed_proportions():
    players = realistic_population(random.Random(1), 20)
    tiers = [p.rank[0] for p in players if p.rank]
    assert len(players) == 20
    assert tiers.count("MASTER") == 1
    assert tiers.count("BRONZE") == 1
    assert tiers.count("SILVER") == 4
    assert tiers.count("GOLD") == 5
    assert set(tiers) <= set(RANK_WEIGHTS)


def test_the_true_skill_deviates_from_the_rank_by_the_requested_amount():
    tight = realistic_population(random.Random(2), 200, deviation_sd=0.0, outlier_share=0.0)
    assert all(p.true_mu == pytest.approx(rank_mu_of(*p.rank)) for p in tight if p.rank)
    loose = realistic_population(random.Random(2), 200, deviation_sd=2.0, outlier_share=0.3)
    gaps = [abs(p.true_mu - rank_mu_of(*p.rank)) for p in loose if p.rank]
    assert max(gaps) > 3.0  # outliers exist
    assert 0.5 < sum(gaps) / len(gaps) < 3.5


def test_populations_are_reproducible_and_players_have_roles():
    a = realistic_population(random.Random(7), 12)
    b = realistic_population(random.Random(7), 12)
    assert [(p.rank, p.true_mu, dict(p.proficiency)) for p in a] == [
        (p.rank, p.true_mu, dict(p.proficiency)) for p in b
    ]
    assert all(5 in p.proficiency.values() for p in a)  # everyone has a main role
    assert "true" in describe_population(a)


def test_overrides_reach_the_right_part_of_the_bot():
    options = apply_overrides(
        SeasonOptions(),
        {"sigma_rank_seed": 3.0, "beta": 8.0, "placement_games": 3, "w_lane": 0.01, "oracle": 1},
    )
    assert options.params.sigma_rank_seed == 3.0
    assert options.engine.beta == 8.0
    assert options.ladder.placement_games == 3
    assert isinstance(apply_overrides(SeasonOptions(), {"form_games": 10.0}).params.form_games, int)
    assert options.ladder.version == SeasonOptions().ladder.version + 1  # a changed ladder
    assert options.balance.w_lane == 0.01
    assert options.oracle is True
    assert options.params.d_up == SeasonOptions().params.d_up  # the rest is untouched


def test_an_unknown_setting_is_an_error_not_silently_ignored():
    with pytest.raises(ValueError, match="Unknown setting 'sigma_rank_sed'"):
        apply_overrides(SeasonOptions(), {"sigma_rank_sed": 3.0})


def test_every_listed_setting_can_be_overridden():
    for group, names in known_settings().items():
        if group == "experiment":
            continue
        for name in names:
            current = {
                "placement_games": 5,
                "demotion_buffer": 0.33,
                "division_width": 1.0,
            }.get(name, 1.0)
            apply_overrides(SeasonOptions(), {name: current})


async def test_the_oracle_starts_with_ratings_equal_to_the_truth():
    people = realistic_population(random.Random(3), 10)
    season = await Season.create(people, 3, options=SeasonOptions(oracle=True))
    try:
        for player_id, sim in season.sim_of.items():
            estimate = (await season.ratings.inspect(player_id)).estimate.mu
            assert estimate == pytest.approx(sim.true_mu, abs=0.01)
    finally:
        await season.close()


async def test_form_is_hidden_and_changes_over_time():
    people = realistic_population(random.Random(4), 10)
    season = await Season.create(people, 4, options=SeasonOptions(form_sd=2.0))
    try:
        before = dict(season.form)
        assert any(v != 0 for v in before.values())
        await season.play_game()
        assert season.form != before
    finally:
        await season.close()


def test_a_small_experiment_reports_every_column_and_holds_the_invariants():
    experiment = Experiment("tiny", {"sigma_rank_seed": 3.0}, World(size=10), games=6, seeds=(1,))
    [summary] = run_experiments([experiment], workers=1)
    [result] = summary.results
    assert summary.problems == 0
    assert len(result.rmse) == 5  # at 0, 10, 20, 40 games and at the end
    assert 0 <= result.lopsided <= 1
    assert result.placed == 1.0  # ten players, six games: everyone passed placement
    text = format_summaries([summary])
    assert "tiny" in text
    assert "E-rmse" in text


def test_the_same_seed_gives_the_same_season():
    experiment = Experiment("repeat", {}, World(size=10), games=4, seeds=(2,))
    assert run_seed(experiment, 2) == run_seed(experiment, 2)


@pytest.mark.slow
def test_the_oracle_floor_shows_the_balancer_is_fine_when_ratings_are_right():
    oracle = Experiment("oracle", {"oracle": 1}, World(), games=30, seeds=(1, 2))
    current = Experiment("current", {}, World(), games=30, seeds=(1, 2))
    best, ordinary = run_experiments([oracle, current], workers=1)
    assert best.mean("lopsided") < 0.1
    assert best.mean("p_error") < 0.05
    assert ordinary.mean("p_error") > best.mean("p_error")
