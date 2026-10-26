"""Dev tool (never imported by the bot): inspect ladder and matchmaking-estimate distributions.

Simulates synthetic players with noisy League ranks and hidden true skill playing random
10-player lobbies, then prints the resulting in-house tier distribution, how far the matchmaking
estimate E drifted from the rank-based estimate R, and how lopsided the matches were.

Teams are the best of the 126 splits on the sum of E, a stand-in until the real balancer exists
(milestone M2), so "lopsided" below measures E against hidden true skill, not the final balancer.
Everything is seeded, so runs are reproducible and different parameter sets are comparable:

    python tools/ladder_sim.py --players 14 --matches 300 --seed 1
    python tools/ladder_sim.py --d-down 1.5 --d-up 2.0 --tier-shift 2 --matches 300
"""

import argparse
import itertools
import random
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from bot.features.ratings.engine import Rating, RatingEngine
from bot.features.ratings.ladder import DEFAULT_LADDER, LadderConfig, TierConfig
from bot.features.ratings.matchmaking import effective_estimate, seed_from_rank
from bot.features.ratings.params import MatchmakingParams
from bot.features.ratings.rank import DIVISIONS, TIERS, rank_mu
from bot.features.ratings.replay import (
    MatchIn,
    PlayerState,
    RatingEventIn,
    apply_event,
    apply_match,
)

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SimResult:
    tiers: Counter[str]
    drift: list[float]  # E - R per player
    max_abs_drift: float
    lopsided_share: float  # matches whose true win probability fell outside [0.4, 0.6]
    matches: int


def _random_rank(rng: random.Random) -> float:
    """Roughly bell-shaped over Iron-Diamond, like a friends' group with a few outliers."""
    tier = min(len(TIERS) - 1, max(0, round(rng.gauss(3.0, 1.5))))
    return rank_mu(TIERS[tier], rng.choice(DIVISIONS), MatchmakingParams())


def simulate(
    players: int,
    matches: int,
    seed: int,
    params: MatchmakingParams | None = None,
    ladder: LadderConfig = DEFAULT_LADDER,
    rank_noise: float = 2.5,
) -> SimResult:
    params = params or MatchmakingParams()
    rng = random.Random(seed)
    engine = RatingEngine()
    ranks = {pid: _random_rank(rng) for pid in range(1, players + 1)}
    truth = {pid: ranks[pid] + rng.gauss(0, rank_noise) for pid in ranks}

    state: dict[int, PlayerState] = {}
    for pid, r in ranks.items():
        mu, sigma = seed_from_rank(r, params)
        apply_event(state, RatingEventIn(pid, pid, "seed", mu, sigma, EPOCH), ladder)

    def estimate(pid: int) -> float:
        s = state[pid]
        return effective_estimate(s.mu, s.sigma, ranks[pid], params).mu

    lopsided = 0
    for i in range(1, matches + 1):
        lobby = rng.sample(sorted(ranks), 10)
        blue, red = best_split(lobby, estimate)
        p_blue = engine.win_probability(
            [Rating(truth[p], 0.1) for p in blue], [Rating(truth[p], 0.1) for p in red]
        )
        lopsided += not 0.4 <= p_blue <= 0.6
        winner = "blue" if rng.random() < p_blue else "red"
        apply_match(state, MatchIn(i, EPOCH, winner, blue, red), engine, ladder)

    tiers: Counter[str] = Counter()
    drift = []
    for pid, s in state.items():
        st = s.standing
        tiers[ladder.label(st.tier_key or "", st.division) if st.placed else "(colocação)"] += 1
        drift.append(effective_estimate(s.mu, s.sigma, ranks[pid], params).mu - ranks[pid])
    return SimResult(tiers, drift, max(abs(d) for d in drift), lopsided / matches, matches)


def best_split(lobby: list[int], estimate) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """The 126 unordered splits (first player fixed on blue); smallest gap in summed E wins."""
    total = sum(estimate(p) for p in lobby)
    best = min(
        itertools.combinations(lobby[1:], 4),
        key=lambda rest: abs(total - 2 * (estimate(lobby[0]) + sum(estimate(p) for p in rest))),
    )
    blue = (lobby[0], *best)
    return blue, tuple(p for p in lobby if p not in blue)


def shifted_ladder(shift: float) -> LadderConfig:
    return LadderConfig(
        version=DEFAULT_LADDER.version + 1,
        tiers=tuple(
            TierConfig(t.key, t.names, t.floor + shift, t.divisions) for t in DEFAULT_LADDER.tiers
        ),
        division_width=DEFAULT_LADDER.division_width,
        demotion_buffer=DEFAULT_LADDER.demotion_buffer,
        placement_games=DEFAULT_LADDER.placement_games,
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--players", type=int, default=14)
    ap.add_argument("--matches", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--d-down", type=float, default=MatchmakingParams.d_down)
    ap.add_argument("--d-up", type=float, default=MatchmakingParams.d_up)
    ap.add_argument("--tier-shift", type=float, default=0.0, help="shift every ladder floor")
    ap.add_argument("--rank-noise", type=float, default=2.5, help="true skill vs rank spread")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # tier names have accents

    params = replace(MatchmakingParams(), d_down=args.d_down, d_up=args.d_up)
    result = simulate(
        args.players,
        args.matches,
        args.seed,
        params,
        shifted_ladder(args.tier_shift) if args.tier_shift else DEFAULT_LADDER,
        args.rank_noise,
    )
    print(f"{args.players} players, {result.matches} matches, seed {args.seed}")
    print(f"params: d_down={params.d_down} d_up={params.d_up}; ladder shift {args.tier_shift}")
    print("\nIn-house tier distribution:")
    for label, count in sorted(result.tiers.items()):
        print(f"  {label:<28} {'#' * count} {count}")
    drift = sorted(result.drift)
    print(
        f"\nE - R drift: min {drift[0]:+.2f}  median {drift[len(drift) // 2]:+.2f}  "
        f"max {drift[-1]:+.2f}  (bounds -{params.d_down} / +{params.d_up})"
    )
    print(f"Lopsided matches (true win probability outside 40-60%): {result.lopsided_share:.0%}")


if __name__ == "__main__":
    main()
