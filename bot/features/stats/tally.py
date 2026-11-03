"""A win/loss record. Its own module so the service and the pair rules can both use it."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tally:
    wins: int = 0
    losses: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games if self.games else None
