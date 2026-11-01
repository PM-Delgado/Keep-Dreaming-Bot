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
        self,
        winners: Sequence[Rating],
        losers: Sequence[Rating],
        winner_losses: Sequence[float] | None = None,
        loser_losses: Sequence[float] | None = None,
    ) -> tuple[list[Rating], list[Rating]]:
        """New ratings for both teams after `winners` beat `losers` (same order as given).

        `*_losses` (rating points each player lost on the role they played) make the update
        role-aware: the engine sees `mu - loss`, and the change it makes is applied to the
        player's real `mu`. Without them (or with all zeros) this is the plain update.
        """
        win_loss = list(winner_losses) if winner_losses is not None else [0.0] * len(winners)
        lose_loss = list(loser_losses) if loser_losses is not None else [0.0] * len(losers)
        if len(win_loss) != len(winners) or len(lose_loss) != len(losers):
            raise ValueError("One loss per player is needed.")
        seen_w = [Rating(r.mu - loss, r.sigma) for r, loss in zip(winners, win_loss, strict=True)]
        seen_l = [Rating(r.mu - loss, r.sigma) for r, loss in zip(losers, lose_loss, strict=True)]
        new_winners, new_losers = self._model.rate(self._teams(seen_w, seen_l), ranks=[1, 2])
        return (
            [Rating(r.mu + loss, r.sigma) for r, loss in zip(new_winners, win_loss, strict=True)],
            [Rating(r.mu + loss, r.sigma) for r, loss in zip(new_losers, lose_loss, strict=True)],
        )

    def win_probability(self, team_a: Sequence[Rating], team_b: Sequence[Rating]) -> float:
        """Probability that team_a beats team_b."""
        return float(self._model.predict_win(self._teams(team_a, team_b))[0])
