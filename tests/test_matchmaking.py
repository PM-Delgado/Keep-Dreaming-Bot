"""Matchmaking estimate E: safeguard properties and the agreed regression scenarios."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from bot.features.ratings.ladder import DEFAULT_LADDER
from bot.features.ratings.matchmaking import (
    default_seed,
    effective_estimate,
    seed_from_rank,
)
from bot.features.ratings.params import MatchmakingParams
from bot.features.ratings.rank import rank_mu
from bot.features.ratings.replay import MatchIn, RatingEventIn, replay

P = MatchmakingParams()
T0 = datetime(2026, 1, 1, tzinfo=UTC)

MASTER = rank_mu("MASTER", None, P)  # 31.2
PLAT_IV = rank_mu("PLATINUM", "IV", P)  # 26.4
GOLD_II = rank_mu("GOLD", "II", P)  # 25.2
SILVER_IV = rank_mu("SILVER", "IV", P)  # 23.2


# --- the formula ---------------------------------------------------------------------------


def test_without_a_rank_e_equals_h():
    e = effective_estimate(27.3, 5.0, None, P)
    assert (e.mu, e.sigma, e.rank_mu, e.deviation, e.diverged) == (27.3, 5.0, None, 0.0, False)


def test_at_zero_games_e_equals_r_exactly():
    mu, sigma = seed_from_rank(MASTER, P)
    e = effective_estimate(mu, sigma, MASTER, P)
    assert e.mu == MASTER
    assert e.sigma == P.sigma_rank_seed


def test_no_rank_default_seed_has_high_sigma_and_e_equals_h():
    mu, sigma = default_seed(P)
    e = effective_estimate(mu, sigma, None, P)
    assert (e.mu, e.sigma) == (25.0, pytest.approx(25 / 3))
    assert sigma > P.sigma_rank_seed


def test_e_stays_within_the_bounds_for_any_h():
    rng = random.Random(7)
    for _ in range(2000):
        r = rng.uniform(20, 35)
        h = rng.uniform(-50, 90)
        e = effective_estimate(h, 5.0, r, P)
        assert r - P.d_down <= e.mu <= r + P.d_up


def test_e_is_monotone_in_h():
    r = 27.0
    hs = [x / 10 for x in range(-200, 600)]
    es = [effective_estimate(h, 5.0, r, P).mu for h in hs]
    assert all(b >= a for a, b in zip(es, es[1:], strict=False))


def test_small_differences_pass_through_almost_undistorted():
    r = 27.0
    for d in (-0.5, -0.2, 0.0, 0.2, 0.5):
        assert effective_estimate(r + d, 5.0, r, P).mu == pytest.approx(r + d, abs=0.02)


def test_asymmetric_bounds_apply_per_direction():
    r = 27.0
    up = effective_estimate(r + 100, 5.0, r, P)
    down = effective_estimate(r - 100, 5.0, r, P)
    assert up.mu - r == pytest.approx(P.d_up, abs=1e-6)
    assert r - down.mu == pytest.approx(P.d_down, abs=1e-6)


def test_rank_and_in_house_disagree_substantially_e_sits_at_the_bound():
    r = 28.0
    way_below = effective_estimate(r - 15, 4.0, r, P)
    way_above = effective_estimate(r + 15, 4.0, r, P)
    assert way_below.mu == pytest.approx(r - P.d_down, abs=0.02)
    assert way_above.mu == pytest.approx(r + P.d_up, abs=0.02)
    assert way_below.diverged
    assert way_above.diverged
    assert not effective_estimate(r - 2, 4.0, r, P).diverged


def test_changing_params_changes_e_but_not_h():
    h = 24.0
    tight = effective_estimate(h, 5.0, MASTER, MatchmakingParams(d_down=1.0))
    loose = effective_estimate(h, 5.0, MASTER, MatchmakingParams(d_down=6.0))
    assert tight.mu > loose.mu
    assert (tight.sigma, loose.sigma) == (5.0, 5.0)


# --- regression scenarios through the replay engine ---------------------------------------


def seeds(ranks: dict[int, float], sigma=P.sigma_rank_seed) -> list[RatingEventIn]:
    return [
        RatingEventIn(i, pid, "seed", mu, sigma, T0) for i, (pid, mu) in enumerate(ranks.items(), 1)
    ]


def matches(blue, red, winners: list[str]) -> list[MatchIn]:
    return [
        MatchIn(i, T0 + timedelta(hours=i), winner, tuple(blue), tuple(red))
        for i, winner in enumerate(winners, 1)
    ]


def estimate_for(result, pid, rank_mu_value):
    state = result.players[pid]
    return effective_estimate(state.mu, state.sigma, rank_mu_value, P)


def test_master_loses_ten_in_a_row():
    ranks = {1: MASTER, 2: PLAT_IV, 3: GOLD_II, 4: GOLD_II, 5: SILVER_IV}
    ranks |= {6: PLAT_IV, 7: GOLD_II, 8: GOLD_II, 9: SILVER_IV, 10: SILVER_IV}
    # A rank-seeded rating is now fairly certain (sigma 2), so ten losses move it less than they
    # used to. This scenario is about the safeguard, so start from an uncertain rating (6.5).
    result = replay(
        seeds(ranks, sigma=6.5),
        matches([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], ["red"] * 10),
    )
    master = result.players[1]
    assert master.mu < MASTER - 3  # results matter: H falls a lot
    assert master.games == 10
    assert master.standing.tier_key != "kd_esports"  # visible standing drops
    e = estimate_for(result, 1, MASTER)
    assert e.mu >= MASTER - P.d_down
    assert e.deviation < -P.d_down  # H is far below R, yet E is held at the bound
    assert e.diverged  # and the admin diagnostics would flag it
    # Still rated above a Platinum IV's rank estimate and above the Silver IV who won ten in a
    # row. The margin over that Silver is thin by design (28.2 vs at most 27.7), but never inverts.
    assert e.mu > rank_mu("PLATINUM", "IV", P) + 1.5
    assert e.mu > estimate_for(result, 9, SILVER_IV).mu


def test_several_masters_slump_together_and_stay_the_top_players():
    masters = {1: MASTER, 2: MASTER, 3: MASTER}
    others = dict.fromkeys(range(4, 11), SILVER_IV)
    result = replay(
        seeds(masters | others),
        matches([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], ["red"] * 10),
    )
    for pid in (1, 2, 3):
        assert result.players[pid].mu < MASTER - 3
        assert estimate_for(result, pid, MASTER).mu >= MASTER - P.d_down
    by_estimate = sorted(
        range(1, 11),
        key=lambda pid: estimate_for(result, pid, MASTER if pid <= 3 else SILVER_IV).mu,
        reverse=True,
    )
    assert set(by_estimate[:3]) == {1, 2, 3}


def test_high_ranked_never_inverts_against_much_lower_ranked():
    # The worst slump for a Master and the best run for a Silver IV still leave a clear gap.
    worst_master = MASTER - P.d_down
    best_silver = SILVER_IV + P.d_up
    assert worst_master > best_silver
    for h_master in (-100.0, 0.0, 20.0):
        for h_silver in (60.0, 100.0):
            m = effective_estimate(h_master, 3.0, MASTER, P).mu
            s = effective_estimate(h_silver, 3.0, SILVER_IV, P).mu
            assert m > s


def test_similar_ranks_with_different_results_separate_but_stay_bounded():
    ranks = {1: GOLD_II, 2: GOLD_II} | dict.fromkeys(range(3, 11), GOLD_II)
    # Player 1 (blue) wins 15 of 20, interleaved; player 2 (red) is on the other side each time.
    winners = ["blue", "blue", "blue", "red"] * 5
    result = replay(seeds(ranks), matches([1, 3, 4, 5, 6], [2, 7, 8, 9, 10], winners))
    e1 = estimate_for(result, 1, GOLD_II)
    e2 = estimate_for(result, 2, GOLD_II)
    assert result.players[1].mu > result.players[2].mu
    assert e1.mu > e2.mu  # order preserved
    assert e1.mu - e2.mu <= P.d_up + P.d_down
    assert GOLD_II - P.d_down <= e2.mu < e1.mu <= GOLD_II + P.d_up


def test_rank_change_moves_e_but_not_h():
    h_mu, h_sigma = 27.0, 4.0
    before = effective_estimate(h_mu, h_sigma, rank_mu("GOLD", "II", P), P)
    after = effective_estimate(h_mu, h_sigma, rank_mu("DIAMOND", "IV", P), P)
    assert after.mu > before.mu  # climbing rank raises the anchor...
    assert after.sigma == before.sigma == h_sigma  # ...while H is untouched
    assert after.deviation < before.deviation


def test_changing_params_never_touches_replayed_history():
    # E is computed from stored inputs; the authoritative replay never sees the params.
    ranks = {pid: GOLD_II for pid in range(1, 11)}
    args = (seeds(ranks), matches([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], ["blue"] * 3))
    first = replay(*args)
    state = first.players[1]
    tight = effective_estimate(state.mu, state.sigma, GOLD_II, MatchmakingParams(d_up=0.5))
    loose = effective_estimate(state.mu, state.sigma, GOLD_II, MatchmakingParams(d_up=9.0))
    assert state.mu > GOLD_II
    assert tight.mu < loose.mu  # different parameters, different estimate...
    assert replay(*args) == first  # ...identical authoritative result
    assert DEFAULT_LADDER.placement_games == 5
