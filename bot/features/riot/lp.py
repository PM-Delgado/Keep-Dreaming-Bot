"""League Points on one straight scale, so a rank change is a plain subtraction. Pure.

Iron IV 0 LP is 0; every division is 100 LP wide, so Diamond I 75 LP is 2775. Master, Grandmaster
and Challenger share one continuous scale above Diamond I (Riot separates them by LP cut-offs, not
by divisions): Master 0 LP is 2800 and a Master player with 1034 LP is 3834, whatever the tier
name says. A promotion from Diamond I 90 LP to Master 10 LP is therefore +20, as it should be.
"""

from bot.features.ratings.rank import APEX_POINTS, rank_points
from bot.features.riot.client import SoloRank

DIVISION_WIDTH = 100
APEX_BASE = APEX_POINTS["MASTER"] * DIVISION_WIDTH


def ladder_points(tier: str, division: str | None, lp: int) -> int:
    """Position on the straight LP scale. Raises ValueError for a tier or division we don't know."""
    tier = tier.strip().upper()
    if tier in APEX_POINTS:
        return APEX_BASE + lp
    return rank_points(tier, division) * DIVISION_WIDTH + lp


def rank_ladder_points(rank: SoloRank) -> int:
    return ladder_points(rank.tier, rank.division, rank.lp)


def lp_delta(before: SoloRank | None, after: SoloRank | None) -> int | None:
    """LP gained (positive) or lost, or None when either rank is missing (unranked, unknown)."""
    if before is None or after is None:
        return None
    return rank_ladder_points(after) - rank_ladder_points(before)


def rank_label(tier: str, division: str | None, lp: int) -> str:
    """`GRANDMASTER · 1804 LP`, `DIAMOND I · 75 LP`: tier (and division below Master), then LP."""
    tier = tier.strip().upper()
    name = tier if tier in APEX_POINTS or not division else f"{tier} {division.upper()}"
    return f"{name} · {lp} LP"
