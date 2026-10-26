"""Role experience and estimated proficiency from ranked Solo/Duo history. Pure functions.

Layers (see the design): role *experience* is a fact (recency-weighted games per role), the
*estimated proficiency* is a hypothesis derived from it (capped below "strong"), and neither is
actual in-house performance. Missing or ambiguous position data lowers confidence.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.features.players.proficiency import ROLES

# Riot's teamPosition values -> our roles. Anything else (empty, "Invalid", ...) is unknown.
ROLE_FROM_POSITION = {
    "TOP": "TOP",
    "JUNGLE": "JUNGLE",
    "MIDDLE": "MID",
    "BOTTOM": "ADC",
    "UTILITY": "SUPPORT",
}

RANKED_SOLO_QUEUE_ID = 420


@dataclass(frozen=True)
class RoleHistoryParams:
    half_life_days: float = 90.0
    window_days: int = 365
    max_games_per_account: int = 100
    min_duration_s: int = 300  # shorter games are remakes
    k: float = 10.0  # confidence = n_q / (n_all + k)
    c_min: float = 0.3  # below this the profile is "unknown", not "low"
    # (weighted games on the role, estimated proficiency), highest first
    thresholds: tuple[tuple[float, int], ...] = ((20.0, 4), (8.0, 3), (3.0, 2))


@dataclass(frozen=True)
class GameObservation:
    played_at: datetime
    team_position: str | None
    individual_position: str | None
    duration_s: int
    early_surrender: bool = False


@dataclass(frozen=True)
class RoleExperience:
    by_role: Mapping[str, float]  # sum of w * q per role
    n_all: float  # sum of w over eligible games
    n_q: float  # sum of w * q
    coverage: float  # n_q / n_all
    confidence: float  # n_q / (n_all + k)


def position_role(position: str | None) -> str | None:
    return ROLE_FROM_POSITION.get((position or "").strip().upper())


def game_quality(game: GameObservation) -> tuple[str | None, float]:
    """(role, q): 1.0 known, 0.5 ambiguous (team and individual position disagree), 0 unknown."""
    role = position_role(game.team_position)
    if role is None:
        return None, 0.0
    individual = position_role(game.individual_position)
    if individual is not None and individual != role:
        return role, 0.5
    return role, 1.0


def recency_weight(age_days: float, half_life_days: float = 90.0) -> float:
    return 0.5 ** (max(age_days, 0.0) / half_life_days)


def confidence_from_weights(n_all: float, n_q: float, k: float = 10.0) -> float:
    """Confidence that the role picture is right: more games help, missing/ambiguous data hurts.

    Equal to coverage * n_all / (n_all + k), so unknown positions lower it directly.
    """
    return n_q / (n_all + k)


def is_eligible(game: GameObservation, now: datetime, params: RoleHistoryParams) -> bool:
    if game.early_surrender or game.duration_s < params.min_duration_s:
        return False
    return now - game.played_at <= timedelta(days=params.window_days)


def role_experience(
    games: Iterable[GameObservation],
    now: datetime,
    params: RoleHistoryParams | None = None,
    weight=recency_weight,
) -> RoleExperience:
    """`weight(age_days, half_life)` is injectable so unit weights can test the formula alone."""
    params = params or RoleHistoryParams()
    by_role = dict.fromkeys(ROLES, 0.0)
    n_all = n_q = 0.0
    for game in games:
        if not is_eligible(game, now, params):
            continue
        age_days = (now - game.played_at).total_seconds() / 86400
        w = weight(age_days, params.half_life_days)
        role, q = game_quality(game)
        n_all += w
        n_q += w * q
        if role is not None:
            by_role[role] += w * q
    coverage = n_q / n_all if n_all else 0.0
    return RoleExperience(
        by_role, n_all, n_q, coverage, confidence_from_weights(n_all, n_q, params.k)
    )


def estimate_proficiency(
    experience: RoleExperience, params: RoleHistoryParams | None = None
) -> dict[str, int | None]:
    """1..4 per role, or None (unknown) when the history is too thin or too unclear.

    Zero ranked games on a role with a confident profile is "1" (little ranked experience, may
    still be fine), never "can't play": refusing a role is a per-lobby preference.
    """
    params = params or RoleHistoryParams()
    if experience.confidence < params.c_min:
        return dict.fromkeys(ROLES)
    result: dict[str, int | None] = {}
    for role in ROLES:
        value = experience.by_role.get(role, 0.0)
        result[role] = next((level for floor, level in params.thresholds if value >= floor), 1)
    return result
