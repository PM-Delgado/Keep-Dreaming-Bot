"""Tunable parameters, as code defaults that tests and tools can override.

`MatchmakingParams` only affects how the matchmaking estimate E is *computed* from stored
inputs, so changing it never rewrites history. `EngineParams` changes how in-house ratings (H)
evolve, so it is part of `algo_version` and a change triggers a replay of the caches.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import version

PARAMS_VERSION = 1

# Rating points a player loses on a role they know less well, by proficiency there (1..5). One
# table for the balancer (a player's strength on the role they would get) and for the optional
# role-aware rating update, so the two cannot drift apart.
DEFAULT_PROF_LOSS: Mapping[int, float] = {5: 0.0, 4: 0.3, 3: 0.8, 2: 1.8, 1: 3.0}
DEFAULT_UNKNOWN_PROF_LOSS = 1.0


def _default_role_loss() -> dict[int, float]:
    return dict(DEFAULT_PROF_LOSS)


@dataclass(frozen=True)
class MatchmakingParams:
    # Riot rank -> rating scale: mu_R = base + step * points (see bot.features.ratings.rank).
    rank_mu_base: float = 20.0
    rank_mu_step: float = 0.40
    # No-information seed, and the (lower) sigma of a seed that comes from a rank.
    default_mu: float = 25.0
    default_sigma: float = 25 / 3
    sigma_rank_seed: float = 2.0
    # Soft bounds on how far E may drift from the rank-based estimate R (E - R in [-d_down, d_up]).
    d_down: float = 2.0
    d_up: float = 4.5
    # Below this many confirmed games a rank change re-seeds H; from then on only E moves.
    reseed_below_games: int = 5
    # Riot rank older than this is flagged stale (E is not affected).
    rank_stale_after_days: int = 30
    # Admin diagnostics flag a divergence when |H - R| exceeds this multiple of the bound.
    divergence_factor: float = 1.5
    # Recent form: the average of (won - predicted win chance) over the last `form_games`
    # confirmed games, times `form_weight` rating points, capped at +/- `form_cap`, is added to the
    # matchmaking estimate E only (never to H or the visible tier). 0 turns it off.
    form_games: int = 5
    form_weight: float = 0.0
    form_cap: float = 2.0


@dataclass(frozen=True)
class EngineParams:
    beta: float = 25 / 6
    tau: float = 25 / 300
    # Role-aware update (off): the update runs on each player's strength *on the role they
    # played* (H minus the loss for their proficiency there) and only the change is applied to H,
    # so an autofilled player is not charged for a role effect the balancer already expected.
    role_aware: bool = False
    role_loss: Mapping[int, float] = field(default_factory=_default_role_loss)
    unknown_role_loss: float = DEFAULT_UNKNOWN_PROF_LOSS

    def loss_for(self, proficiency: int | None) -> float:
        """Rating points lost on a role at this proficiency (None = unknown)."""
        return self.unknown_role_loss if proficiency is None else self.role_loss[proficiency]


def algo_version(engine: EngineParams | None = None) -> str:
    """Identifies everything that determines how H evolves from the same inputs.

    The role-aware part is added only when it is on, so shipping it switched off keeps the
    version (and does not replay anything at startup).
    """
    engine = engine or EngineParams()
    text = f"openskill-{version('openskill')}-pl-b{engine.beta:.6g}-t{engine.tau:.6g}"
    if engine.role_aware:
        table = repr((sorted(engine.role_loss.items()), engine.unknown_role_loss))
        text += f"-ra{hashlib.sha1(table.encode()).hexdigest()[:8]}"
    return text
