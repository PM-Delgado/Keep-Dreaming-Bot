import random
from datetime import UTC, datetime, timedelta

import pytest

from bot.features.ratings.engine import RatingEngine
from bot.features.ratings.ladder import DEFAULT_LADDER
from bot.features.ratings.params import EngineParams
from bot.features.ratings.replay import (
    MatchIn,
    RatingEventIn,
    ReplayError,
    apply_event,
    apply_match,
    replay,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
BLUE = (1, 2, 3, 4, 5)
RED = (6, 7, 8, 9, 10)


def seed_events(mu=25.0, sigma=25 / 3):
    return [RatingEventIn(pid, pid, "seed", mu, sigma, T0) for pid in range(1, 11)]


def match(i, winner, at=None):
    return MatchIn(i, at or T0 + timedelta(hours=i), winner, BLUE, RED)


def test_seeds_set_ratings_and_unplaced_standing():
    result = replay(seed_events(26.0, 6.5), [])
    assert result.players[1].mu == 26.0
    assert result.players[1].games == 0
    assert not result.players[1].standing.placed
    assert len(result.history) == 10
    assert result.history[0].mu_before is None


def test_replay_is_deterministic_and_input_order_independent():
    events = seed_events()
    matches = [match(i, "blue" if i % 3 else "red") for i in range(1, 9)]
    first = replay(events, matches)
    shuffled_events, shuffled_matches = events[:], matches[:]
    random.Random(3).shuffle(shuffled_events)
    random.Random(4).shuffle(shuffled_matches)
    assert replay(shuffled_events, shuffled_matches) == first
    assert replay(events, matches) == first


def test_incremental_application_equals_full_replay():
    events = seed_events()
    matches = [match(i, "blue" if i % 2 else "red") for i in range(1, 8)]
    full = replay(events, matches)

    state: dict = {}
    engine = RatingEngine()
    for event in events:
        apply_event(state, event, DEFAULT_LADDER)
    for m in matches:
        apply_match(state, m, engine, DEFAULT_LADDER)
    assert state == full.players


def test_voided_match_is_simply_not_an_input():
    events = seed_events()
    a, b, c = match(1, "blue"), match(2, "blue"), match(3, "red")
    with_b = replay(events, [a, b, c])
    without_b = replay(events, [a, c])
    assert with_b.players != without_b.players
    assert without_b.players[1].games == 2
    # Correcting a winner is the same as replaying the corrected history from scratch.
    corrected = replay(events, [a, MatchIn(2, b.confirmed_at, "red", BLUE, RED), c])
    assert corrected.players != with_b.players


def test_games_count_and_placement_after_enough_matches():
    result = replay(seed_events(), [match(i, "blue") for i in range(1, 6)])
    assert result.players[1].games == 5
    assert result.players[1].standing.placed
    assert result.players[6].standing.placed


def test_adjust_event_resets_mu_but_keeps_games():
    events = [*seed_events(), RatingEventIn(100, 1, "adjust", 31.0, 4.0, T0 + timedelta(hours=10))]
    result = replay(events, [match(i, "red") for i in range(1, 6)])
    assert result.players[1].mu == 31.0
    assert result.players[1].sigma == 4.0
    assert result.players[1].games == 5


def test_events_precede_matches_at_the_same_timestamp():
    events = [*seed_events(), RatingEventIn(100, 1, "adjust", 31.0, 4.0, T0 + timedelta(hours=1))]
    result = replay(events, [match(1, "blue", at=T0 + timedelta(hours=1))])
    # The adjustment was applied first, so the match started from mu=31.
    assert result.players[1].mu > 31.0


def test_engine_params_change_the_outcome_which_is_why_algo_version_exists():
    matches = [match(i, "blue") for i in range(1, 4)]
    default = replay(seed_events(), matches)
    other = replay(seed_events(), matches, RatingEngine(EngineParams(beta=2.0)))
    assert default.players[1] != other.players[1]


def test_ladder_change_rebuilds_standing_without_changing_ratings():
    from bot.features.ratings.ladder import LadderConfig, TierConfig

    shifted = LadderConfig(
        version=2,
        tiers=tuple(
            TierConfig(t.key, t.names, t.floor + 3, t.divisions) for t in DEFAULT_LADDER.tiers
        ),
    )
    matches = [match(i, "blue") for i in range(1, 7)]
    base = replay(seed_events(), matches)
    retuned = replay(seed_events(), matches, ladder=shifted)
    assert {p: (s.mu, s.sigma) for p, s in base.players.items()} == {
        p: (s.mu, s.sigma) for p, s in retuned.players.items()
    }
    assert base.players[1].standing != retuned.players[1].standing


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (MatchIn(1, T0, "green", BLUE, RED), "invalid winner"),
        (MatchIn(1, T0, "blue", BLUE, RED[:4]), "unbalanced"),
        (MatchIn(1, T0, "blue", BLUE, (5, 7, 8, 9, 10)), "more than once"),
        (MatchIn(1, T0, "blue", BLUE, (6, 7, 8, 9, 99)), "without a rating"),
    ],
)
def test_unreplayable_matches_raise(bad, message):
    with pytest.raises(ReplayError, match=message):
        replay(seed_events(), [bad])


def test_unknown_event_kind_raises():
    with pytest.raises(ReplayError, match="Unknown rating event kind"):
        replay([RatingEventIn(1, 1, "bogus", 25, 8, T0)], [])
