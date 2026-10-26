"""Persistent role proficiency: what a player can do, separate from per-lobby preference."""

from dataclasses import dataclass

ROLES: tuple[str, ...] = ("TOP", "JUNGLE", "MID", "ADC", "SUPPORT")
ROLE_NAMES_PT = {
    "TOP": "Top",
    "JUNGLE": "Jungle",
    "MID": "Mid",
    "ADC": "ADC",
    "SUPPORT": "Suporte",
}

# Riot ranked history can suggest at most this; 5 needs a declaration, an admin or in-house proof.
MAX_ESTIMATED = 4


@dataclass(frozen=True)
class EffectiveProficiency:
    value: int | None  # 1..5, None = unknown
    source: str  # admin | declared | estimated | unknown


def effective_proficiency(
    declared: int | None, admin_override: int | None, estimated: int | None = None
) -> EffectiveProficiency:
    """Precedence: admin override > player declaration > estimate (capped) > unknown."""
    if admin_override is not None:
        return EffectiveProficiency(admin_override, "admin")
    if declared is not None:
        return EffectiveProficiency(declared, "declared")
    if estimated is not None:
        return EffectiveProficiency(min(estimated, MAX_ESTIMATED), "estimated")
    return EffectiveProficiency(None, "unknown")
