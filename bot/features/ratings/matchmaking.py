"""Effective matchmaking estimate E from the in-house rating H and the rank-based estimate R."""

import math
from dataclasses import dataclass

from bot.features.ratings.params import MatchmakingParams


@dataclass(frozen=True)
class Estimate:
    mu: float
    sigma: float
    rank_mu: float | None  # R, None when no rank signal resolves
    deviation: float  # d = mu_H - R (0 without a rank)
    diverged: bool  # |d| is far beyond the bound: rank may be stale, or results are decisive


def effective_estimate(
    mu_h: float, sigma_h: float, rank_mu: float | None, params: MatchmakingParams
) -> Estimate:
    """E = R + D * tanh((H - R) / D), D = d_up above R and d_down below it.

    Smooth and monotone in H, equal to H for small differences, and E - R never leaves
    [-d_down, +d_up] however long a streak lasts. Without a rank E = H. sigma_E = sigma_H.
    """
    if rank_mu is None:
        return Estimate(mu_h, sigma_h, None, 0.0, False)
    d = mu_h - rank_mu
    bound = params.d_up if d > 0 else params.d_down
    mu_e = rank_mu + bound * math.tanh(d / bound)
    return Estimate(mu_e, sigma_h, rank_mu, d, abs(d) > params.divergence_factor * bound)


def default_seed(params: MatchmakingParams) -> tuple[float, float]:
    return params.default_mu, params.default_sigma


def seed_from_rank(rank_mu: float, params: MatchmakingParams) -> tuple[float, float]:
    return rank_mu, params.sigma_rank_seed
