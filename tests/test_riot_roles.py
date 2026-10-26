from datetime import UTC, datetime, timedelta

import pytest

from bot.features.players.proficiency import effective_proficiency
from bot.features.riot.roles import (
    GameObservation,
    RoleHistoryParams,
    confidence_from_weights,
    estimate_proficiency,
    game_quality,
    position_role,
    recency_weight,
    role_experience,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)
P = RoleHistoryParams()


def unit_weight(age_days, half_life):
    return 1.0


def game(role="MIDDLE", individual=None, days=0.0, duration=1800, early=False):
    return GameObservation(NOW - timedelta(days=days), role, individual or role, duration, early)


def unknown_game(days=0.0):
    return GameObservation(NOW - timedelta(days=days), "", "", 1800)


# --- confidence formula with unit weights (independent of recency) ------------------------


def test_formula_all_known():
    assert confidence_from_weights(100, 100) == pytest.approx(100 / 110)


def test_formula_half_unknown_is_genuinely_lower():
    all_known = confidence_from_weights(100, 100)
    half_unknown = confidence_from_weights(100, 50)
    assert half_unknown == pytest.approx(50 / 110)
    assert half_unknown < all_known - 0.4  # the old n_known/(n_known+k) gave ~0.83


def test_formula_ambiguous_lowers_but_less_than_unknown():
    assert confidence_from_weights(100, 90) == pytest.approx(90 / 110)
    assert confidence_from_weights(100, 50) < confidence_from_weights(100, 90) < 100 / 110


def test_formula_edges():
    assert confidence_from_weights(100, 0) == 0.0  # all unknown
    assert confidence_from_weights(0, 0) == 0.0  # no games
    assert confidence_from_weights(50, 50) < confidence_from_weights(100, 100)  # more games help


def test_unit_weight_experience_matches_the_formula():
    games = [game() for _ in range(100)]
    exp = role_experience(games, NOW, P, weight=unit_weight)
    assert exp.n_all == exp.n_q == 100
    assert exp.confidence == pytest.approx(100 / 110)

    mixed = [game() for _ in range(50)] + [unknown_game() for _ in range(50)]
    exp = role_experience(mixed, NOW, P, weight=unit_weight)
    assert exp.confidence == pytest.approx(50 / 110)
    assert exp.coverage == pytest.approx(0.5)

    ambiguous = [game() for _ in range(80)] + [game("MIDDLE", "TOP") for _ in range(20)]
    exp = role_experience(ambiguous, NOW, P, weight=unit_weight)
    assert exp.confidence == pytest.approx(90 / 110)
    assert exp.by_role["MID"] == pytest.approx(90)


# --- recency weighting, tested on its own -------------------------------------------------


def test_recency_weights():
    assert recency_weight(0) == 1.0
    assert recency_weight(90) == pytest.approx(0.5)
    assert recency_weight(180) == pytest.approx(0.25)
    assert recency_weight(30) > recency_weight(60)
    assert recency_weight(-5) == 1.0  # clock skew never exceeds 1


def test_dated_sample_confidence_is_hand_computed_and_below_the_unit_weight_figure():
    # 10 games today and 10 games 90 days ago, all known: n = 10*1 + 10*0.5 = 15.
    games = [game(days=0) for _ in range(10)] + [game(days=90) for _ in range(10)]
    exp = role_experience(games, NOW, P)
    assert exp.n_all == pytest.approx(15.0)
    assert exp.confidence == pytest.approx(15 / 25)
    assert exp.confidence < confidence_from_weights(20, 20)  # unit weights would say 20/30


def test_recent_games_count_more_than_old_ones():
    recent = role_experience([game("TOP", days=1)] * 5 + [game("MIDDLE", days=300)] * 5, NOW, P)
    assert recent.by_role["TOP"] > recent.by_role["MID"] * 5


# --- eligibility and position handling -------------------------------------------------------


def test_remakes_early_surrenders_and_old_games_are_excluded():
    games = [
        game(duration=299),  # remake
        game(early=True),
        game(days=400),  # outside the 12-month window
        game(),
    ]
    assert role_experience(games, NOW, P, weight=unit_weight).n_all == 1


def test_position_mapping_and_quality():
    assert position_role("BOTTOM") == "ADC"
    assert position_role("utility") == "SUPPORT"
    assert position_role("Invalid") is None
    assert position_role(None) is None
    assert game_quality(game("MIDDLE")) == ("MID", 1.0)
    assert game_quality(game("MIDDLE", "TOP")) == ("MID", 0.5)
    assert game_quality(game("MIDDLE", "Invalid")) == ("MID", 1.0)  # individual absent/invalid
    assert game_quality(unknown_game()) == (None, 0.0)


# --- estimated proficiency ----------------------------------------------------------------


def estimate(games, weight=unit_weight):
    return estimate_proficiency(role_experience(games, NOW, P, weight=weight), P)


def test_thresholds_and_zero_experience_with_a_confident_profile():
    games = [game("MIDDLE")] * 25 + [game("TOP")] * 8 + [game("JUNGLE")] * 3 + [game("BOTTOM")]
    result = estimate(games)
    assert result == {"TOP": 3, "JUNGLE": 2, "MID": 4, "ADC": 1, "SUPPORT": 1}


def test_estimate_is_capped_at_four():
    assert max(v for v in estimate([game("MIDDLE")] * 500).values() if v) == 4
    assert effective_proficiency(None, None, 5).value == 4


def test_low_confidence_means_unknown_not_low():
    assert all(v is None for v in estimate([game("MIDDLE")] * 2).values())
    assert all(v is None for v in estimate([]).values())


def test_missing_position_data_pushes_the_profile_to_unknown():
    clear = [game("MIDDLE")] * 12
    assert estimate(clear)["MID"] == 3
    murky = [game("MIDDLE")] * 12 + [unknown_game() for _ in range(40)]
    assert all(v is None for v in estimate(murky).values())


def test_override_precedence_still_wins_over_the_estimate():
    assert effective_proficiency(2, None, 4).value == 2
    assert effective_proficiency(2, 5, 4).source == "admin"
