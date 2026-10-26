"""Team balancing for ten players. Pure and deterministic for a given RNG.

Every player has a proficiency (1-5, or unknown) on each of the five roles, and a rating. A player
on a role they know less well is *weaker there*, so their strength depends on the role they get.
For each of the 126 unordered splits into two teams of five we choose the roles for both teams,
then score the split on:

* win probability from the teams' summed role strengths,
* role fit (how comfortable everyone is on the role they were given),
* lane matchups: opponents in the same role should be about equally strong, so two strong players
  meet each other (both in their lane, or both off it) instead of one crushing a weak opponent,
* how the strongest players are distributed across the two teams.

Roles are never chosen by the players: with five junglers in the lobby, one plays jungle and the
others go to their next-best role, whichever combination costs everyone least.

Role choice is solved exactly per team by dynamic programming over the set of roles used. The lane
term couples the two teams, so the two sides are improved alternately (each side's best response
to the other); this converges in a few rounds and is checked against exhaustive search in tests.
"""

import itertools
import math
import random
from collections.abc import Iterator, Sequence
from typing import NamedTuple

from bot.features.balance.model import (
    BalanceParams,
    BalanceResult,
    PlayerInput,
    Problem,
    Proposal,
    Slot,
)
from bot.features.players.proficiency import ROLES

TEAM_SIZE = 5
PLAYERS = 2 * TEAM_SIZE


def _p_blue(diff: float, sigma_sq: float, count: int, beta: float) -> float:
    spread = math.sqrt(count * beta**2 + sigma_sq)
    return 0.5 * (1 + math.erf(diff / spread / math.sqrt(2)))


def win_probability(
    blue: Sequence[PlayerInput], red: Sequence[PlayerInput], beta: float = 25 / 6
) -> float:
    """P(blue wins) from raw ratings and uncertainty (the Weng-Lin / TrueSkill form)."""
    sigma_sq = sum(p.sigma**2 for p in blue) + sum(p.sigma**2 for p in red)
    diff = sum(p.mu for p in blue) - sum(p.mu for p in red)
    return _p_blue(diff, sigma_sq, len(blue) + len(red), beta)


def enumerate_splits(ids: Sequence[int]) -> Iterator[tuple[tuple[int, ...], tuple[int, ...]]]:
    """All 126 unordered splits: the first id is fixed on the first team to avoid mirror copies."""
    first, rest = ids[0], ids[1:]
    for others in itertools.combinations(rest, TEAM_SIZE - 1):
        blue = (first, *others)
        yield blue, tuple(i for i in ids if i not in blue)


def role_cost(player: PlayerInput, role: str, params: BalanceParams) -> float:
    """How unhappy the player is on the role: 0 on a 5, more the less they know it."""
    prof = player.proficiency.get(role)
    return params.unknown_prof_cost if prof is None else params.prof_cost[prof]


def role_strength(player: PlayerInput, role: str, params: BalanceParams) -> float:
    """The player's rating *on this role*: lower where they are less proficient."""
    prof = player.proficiency.get(role)
    loss = params.unknown_prof_loss if prof is None else params.prof_loss[prof]
    return player.mu - loss


def diagnose(players: Sequence[PlayerInput]) -> list[Problem]:
    """Why a lobby cannot be balanced. Any ten players can fill five roles, so only the count."""
    if len(players) != PLAYERS or len({p.player_id for p in players}) != PLAYERS:
        return [Problem("wrong_player_count", str(len(players)))]
    return []


class _Option(NamedTuple):
    """One way to give a team's five players the five roles, with everything the score needs.

    These depend only on the team, not on the opponent, so they are computed once per team and
    reused by every split that contains it.
    """

    players: tuple[int, ...]  # index (in the team's sorted-id order) of whoever plays each role
    cost: float  # summed role cost
    lane: tuple[float, ...]  # strength of whoever plays each role
    total: float  # summed strength
    top: float  # summed strength of the top-k
    spread: float  # standard deviation of the strengths


def _options(team: Sequence[PlayerInput], params: BalanceParams) -> list[_Option]:
    """All 120 assignments of the team's players to roles. This is the hot loop of the balancer
    (about 30,000 options per lobby), so the arithmetic is unrolled over per-role columns."""
    n = len(ROLES)
    c0, c1, c2, c3, c4 = ([role_cost(p, ROLES[r], params) for p in team] for r in range(n))
    s0, s1, s2, s3, s4 = ([role_strength(p, ROLES[r], params) for p in team] for r in range(n))
    topk = params.topk
    sqrt = math.sqrt
    options = []
    append = options.append
    for perm in itertools.permutations(range(n)):
        a, b, c, d, e = perm
        l0, l1, l2, l3, l4 = s0[a], s1[b], s2[c], s3[d], s4[e]
        total = l0 + l1 + l2 + l3 + l4
        mean = total / 5
        spread = sqrt(
            (
                (l0 - mean) ** 2
                + (l1 - mean) ** 2
                + (l2 - mean) ** 2
                + (l3 - mean) ** 2
                + (l4 - mean) ** 2
            )
            / 5
        )
        lane = (l0, l1, l2, l3, l4)
        top = sum(sorted(lane, reverse=True)[:topk])
        append(_Option(perm, c0[a] + c1[b] + c2[c] + c3[d] + c4[e], lane, total, top, spread))
    return options


def _pair_score(
    a: _Option, b: _Option, params: BalanceParams, sigma_sq: float
) -> tuple[float, float]:
    """(score, P(blue wins)) for blue playing option `a` against red playing option `b`."""
    p_blue = _p_blue(a.total - b.total, sigma_sq, PLAYERS, params.beta)
    lane_gap = sum(abs(x - y) for x, y in zip(a.lane, b.lane, strict=True))
    score = (
        params.w_prob * abs(p_blue - 0.5)
        + params.w_role * (a.cost + b.cost)
        + params.w_lane * lane_gap
        + params.w_topk * abs(a.top - b.top)
        + params.w_spread * abs(a.spread - b.spread)
    )
    return score, p_blue


def solve_split(
    opts_blue: Sequence[_Option],
    opts_red: Sequence[_Option],
    params: BalanceParams,
    sigma_sq: float,
) -> tuple[float, float, _Option, _Option]:
    """Best roles for both teams of one split (score without the elite term, P(blue), options).

    From several starting points (each team on its most comfortable roles, or on its strongest
    line-up), let each side in turn take its exact best response to the other until neither
    changes, and keep the best result. Checked against exhaustive search in the tests.
    """
    # The search evaluates ~100k pairs per lobby, so its score is an unrolled copy of
    # `_pair_score` (same value) with the constants hoisted: |P - 0.5| = 0.5 * |erf(z)|.
    w_prob, w_role, w_lane = params.w_prob * 0.5, params.w_role, params.w_lane
    w_topk, w_spread = params.w_topk, params.w_spread
    inv = 1 / (math.sqrt(PLAYERS * params.beta**2 + sigma_sq) * math.sqrt(2))
    erf = math.erf

    def score(a: _Option, b: _Option) -> float:
        la, lb = a.lane, b.lane
        return (
            w_prob * abs(erf((a.total - b.total) * inv))
            + w_role * (a.cost + b.cost)
            + w_lane
            * (
                abs(la[0] - lb[0])
                + abs(la[1] - lb[1])
                + abs(la[2] - lb[2])
                + abs(la[3] - lb[3])
                + abs(la[4] - lb[4])
            )
            + w_topk * abs(a.top - b.top)
            + w_spread * abs(a.spread - b.spread)
        )

    def starts(opts: Sequence[_Option]) -> list[_Option]:
        cheapest = min(opts, key=lambda o: o.cost)
        strongest = max(opts, key=lambda o: o.total)
        return [cheapest] if strongest is cheapest else [cheapest, strongest]

    best: tuple[float, float, _Option, _Option] | None = None
    for start_a in starts(opts_blue):
        for start_b in starts(opts_red):
            a, b = start_a, start_b
            for _ in range(params.lane_rounds):
                new_a = min(opts_blue, key=lambda o: score(o, b))  # noqa: B023
                new_b = min(opts_red, key=lambda o: score(new_a, o))  # noqa: B023
                if new_a is a and new_b is b:
                    break
                a, b = new_a, new_b
            final, p_blue = _pair_score(a, b, params, sigma_sq)
            if best is None or final < best[0]:
                best = (final, p_blue, a, b)
    assert best is not None
    return best


def balance(
    players: Sequence[PlayerInput],
    params: BalanceParams | None = None,
    rng: random.Random | None = None,
) -> BalanceResult:
    """The best proposals for ten players, best first (near-ties shuffled)."""
    params = params or BalanceParams()
    rng = rng or random.Random()
    problems = diagnose(players)
    if problems:
        return BalanceResult([], problems)

    by_id = {p.player_id: p for p in players}
    ids = sorted(by_id)
    elite = {
        p.player_id
        for p in sorted(players, key=lambda p: (-p.mu, p.player_id))[: params.elite_count]
    }
    team_options: dict[tuple[int, ...], list[_Option]] = {}

    def options_for(team_ids: tuple[int, ...]) -> tuple[tuple[int, ...], list[_Option]]:
        key = tuple(sorted(team_ids))
        if key not in team_options:
            team_options[key] = _options([by_id[i] for i in key], params)
        return key, team_options[key]

    scored: list[tuple[float, tuple[int, ...], Proposal]] = []
    for blue_ids, red_ids in enumerate_splits(ids):
        blue_key, opts_blue = options_for(blue_ids)
        red_key, opts_red = options_for(red_ids)
        sigma_sq = sum(by_id[i].sigma ** 2 for i in ids)
        score, p_blue, a, b = solve_split(opts_blue, opts_red, params, sigma_sq)
        elite_on_blue = len(elite & set(blue_key))
        if params.elite_count >= 2 and elite_on_blue in (0, len(elite)):
            score += params.elite_penalty  # all the strongest players on one side
        proposal = Proposal(
            blue=_slots(blue_key, a.players),
            red=_slots(red_key, b.players),
            blue_win_prob=p_blue,
            role_cost=a.cost + b.cost,
            score=score,
        )
        scored.append((score, blue_key, proposal))

    scored.sort(key=lambda item: (item[0], item[1]))
    best_score = scored[0][0]
    near = [s for s in scored if s[0] - best_score <= params.tie_epsilon]
    rest = [s for s in scored if s[0] - best_score > params.tie_epsilon]
    rng.shuffle(near)
    ordered = [item[2] for item in near + rest][: params.max_proposals]
    return BalanceResult([_maybe_swap_sides(p, rng) for p in ordered], [])


def _slots(team_ids: Sequence[int], players: Sequence[int]) -> tuple[Slot, ...]:
    return tuple(Slot(team_ids[players[r]], ROLES[r]) for r in range(len(ROLES)))


def _maybe_swap_sides(proposal: Proposal, rng: random.Random) -> Proposal:
    """The enumeration pins the first player to blue; flip sides at random for fairness."""
    if rng.random() < 0.5:
        return proposal
    return Proposal(
        blue=proposal.red,
        red=proposal.blue,
        blue_win_prob=1 - proposal.blue_win_prob,
        role_cost=proposal.role_cost,
        score=proposal.score,
    )
