"""Effective matchmaking estimate E from the in-house rating H and the rank-based estimate R."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from bot.features.ratings.params import MatchmakingParams


@dataclass(frozen=True)
class Estimate:
    mu: float
    sigma: float
    rank_mu: float | None  # R, None when no rank signal resolves
    deviation: float  # d = mu_H - R (0 without a rank)
    diverged: bool  # |d| is far beyond the bound: rank may be stale, or results are decisive
    form: float = 0.0  # recent-form adjustment already included in `mu`


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


@dataclass(frozen=True)
class FormGame:
    """One recent confirmed game of a player, as the form signal sees it."""

    won: bool
    expected: float  # the predicted win chance of the player's team before the game


def form_adjustment(games: Sequence[FormGame], params: MatchmakingParams) -> float:
    """Rating points to add to E for a player who has beaten (or missed) the predictions lately.

    `games` are oldest first. A win against a 50% prediction counts +0.5, a loss -0.5; a win the
    bot expected counts little. Fewer than `form_games` games count proportionally less.
    """
    if params.form_weight == 0 or params.form_games <= 0 or not games:
        return 0.0
    recent = games[-params.form_games :]
    residual = sum((1.0 if g.won else 0.0) - g.expected for g in recent) / len(recent)
    scaled = params.form_weight * residual * len(recent) / params.form_games
    return max(-params.form_cap, min(params.form_cap, scaled))


def default_seed(params: MatchmakingParams) -> tuple[float, float]:
    return params.default_mu, params.default_sigma


def seed_from_rank(rank_mu: float, params: MatchmakingParams) -> tuple[float, float]:
    return rank_mu, params.sigma_rank_seed
