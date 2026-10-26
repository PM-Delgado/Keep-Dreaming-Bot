"""Thin wrapper around openskill; the only module that imports it."""

from collections.abc import Sequence
from dataclasses import dataclass

from openskill.models import PlackettLuce

from bot.features.ratings.params import EngineParams


@dataclass(frozen=True)
class Rating:
    mu: float
    sigma: float


class RatingEngine:
    def __init__(self, params: EngineParams | None = None) -> None:
        self.params = params or EngineParams()
        self._model = PlackettLuce(beta=self.params.beta, tau=self.params.tau)

    def _teams(self, *teams: Sequence[Rating]):
        return [[self._model.rating(mu=r.mu, sigma=r.sigma) for r in team] for team in teams]

    def update(
        self, winners: Sequence[Rating], losers: Sequence[Rating]
    ) -> tuple[list[Rating], list[Rating]]:
        """New ratings for both teams after `winners` beat `losers` (same order as given)."""
        new_winners, new_losers = self._model.rate(self._teams(winners, losers), ranks=[1, 2])
        return (
            [Rating(r.mu, r.sigma) for r in new_winners],
            [Rating(r.mu, r.sigma) for r in new_losers],
        )

    def win_probability(self, team_a: Sequence[Rating], team_b: Sequence[Rating]) -> float:
        """Probability that team_a beats team_b."""
        return float(self._model.predict_win(self._teams(team_a, team_b))[0])
