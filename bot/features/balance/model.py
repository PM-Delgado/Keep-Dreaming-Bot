"""Data types and parameters for team balancing. Pure: no Discord, no database."""

from collections.abc import Mapping
from dataclasses import dataclass, field


def _default_prof_cost() -> dict[int, float]:
    # How unhappy a player is on a role, by their proficiency there (1..5).
    return {5: 0.0, 4: 0.5, 3: 1.0, 2: 2.5, 1: 4.0}


def _default_prof_loss() -> dict[int, float]:
    # Rating points a player loses on a role they know less well (their strength *there*).
    return {5: 0.0, 4: 0.3, 3: 0.8, 2: 1.8, 1: 3.0}


@dataclass(frozen=True)
class BalanceParams:
    """Provisional weights; every one is meant to be tuned after real lobbies."""

    beta: float = 25 / 6  # same as the rating engine, for the win-probability estimate
    prof_cost: Mapping[int, float] = field(default_factory=_default_prof_cost)
    unknown_prof_cost: float = 1.5  # slightly worse than "3": unknown is not free
    prof_loss: Mapping[int, float] = field(default_factory=_default_prof_loss)
    unknown_prof_loss: float = 1.0
    # score = w_prob*|P-0.5| + w_role*sum(role cost) + w_lane*sum(lane gaps)
    #         + w_topk*top-k gap + w_spread*spread gap
    #         + elite_penalty if the strongest players are all on one team
    w_prob: float = 1.0
    w_role: float = 0.015
    w_lane: float = 0.004  # per rating point of difference between opponents in the same role
    w_topk: float = 0.005  # per rating point of difference between the teams' top-k sums
    topk: int = 2
    w_spread: float = 0.01  # per rating point of difference in the teams' spread
    elite_count: int = 3
    elite_penalty: float = 0.10
    lane_rounds: int = 4  # best-response rounds when matching lanes
    tie_epsilon: float = 0.002  # proposals this close to the best are shuffled for variety
    max_proposals: int = 3


@dataclass(frozen=True)
class PlayerInput:
    player_id: int
    mu: float  # the matchmaking estimate E (never the raw in-house rating)
    sigma: float
    proficiency: Mapping[str, int | None] = field(default_factory=dict)  # role -> 1..5 | None


@dataclass(frozen=True)
class Slot:
    player_id: int
    role: str


@dataclass(frozen=True)
class Proposal:
    blue: tuple[Slot, ...]  # in role order (TOP, JUNGLE, MID, ADC, SUPPORT)
    red: tuple[Slot, ...]
    blue_win_prob: float
    role_cost: float
    score: float

    @property
    def partition(self) -> frozenset[frozenset[int]]:
        """The unordered split, ignoring sides and roles."""
        return frozenset(
            (frozenset(s.player_id for s in self.blue), frozenset(s.player_id for s in self.red))
        )

    def to_dict(self) -> dict:
        return {
            "blue": [[s.player_id, s.role] for s in self.blue],
            "red": [[s.player_id, s.role] for s in self.red],
            "p": self.blue_win_prob,
            "cost": self.role_cost,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "Proposal":
        return cls(
            blue=tuple(Slot(int(p), str(r)) for p, r in data["blue"]),
            red=tuple(Slot(int(p), str(r)) for p, r in data["red"]),
            blue_win_prob=float(data["p"]),
            role_cost=float(data["cost"]),
            score=float(data["score"]),
        )


@dataclass(frozen=True)
class Problem:
    kind: str  # wrong_player_count
    detail: str


@dataclass(frozen=True)
class BalanceResult:
    proposals: list[Proposal]
    problems: list[Problem]


# Coarse labels so players never see the raw probability.
BAND_EVEN, BAND_SLIGHT, BAND_CLEAR, BAND_LOPSIDED = "even", "slight", "clear", "lopsided"


def balance_band(blue_win_prob: float) -> str:
    edge = abs(blue_win_prob - 0.5)
    if edge < 0.03:
        return BAND_EVEN
    if edge < 0.07:
        return BAND_SLIGHT
    if edge < 0.15:
        return BAND_CLEAR
    return BAND_LOPSIDED
