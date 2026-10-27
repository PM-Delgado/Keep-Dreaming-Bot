"""Visible in-house ladder: tier/division derived from the hidden in-house rating H.

This is standing *within the group*, not a League rank. Everything presentational (names,
floors, divisions, placement, hysteresis) lives in `LadderConfig`; stored data only ever holds
the stable `tier_key`, so renaming or retuning never touches authoritative history.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

ROMAN = {1: "I", 2: "II", 3: "III"}


@dataclass(frozen=True)
class TierConfig:
    key: str
    names: Mapping[str, str]
    floor: float  # mu_H at which the tier's lowest division starts
    divisions: int  # 0 = a single undivided step (the top tier)


@dataclass(frozen=True)
class Step:
    tier_key: str
    division: int | None  # 3 = lowest ... 1 = highest, None for an undivided tier
    floor: float


@dataclass(frozen=True)
class LadderConfig:
    version: int
    tiers: tuple[TierConfig, ...]
    division_width: float = 1.0
    demotion_buffer: float = 0.33
    placement_games: int = 5
    steps: tuple[Step, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        steps: list[Step] = []
        for tier in self.tiers:
            if tier.divisions == 0:
                steps.append(Step(tier.key, None, tier.floor))
                continue
            for i in range(tier.divisions):
                steps.append(
                    Step(tier.key, tier.divisions - i, tier.floor + i * self.division_width)
                )
        if any(b.floor <= a.floor for a, b in zip(steps, steps[1:], strict=False)):
            raise ValueError("Ladder steps must be strictly ascending.")
        object.__setattr__(self, "steps", tuple(steps))

    def display_name(self, tier_key: str, locale: str = "pt") -> str:
        for tier in self.tiers:
            if tier.key == tier_key:
                return tier.names.get(locale) or next(iter(tier.names.values()))
        return tier_key  # unknown key (e.g. tier removed from config): show the stored key

    def label(self, tier_key: str, division: int | None, locale: str = "pt") -> str:
        name = self.display_name(tier_key, locale)
        return name if division is None else f"{name} {ROMAN[division]}"


def _tier(key: str, name: str, floor: float, divisions: int = 3) -> TierConfig:
    return TierConfig(key, MappingProxyType({"pt": name}), floor, divisions)


# Provisional, uncalibrated thresholds (see tools/ladder_sim.py to inspect the distribution).
DEFAULT_LADDER = LadderConfig(
    version=1,
    tiers=(
        _tier("bot_maximo", "Bot Máximo", 18.0),
        _tier("rush_caixa", "Rush Caixa", 21.0),
        _tier("duro_rijo", "Duro que nem um rijo", 24.0),
        _tier("no_box", "No Box", 27.0),
        _tier("kd_esports", "KD esports", 30.0, divisions=0),
    ),
)


@dataclass(frozen=True)
class Standing:
    games: int
    placed: bool
    tier_key: str | None = None
    division: int | None = None
    progress: float | None = None  # 0..1 inside the current step, None for the top step


def _step_index(ladder: LadderConfig, standing: Standing | None) -> int | None:
    if standing is None or not standing.placed:
        return None
    for i, step in enumerate(ladder.steps):
        if step.tier_key == standing.tier_key and step.division == standing.division:
            return i
    return None  # previous tier/division no longer exists in this ladder version


def step_rank(ladder: LadderConfig, tier_key: str | None, division: int | None) -> int | None:
    """Position of a stored tier/division on the ladder (higher = better), None if unknown."""
    return _step_index(ladder, Standing(0, True, tier_key, division))


def step_progress(
    ladder: LadderConfig, tier_key: str | None, division: int | None, mu: float
) -> float | None:
    """0..1 progress of mu inside a stored tier/division (None for the top step or unknown keys)."""
    index = _step_index(ladder, Standing(0, True, tier_key, division))
    if index is None or index + 1 >= len(ladder.steps):
        return None
    step = ladder.steps[index]
    span = ladder.steps[index + 1].floor - step.floor
    return min(1.0, max(0.0, (mu - step.floor) / span))


def derive_standing(
    mu: float, games: int, previous: Standing | None, ladder: LadderConfig = DEFAULT_LADDER
) -> Standing:
    """Visible standing from mu_H. Promotion at a step's floor; demotion only once mu falls
    `demotion_buffer` below the current step's floor. `previous=None` skips the hysteresis
    (first placement, seeds and admin adjustments)."""
    if games < ladder.placement_games:
        return Standing(games=games, placed=False)

    steps = ladder.steps
    raw = max((i for i, s in enumerate(steps) if s.floor <= mu), default=0)
    current = _step_index(ladder, previous)
    if (
        current is None
        or raw > current
        or raw < current
        and mu < steps[current].floor - ladder.demotion_buffer
    ):
        index = raw
    else:
        index = current

    step = steps[index]
    progress = None
    if index + 1 < len(steps):
        span = steps[index + 1].floor - step.floor
        progress = min(1.0, max(0.0, (mu - step.floor) / span))
    return Standing(games, True, step.tier_key, step.division, progress)
