"""Replay the real games under a variant and score the prediction made before each one.

It drives the bot's own pieces (`apply_event`, `apply_match`, `effective_estimate`, the balancer's
role strength and win probability, the ladder), so the baseline variant reproduces what the bot
has stored. Only the thin loop around them lives here.
"""

import math
from dataclasses import replace

from bot.features.backtest.model import (
    Dataset,
    Final,
    Game,
    GameScore,
    Paired,
    Variant,
    VariantResult,
)
from bot.features.balance.engine import role_strength, win_probability
from bot.features.balance.model import BalanceParams, PlayerInput
from bot.features.ratings.engine import RatingEngine
from bot.features.ratings.ladder import DEFAULT_LADDER
from bot.features.ratings.matchmaking import effective_estimate
from bot.features.ratings.params import (
    DEFAULT_PROF_LOSS,
    DEFAULT_UNKNOWN_PROF_LOSS,
    EngineParams,
    MatchmakingParams,
)
from bot.features.ratings.replay import MatchIn, State, apply_event, apply_match

BASELINE = Variant("today")


def _params(variant: Variant) -> tuple[RatingEngine, MatchmakingParams, BalanceParams]:
    scale = variant.loss_scale
    table = {level: loss * scale for level, loss in DEFAULT_PROF_LOSS.items()}
    unknown = DEFAULT_UNKNOWN_PROF_LOSS * scale
    engine = RatingEngine(
        EngineParams(role_aware=variant.role_aware, role_loss=table, unknown_role_loss=unknown)
    )
    mm = MatchmakingParams()
    if variant.d_up is not None:
        mm = replace(mm, d_up=variant.d_up)
    if variant.d_down is not None:
        mm = replace(mm, d_down=variant.d_down)
    return engine, mm, BalanceParams(prof_loss=table, unknown_prof_loss=unknown)


def _predict(
    game: Game, state: State, mm: MatchmakingParams, balance: BalanceParams
) -> tuple[float, int]:
    """P(blue wins) from the state just before the game, and how many players had no profile."""
    blue: list[PlayerInput] = []
    red: list[PlayerInput] = []
    unrecorded = 0
    for p in game.players:
        rating = state[p.player_id]
        estimate = effective_estimate(rating.mu, rating.sigma, p.rank_mu, mm)
        profile = p.proficiency
        unrecorded += profile is None
        on_role = PlayerInput(p.player_id, estimate.mu, estimate.sigma, dict(profile or {}))
        strength = role_strength(on_role, p.role, balance)  # E minus the loss on this role
        (blue if p.side == "blue" else red).append(
            PlayerInput(p.player_id, strength, estimate.sigma)
        )
    return win_probability(blue, red, balance.beta), unrecorded


def _match_in(game: Game) -> MatchIn:
    return MatchIn(
        game.id,
        game.confirmed_at,
        game.winner,
        tuple(p.player_id for p in game.players if p.side == "blue"),
        tuple(p.player_id for p in game.players if p.side == "red"),
        {p.player_id: p.proficiency.get(p.role) for p in game.players if p.proficiency is not None},
    )


def run_variant(data: Dataset, variant: Variant) -> VariantResult:
    engine, mm, balance = _params(variant)
    timeline: list[tuple] = [(e.created_at, 0, e.id, e) for e in data.events]
    timeline += [(g.confirmed_at, 1, g.id, g) for g in data.games]
    timeline.sort(key=lambda item: item[:3])  # the same order as the bot's replay

    state: State = {}
    scores: list[GameScore] = []
    unrecorded = 0
    for _, _, _, item in timeline:
        if isinstance(item, Game):
            p_blue, missing = _predict(item, state, mm, balance)
            unrecorded += missing
            scores.append(GameScore(item.id, p_blue, item.winner == "blue"))
            apply_match(state, _match_in(item), engine, DEFAULT_LADDER)
            if variant.sigma_floor:
                for p in item.players:
                    if state[p.player_id].sigma < variant.sigma_floor:
                        state[p.player_id] = replace(state[p.player_id], sigma=variant.sigma_floor)
        else:
            apply_event(state, item, DEFAULT_LADDER)

    final = {}
    for pid, s in state.items():
        label = (
            DEFAULT_LADDER.label(s.standing.tier_key or "", s.standing.division)
            if s.standing.placed
            else "-"
        )
        final[pid] = Final(s.mu, s.games, label)
    return VariantResult(variant, scores, final, unrecorded)


def log_loss(result: VariantResult) -> float:
    """Mean negative log-likelihood of what really happened (a coin flip is 0.6931)."""
    return -sum(math.log(s.p_actual) for s in result.scores) / len(result.scores)


def brier(result: VariantResult) -> float:
    """Mean squared error of the predicted win chance (a coin flip is 0.25)."""
    return sum((s.p_blue - s.blue_won) ** 2 for s in result.scores) / len(result.scores)


def compare(baseline: VariantResult, other: VariantResult) -> Paired:
    """Per-game gain in log-likelihood of `other` over `baseline` (same games, paired)."""
    gains = [
        math.log(o.p_actual) - math.log(b.p_actual)
        for b, o in zip(baseline.scores, other.scores, strict=True)
    ]
    n = len(gains)
    mean = sum(gains) / n
    stderr = None
    if n > 1:
        stderr = math.sqrt(sum((g - mean) ** 2 for g in gains) / (n - 1) / n)
    return Paired(mean, stderr, sum(1 for g in gains if g > 0), n)


def check_against_stored(data: Dataset, baseline: VariantResult) -> float | None:
    """Largest difference between the replayed H and the bot's stored H (None: nothing to compare).

    Should be ~0 for the baseline; if not, the tool and the bot disagree and nothing else here
    can be trusted.
    """
    diffs = [
        abs(baseline.final[pid].mu - mu)
        for pid, mu in data.stored_mu.items()
        if pid in baseline.final
    ]
    return max(diffs) if diffs else None
