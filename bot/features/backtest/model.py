"""Data types for the backtest. Pure: no Discord, no database."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from bot.features.ratings.params import EngineParams
from bot.features.ratings.replay import RatingEventIn


@dataclass(frozen=True)
class GamePlayer:
    player_id: int
    side: str  # blue | red
    role: str  # the role the balancer assigned
    rank_mu: float | None  # R frozen when the game started
    # Role -> proficiency (None = unknown) when the game started; None = nothing was recorded.
    proficiency: Mapping[str, int | None] | None


@dataclass(frozen=True)
class Game:
    id: int
    confirmed_at: datetime
    winner: str  # blue | red
    players: tuple[GamePlayer, ...]


@dataclass(frozen=True)
class Dataset:
    events: list[RatingEventIn]
    games: list[Game]  # confirmed games only
    names: Mapping[int, str]
    stored_mu: Mapping[int, float]  # H as the bot has it now (the cache), to check the replay


@dataclass(frozen=True)
class Variant:
    """One way the system could have worked. Every field defaults to today's behaviour."""

    name: str
    loss_scale: float = 1.0  # multiplies the role-loss table (prediction and, if on, the update)
    role_aware: bool = EngineParams().role_aware  # role-aware rating update (the bot's default)
    sigma_floor: float = 0.0  # the uncertainty never drops below this after a game
    d_up: float | None = None  # bounds of E around the rank; None = the bot's default
    d_down: float | None = None


@dataclass(frozen=True)
class GameScore:
    game_id: int
    p_blue: float  # predicted from the state just before the game
    blue_won: bool

    @property
    def p_actual(self) -> float:
        return self.p_blue if self.blue_won else 1 - self.p_blue


@dataclass(frozen=True)
class Final:
    mu: float
    games: int
    label: str  # visible tier, "-" while still in placement


@dataclass(frozen=True)
class VariantResult:
    variant: Variant
    scores: list[GameScore]
    final: Mapping[int, Final]
    unrecorded: int = 0  # players (over all games) whose role profile was not recorded
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Paired:
    """The per-game difference in log-likelihood between a variant and the baseline."""

    mean_gain: float  # positive = the variant predicted the real results better
    stderr: float | None
    better: int  # games where the variant did better
    games: int
