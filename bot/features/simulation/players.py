"""Synthetic players for matchmaking tests and simulations. Pure: no Discord, no database.

A `SimPlayer` has two kinds of skill. `mu`/`sigma` are what the bot *believes* (the matchmaking
estimate E that the balancer sees); `true_mu` is the hidden truth that only the simulation uses to
judge outcomes, so a test can ask "was this split really fair?" without the balancer grading its
own homework.
"""

import random
from collections.abc import Mapping
from dataclasses import dataclass, field

from bot.features.balance.model import PlayerInput
from bot.features.players.proficiency import ROLES


@dataclass(frozen=True)
class SimPlayer:
    player_id: int
    name: str
    mu: float  # believed strength (matchmaking estimate)
    sigma: float
    true_mu: float  # hidden true strength
    proficiency: Mapping[str, int | None] = field(default_factory=dict)  # role -> 1..5 | None
    rank: tuple[str, str | None] | None = None  # Riot (tier, division), when the bot knows one

    def to_input(self) -> PlayerInput:
        return PlayerInput(self.player_id, self.mu, self.sigma, dict(self.proficiency))

    @property
    def best_level(self) -> int | None:
        known = [v for v in self.proficiency.values() if v is not None]
        return max(known) if known else None

    @property
    def best_roles(self) -> list[str]:
        best = self.best_level
        return [r for r in ROLES if best is not None and self.proficiency.get(r) == best]


def make_player(
    player_id: int,
    mu: float,
    main: str | None = None,
    second: str | None = None,
    third: str | None = None,
    rest: int | None = 1,
    sigma: float = 5.0,
    true_mu: float | None = None,
    name: str | None = None,
) -> SimPlayer:
    """A player with a main role (5), optional second (4) and third (3); other roles at `rest`.

    `rest=None` makes the remaining roles unknown (no data), `main=None` makes every role `rest`.
    """
    proficiency: dict[str, int | None] = dict.fromkeys(ROLES, rest)
    if main:
        proficiency[main] = 5
    if second:
        proficiency[second] = 4
    if third:
        proficiency[third] = 3
    return SimPlayer(
        player_id,
        name or f"P{player_id:02d}",
        mu,
        sigma,
        mu if true_mu is None else true_mu,
        proficiency,
    )


# --- random populations ------------------------------------------------------------------------

STYLES = ("realistic", "role_stack", "wide", "unknown_heavy")


def random_player(
    rng: random.Random, player_id: int, style: str = "realistic", estimate_noise: float = 0.0
) -> SimPlayer:
    """One random player. Styles:

    * realistic: ratings around 25 (sd 3), a main role, often a second and a third;
    * role_stack: like realistic, but about half the players main the same popular role;
    * wide: ratings spread uniformly between 15 and 35;
    * unknown_heavy: many roles have no data at all.

    `estimate_noise` is how far the bot's rating is from the truth (standard deviation, in rating
    points). 0 isolates the balancer; real ratings are noisy, which no balancer can fix.
    """
    if style not in STYLES:
        raise ValueError(f"Unknown style {style!r}")
    mu = rng.uniform(15, 35) if style == "wide" else min(35.0, max(15.0, rng.gauss(25, 3)))
    true_mu = mu + (rng.gauss(0, estimate_noise) if estimate_noise else 0.0)
    main = "JUNGLE" if style == "role_stack" and rng.random() < 0.5 else rng.choice(ROLES)
    others = [r for r in ROLES if r != main]
    rng.shuffle(others)
    second = others[0] if rng.random() < 0.6 else None
    third = others[1] if second and rng.random() < 0.5 else None
    unknown_chance = 0.6 if style == "unknown_heavy" else 0.15
    proficiency: dict[str, int | None] = {}
    for role in ROLES:
        if role == main:
            proficiency[role] = 5
        elif role == second:
            proficiency[role] = 4
        elif role == third:
            proficiency[role] = 3
        else:
            proficiency[role] = None if rng.random() < unknown_chance else rng.choice([1, 2])
    return SimPlayer(player_id, f"P{player_id:02d}", mu, rng.uniform(3, 7), true_mu, proficiency)


def random_roster(
    rng: random.Random, size: int = 10, style: str = "realistic", estimate_noise: float = 0.0
) -> list[SimPlayer]:
    return [random_player(rng, i, style, estimate_noise) for i in range(1, size + 1)]
