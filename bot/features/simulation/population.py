"""Populations that look like the real server: a mix of League ranks, with hidden true skill.

The bot only ever sees a player's *Riot rank* (tier and division). How good they really are in
our games is `true_mu`: the rank-based estimate plus a deviation. Most people sit close to their
rank; some are under-ranked (smurfs, people who rarely play ranked) and some over-ranked (old
peaks, inactivity). Those outliers are what the in-house rating has to discover.

Defaults describe about twenty friends from Silver/Bronze up to Master. They are assumptions, not
measurements: change them (see `python -m bot.features.simulation tune --help`) and see whether a
conclusion survives.
"""

import random
from collections.abc import Mapping
from dataclasses import replace

from bot.features.ratings.params import MatchmakingParams
from bot.features.ratings.rank import APEX_POINTS, rank_points
from bot.features.simulation.players import SimPlayer, random_player

# Relative number of players per tier (the share of a 20-player group, roughly 1:4:5:4:3:2:1).
RANK_WEIGHTS: Mapping[str, float] = {
    "BRONZE": 1,
    "SILVER": 4,
    "GOLD": 5,
    "PLATINUM": 4,
    "EMERALD": 3,
    "DIAMOND": 2,
    "MASTER": 1,
}


def rank_mu_of(tier: str, division: str | None, params: MatchmakingParams | None = None) -> float:
    """The rank-based estimate R (on the rating scale) for a Riot rank."""
    params = params or MatchmakingParams()
    return params.rank_mu_base + params.rank_mu_step * rank_points(tier, division)


def _quota(weights: Mapping[str, float], size: int) -> list[str]:
    """Tiers for `size` players in proportion to `weights` (largest remainder), so a group of
    twenty always has about its one Master and its handful of Silvers instead of a random draw."""
    total = sum(weights.values())
    exact = {t: w * size / total for t, w in weights.items()}
    counts = {t: int(x) for t, x in exact.items()}
    by_remainder = sorted(exact, key=lambda t: exact[t] - counts[t], reverse=True)
    for t in by_remainder[: size - sum(counts.values())]:
        counts[t] += 1
    return [t for t, n in counts.items() for _ in range(n)]


def realistic_population(
    rng: random.Random,
    size: int = 20,
    weights: Mapping[str, float] = RANK_WEIGHTS,
    deviation_sd: float = 1.2,
    outlier_share: float = 0.15,
    outlier_size: float = 3.0,
    params: MatchmakingParams | None = None,
) -> list[SimPlayer]:
    """`size` players with a Riot rank, a main role, and a hidden true skill.

    true_mu = R + N(0, deviation_sd), and with probability `outlier_share` a further
    +/- `outlier_size` (about two tiers): a smurf or an inactive old peak.
    """
    tiers = _quota(weights, size)
    rng.shuffle(tiers)
    players = []
    for player_id, tier in enumerate(tiers, start=1):
        division = None if tier in APEX_POINTS else rng.choice(("IV", "III", "II", "I"))
        r = rank_mu_of(tier, division, params)
        true_mu = r + rng.gauss(0, deviation_sd)
        if rng.random() < outlier_share:
            true_mu += rng.choice((-1, 1)) * outlier_size
        base = random_player(rng, player_id, "realistic")  # roles come from here
        players.append(
            replace(
                base,
                mu=r,
                sigma=5.0,
                true_mu=true_mu,
                rank=(tier, division),
                name=f"P{player_id:02d}",
            )
        )
    return players


def describe_population(players: list[SimPlayer], params: MatchmakingParams | None = None) -> str:
    lines = []
    for p in sorted(players, key=lambda x: -x.true_mu):
        tier, division = p.rank or ("?", None)
        label = tier.title() if division is None else f"{tier.title()} {division}"
        r = rank_mu_of(tier, division, params) if p.rank else float("nan")
        lines.append(
            f"  {p.name}  {label:<14} R {r:5.1f}  true {p.true_mu:5.1f}  ({p.true_mu - r:+.1f})"
        )
    return "\n".join(lines)
