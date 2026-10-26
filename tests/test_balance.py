import itertools
import random
import time

import pytest

from bot.features.balance.engine import (
    _options,
    _pair_score,
    balance,
    diagnose,
    enumerate_splits,
    role_cost,
    role_strength,
    solve_split,
    win_probability,
)
from bot.features.balance.model import (
    BalanceParams,
    PlayerInput,
    Proposal,
    balance_band,
)
from bot.features.players.proficiency import ROLES

P = BalanceParams()


def prof(main: str | None = None, second: str | None = None, third: str | None = None, base=1):
    """A role profile: a main (5), optionally a second (4) and third (3), everything else `base`."""
    d: dict[str, int | None] = dict.fromkeys(ROLES, base)
    if main:
        d[main] = 5
    if second:
        d[second] = 4
    if third:
        d[third] = 3
    return d


def roster(mus, sigma=5.0, proficiency=None) -> list[PlayerInput]:
    return [PlayerInput(i, mu, sigma, proficiency or {}) for i, mu in enumerate(mus, start=1)]


def ids(team) -> set[int]:
    return {s.player_id for s in team}


def role_of(proposal: Proposal, player_id: int) -> str:
    return next(s.role for s in (*proposal.blue, *proposal.red) if s.player_id == player_id)


def team_of(proposal: Proposal, player_id: int) -> str:
    return "blue" if player_id in ids(proposal.blue) else "red"


# --- enumeration and probability ----


def test_there_are_126_unordered_splits_each_once():
    splits = list(enumerate_splits(list(range(10))))
    assert len(splits) == 126
    keys = {frozenset((frozenset(a), frozenset(b))) for a, b in splits}
    assert len(keys) == 126  # no mirrored duplicates
    assert all(len(a) == len(b) == 5 and not set(a) & set(b) for a, b in splits)
    # (the brief's 252 counts which five go on team A; unordered splits are half of that)
    assert len(list(itertools.combinations(range(10), 5))) == 252


def test_win_probability_symmetry_and_direction():
    even = roster([25] * 10)
    assert win_probability(even[:5], even[5:]) == pytest.approx(0.5)
    strong, weak = roster([30] * 5), roster([20] * 5)
    assert win_probability(strong, weak) > 0.5
    assert win_probability(strong, weak) + win_probability(weak, strong) == pytest.approx(1.0)


def test_uncertainty_pulls_the_probability_toward_a_coin_flip():
    sure = win_probability(roster([30] * 5, 1.0), roster([20] * 5, 1.0))
    unsure = win_probability(roster([30] * 5, 9.0), roster([20] * 5, 9.0))
    assert 0.5 < unsure < sure


def test_balance_bands_are_coarse_labels():
    assert [balance_band(p) for p in (0.5, 0.54, 0.6, 0.8)] == [
        "even",
        "slight",
        "clear",
        "lopsided",
    ]
    assert balance_band(0.4) == balance_band(0.6)  # symmetric


# --- structure of the result ----


def test_every_proposal_is_five_v_five_with_each_role_once_per_side():
    players = roster([20, 22, 24, 26, 28, 30, 25, 25, 27, 23])
    result = balance(players, rng=random.Random(1))
    assert result.problems == []
    assert 1 <= len(result.proposals) <= P.max_proposals
    for proposal in result.proposals:
        assert ids(proposal.blue) | ids(proposal.red) == set(range(1, 11))
        assert not ids(proposal.blue) & ids(proposal.red)
        assert [s.role for s in proposal.blue] == list(ROLES)
        assert [s.role for s in proposal.red] == list(ROLES)


def test_proposals_are_distinct_splits_sorted_by_quality():
    result = balance(roster(range(20, 30)), rng=random.Random(2))
    partitions = [p.partition for p in result.proposals]
    assert len(set(partitions)) == len(partitions)
    scores = [p.score for p in result.proposals]
    assert scores[1:] == sorted(scores[1:])  # after the shuffled near-tie head


def test_same_seed_same_result_and_variety_only_among_near_ties():
    players = roster([25] * 10)  # every split is equal: pure tie-breaking
    a = balance(players, rng=random.Random(7))
    b = balance(players, rng=random.Random(7))
    assert [p.to_dict() for p in a.proposals] == [p.to_dict() for p in b.proposals]
    firsts = {balance(players, rng=random.Random(s)).proposals[0].partition for s in range(6)}
    assert len(firsts) > 1  # different seeds, different (equally good) teams


def test_the_first_proposal_is_always_within_epsilon_of_the_true_best():
    players = roster([18, 31, 22, 27, 24, 29, 20, 26, 23, 25])
    everything = BalanceParams(max_proposals=126)
    best = min(balance(players, everything, random.Random(0)).proposals, key=lambda p: p.score)
    for seed in range(4):
        first = balance(players, rng=random.Random(seed)).proposals[0]
        assert first.score <= best.score + P.tie_epsilon + 1e-12


def test_sides_are_not_always_the_same_for_the_first_player():
    sides = set()
    for seed in range(8):
        proposal = balance(roster(range(20, 30)), rng=random.Random(seed)).proposals[0]
        sides.add(1 in ids(proposal.blue))
    assert sides == {True, False}


def test_balanced_ratings_give_an_even_match():
    players = roster([20, 22, 24, 26, 28, 30, 25, 25, 27, 23], proficiency=prof("MID", base=3))
    for proposal in balance(players, rng=random.Random(3)).proposals:
        assert abs(proposal.blue_win_prob - 0.5) < 0.03


def test_wrong_player_counts_are_rejected():
    assert diagnose(roster([25] * 9))[0].kind == "wrong_player_count"
    assert balance(roster([25] * 11)).proposals == []
    duplicate = roster([25] * 10)
    duplicate[1] = PlayerInput(1, 25, 5)
    assert diagnose(duplicate)[0].kind == "wrong_player_count"


def test_it_is_fast_enough_to_run_inside_a_bot():
    rng = random.Random(3)
    players = [
        PlayerInput(i, rng.uniform(20, 30), 5, {r: rng.choice([None, 1, 2, 3, 4]) for r in ROLES})
        for i in range(1, 11)
    ]
    start = time.perf_counter()
    balance(players, rng=rng)
    assert time.perf_counter() - start < 2.0  # about 0.3 s on a laptop


# --- roles come from proficiency only ----


def test_role_cost_and_strength_follow_proficiency():
    p = PlayerInput(1, 25, 5, proficiency={"MID": 5, "TOP": 1, "JUNGLE": None, "ADC": 3})
    assert role_cost(p, "MID", P) < role_cost(p, "ADC", P) < role_cost(p, "TOP", P)
    assert role_cost(p, "JUNGLE", P) > role_cost(p, "ADC", P)  # unknown is a bit worse than "3"
    assert role_strength(p, "MID", P) == 25  # no loss on a main role
    assert role_strength(p, "TOP", P) < role_strength(p, "ADC", P) < role_strength(p, "MID", P)


def test_each_player_gets_the_role_they_are_best_at_when_it_is_possible():
    mains = ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"] * 2
    players = [PlayerInput(i, 25, 5, prof(mains[i - 1], base=2)) for i in range(1, 11)]
    proposal = balance(players, rng=random.Random(1)).proposals[0]
    for slot in (*proposal.blue, *proposal.red):
        assert slot.role == mains[slot.player_id - 1]  # nobody off their main
    assert [s.role for s in proposal.blue] == list(ROLES)


def test_five_junglers_one_per_team_and_the_rest_go_to_their_next_best_role():
    seconds = {1: "TOP", 2: "MID", 3: "ADC", 4: "SUPPORT", 5: "TOP"}
    junglers = [PlayerInput(i, 25, 5, prof("JUNGLE", seconds[i])) for i in range(1, 6)]
    others = [
        PlayerInput(6, 25, 5, prof("TOP")),
        PlayerInput(7, 25, 5, prof("MID")),
        PlayerInput(8, 25, 5, prof("ADC")),
        PlayerInput(9, 25, 5, prof("SUPPORT")),
        PlayerInput(10, 25, 5, prof("TOP", "MID")),
    ]
    players = junglers + others
    proposal = balance(players, rng=random.Random(1)).proposals[0]
    jungle_players = [s.player_id for s in (*proposal.blue, *proposal.red) if s.role == "JUNGLE"]
    assert len(jungle_players) == 2
    assert {team_of(proposal, j) for j in jungle_players} == {"blue", "red"}  # one per team
    assert set(jungle_players) <= {1, 2, 3, 4, 5}
    # The three junglers who are not in the jungle play a role they know well, never a 1.
    for j in {1, 2, 3, 4, 5} - set(jungle_players):
        proficiency = players[j - 1].proficiency[role_of(proposal, j)]
        assert proficiency is not None
        assert proficiency >= 3
    worst_case = sum(role_cost(p, "TOP", P) for p in players)  # a clumsy line-up, for scale
    assert proposal.role_cost < worst_case


def test_four_junglers_two_stay_and_two_move_to_their_second_role():
    seconds = ["TOP", "MID", "ADC", "SUPPORT"]
    players = [PlayerInput(i, 25, 5, prof("JUNGLE", seconds[i - 1])) for i in range(1, 5)]
    players += [PlayerInput(i, 25, 5, prof(ROLES[i % 5])) for i in range(5, 11)]
    proposal = balance(players, rng=random.Random(2)).proposals[0]
    jungle = [s.player_id for s in (*proposal.blue, *proposal.red) if s.role == "JUNGLE"]
    assert len(jungle) == 2
    for j in (1, 2, 3, 4):
        if j not in jungle:
            assert role_of(proposal, j) == seconds[j - 1]  # their second-best, not a random role


def test_everyone_with_the_same_main_still_gets_a_valid_distinct_role_each():
    players = [PlayerInput(i, 25, 5, prof("MID", ROLES[i % 5], base=2)) for i in range(1, 11)]
    proposal = balance(players, rng=random.Random(4)).proposals[0]
    assert [s.role for s in proposal.blue] == list(ROLES)
    assert [s.role for s in proposal.red] == list(ROLES)
    mids = [s.player_id for s in (*proposal.blue, *proposal.red) if s.role == "MID"]
    assert len(mids) == 2
    assert {team_of(proposal, m) for m in mids} == {"blue", "red"}


def test_unknown_proficiency_is_handled_and_gets_some_role():
    proposal = balance(roster([25] * 10), rng=random.Random(1)).proposals[0]
    assert [s.role for s in proposal.blue] == list(ROLES)
    assert proposal.role_cost == pytest.approx(10 * P.unknown_prof_cost)


def test_a_player_proficient_in_one_role_only_gets_it_when_possible():
    players = roster([25] * 10, proficiency=prof(base=2))
    players[3] = PlayerInput(4, 25, 5, prof("SUPPORT", base=1))
    players[7] = PlayerInput(8, 25, 5, prof("ADC", base=1))
    proposal = balance(players, rng=random.Random(5)).proposals[0]
    assert role_of(proposal, 4) == "SUPPORT"
    assert role_of(proposal, 8) == "ADC"


def test_being_off_role_makes_a_player_weaker_in_the_team_strength():
    on = PlayerInput(1, 30, 5, prof("MID", base=1))
    assert role_strength(on, "MID", P) > role_strength(on, "TOP", P)
    team_on = [on, *roster([25] * 4, proficiency=prof("TOP", base=5))][:5]
    options = _options(team_on, P)
    mid_on = next(o for o in options if o.roles[0] == ROLES.index("MID"))
    top_off = next(o for o in options if o.roles[0] == ROLES.index("TOP"))
    assert mid_on.lane[ROLES.index("MID")] > top_off.lane[ROLES.index("TOP")]


# --- lane matchups ----


def test_two_masters_who_both_main_mid_meet_in_mid():
    masters = [PlayerInput(1, 31, 5, prof("MID", "TOP")), PlayerInput(2, 31, 5, prof("MID", "ADC"))]
    golds = [PlayerInput(i, 25, 5, prof(ROLES[i % 5], ROLES[(i + 1) % 5])) for i in range(3, 11)]
    for seed in range(3):
        proposal = balance(masters + golds, rng=random.Random(seed)).proposals[0]
        assert role_of(proposal, 1) == role_of(proposal, 2) == "MID"
        assert team_of(proposal, 1) != team_of(proposal, 2)  # opposite sides: they face each other


def test_equally_strong_players_face_each_other_in_the_same_lane():
    players = [
        PlayerInput(1, 31, 5, prof("MID", "TOP")),
        PlayerInput(2, 31, 5, prof("MID", "TOP")),
        *[PlayerInput(i, 24, 5, prof(ROLES[i % 5], ROLES[(i + 2) % 5])) for i in range(3, 11)],
    ]
    proposal = balance(players, rng=random.Random(8)).proposals[0]
    # Either both masters are in the same lane against each other, or in lanes where each faces
    # a player of matching strength: the lane gap of the chosen line-up beats any mismatch.
    roles = (role_of(proposal, 1), role_of(proposal, 2))
    if team_of(proposal, 1) != team_of(proposal, 2):
        assert roles[0] == roles[1]


def test_the_lane_term_changes_which_line_up_wins():
    players = [
        PlayerInput(1, 31, 5, prof("MID")),
        PlayerInput(2, 31, 5, prof("MID")),
        *[PlayerInput(i, 24, 5, prof(ROLES[i % 5])) for i in range(3, 11)],
    ]
    no_lanes = BalanceParams(w_lane=0.0)
    strict = BalanceParams(w_lane=0.1)
    gaps = {}
    for name, params in (("none", no_lanes), ("strict", strict)):
        proposal = balance(players, params, random.Random(1)).proposals[0]
        by_id = {p.player_id: p for p in players}
        gap = sum(
            abs(
                role_strength(by_id[next(s.player_id for s in proposal.blue if s.role == r)], r, P)
                - role_strength(by_id[next(s.player_id for s in proposal.red if s.role == r)], r, P)
            )
            for r in ROLES
        )
        gaps[name] = gap
    assert gaps["strict"] <= gaps["none"] + 1e-9  # a heavier lane weight never widens the gaps


def test_both_teams_playing_off_role_symmetrically_stays_balanced():
    players = roster([25] * 10, proficiency=prof("MID", base=2))
    proposal = balance(players, rng=random.Random(1)).proposals[0]
    assert abs(proposal.blue_win_prob - 0.5) < 0.02


# --- strength distribution ----


def test_one_very_strong_player_among_weaker_ones_still_gets_a_balanced_split():
    players = roster([31.2] + [23.2] * 9, proficiency=prof(base=3))
    proposal = balance(players, rng=random.Random(1)).proposals[0]
    assert abs(proposal.blue_win_prob - 0.5) < 0.2
    everything = balance(players, BalanceParams(max_proposals=126), random.Random(0))
    assert proposal.score <= min(p.score for p in everything.proposals) + P.tie_epsilon


def test_the_strongest_three_are_not_stacked_on_one_team_when_ratings_are_otherwise_equal():
    players = roster([30.0] * 10, proficiency=prof(base=3))  # only the distribution term differs
    for seed in range(3):
        for proposal in balance(players, rng=random.Random(seed)).proposals:
            elite = {1, 2, 3}  # top three by (rating, id)
            assert 0 < len(elite & ids(proposal.blue)) < 3


def test_elite_concentration_penalty_can_be_tuned_off():
    players = roster([30.0] * 10, proficiency=prof(base=3))
    off = BalanceParams(elite_penalty=0.0, max_proposals=126)
    result = balance(players, params=off, rng=random.Random(0))
    stacked = [p for p in result.proposals if len({1, 2, 3} & ids(p.blue)) in (0, 3)]
    assert stacked  # allowed again: all splits score the same without the penalty


def test_three_masters_on_a_losing_streak_are_still_spread_out():
    players = roster([28.2, 28.2, 28.2] + [23.2] * 7, proficiency=prof(base=3))
    proposal = balance(players, rng=random.Random(5)).proposals[0]
    assert 0 < len({1, 2, 3} & ids(proposal.blue)) < 3


def test_stacked_stars_are_avoided_when_totals_are_equal():
    mus = [30, 30, 20, 20, 20, 25, 25, 25, 25, 25]
    players = roster(mus, proficiency=prof(base=3))
    proposal = balance(players, rng=random.Random(1)).proposals[0]
    blue = sorted(players[i - 1].mu for i in ids(proposal.blue))
    red = sorted(players[i - 1].mu for i in ids(proposal.red))
    assert abs(sum(blue) - sum(red)) <= 5  # the total is odd, so 5 is the closest possible
    assert abs(max(blue) - max(red)) <= 5


# --- accuracy of the role search ----


def test_the_role_search_matches_exhaustive_search_on_the_best_splits():
    """For the splits it ranks best, the alternating search must be within a hair of the true
    optimum over all 120 x 120 role assignments."""
    rng = random.Random(21)
    for _ in range(2):
        players = [
            PlayerInput(
                i,
                rng.uniform(21, 29),
                rng.uniform(4, 6),
                {r: rng.choice([None, 1, 2, 3, 4]) for r in ROLES} | {rng.choice(ROLES): 5},
            )
            for i in range(1, 11)
        ]
        by_id = {p.player_id: p for p in players}
        sigma_sq = sum(p.sigma**2 for p in players)
        ranked = []
        for blue_ids, red_ids in enumerate_splits(sorted(by_id)):
            ob = _options([by_id[i] for i in sorted(blue_ids)], P)
            orr = _options([by_id[i] for i in sorted(red_ids)], P)
            ranked.append((solve_split(ob, orr, P, sigma_sq)[0], ob, orr))
        ranked.sort(key=lambda item: item[0])
        for rank, (score, ob, orr) in enumerate(ranked[:4]):
            exhaustive = min(_pair_score(a, b, P, sigma_sq)[0] for a in ob for b in orr)
            assert score <= exhaustive + (0.005 if rank == 0 else 0.02)  # tight for the winner


# --- storage and random data ----


def test_proposal_round_trips_through_json():
    proposal = balance(roster(range(20, 30)), rng=random.Random(1)).proposals[0]
    again = Proposal.from_dict(proposal.to_dict())
    assert again == proposal
    assert again.partition == proposal.partition


def test_random_rosters_always_yield_valid_proposals():
    rng = random.Random(11)
    for _ in range(5):
        players = [
            PlayerInput(
                i,
                rng.uniform(15, 35),
                rng.uniform(2, 8),
                {r: rng.choice([None, 1, 2, 3, 4, 5]) for r in ROLES},
            )
            for i in range(1, 11)
        ]
        result = balance(players, rng=rng)
        assert result.problems == []
        for proposal in result.proposals:
            assert [s.role for s in proposal.blue] == list(ROLES)
            assert [s.role for s in proposal.red] == list(ROLES)
            assert ids(proposal.blue) | ids(proposal.red) == set(range(1, 11))
            assert 0.0 <= proposal.blue_win_prob <= 1.0
