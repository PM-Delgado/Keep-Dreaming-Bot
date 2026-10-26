"""League rank -> rating scale (R), rank-signal resolution and freshness. Pure functions."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from bot.features.ratings.params import MatchmakingParams

# Riot returns tiers upper-case and the division ("rank") as a roman numeral.
TIERS = ("IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND")
DIVISIONS = ("IV", "III", "II", "I")  # lowest -> highest
# Apex tiers have no divisions; division and LP are ignored in v1.
APEX_POINTS = {"MASTER": 28, "GRANDMASTER": 32, "CHALLENGER": 36}
ALL_TIERS = (*TIERS, *APEX_POINTS)


def rank_points(tier: str, division: str | None = None) -> int:
    """Ladder position: Iron IV = 0 ... Diamond I = 27, then Master 28 / GM 32 / Challenger 36."""
    tier = tier.strip().upper()
    if tier in APEX_POINTS:
        return APEX_POINTS[tier]
    if tier not in TIERS:
        raise ValueError(f"Unknown tier: {tier!r}")
    if division is None or division.strip().upper() not in DIVISIONS:
        raise ValueError(f"Unknown division for {tier}: {division!r}")
    return TIERS.index(tier) * 4 + DIVISIONS.index(division.strip().upper())


def rank_mu(tier: str, division: str | None, params: MatchmakingParams) -> float:
    return params.rank_mu_base + params.rank_mu_step * rank_points(tier, division)


@dataclass(frozen=True)
class RankSignal:
    """One row of `rank_signals`: an authoritative input for R."""

    id: int
    source: str  # riot | self_reported | admin
    status: str  # ranked | unranked | cleared
    created_at: datetime
    account_key: str | None = None  # puuid, riot source only
    tier: str | None = None
    division: str | None = None


@dataclass(frozen=True)
class ResolvedRank:
    source: str
    tier: str
    division: str | None
    as_of: datetime

    def mu(self, params: MatchmakingParams) -> float:
        return rank_mu(self.tier, self.division, params)

    def label(self) -> str:
        tier = self.tier.capitalize()
        return tier if self.tier.upper() in APEX_POINTS else f"{tier} {self.division}"


@dataclass(frozen=True)
class RankResolution:
    rank: ResolvedRank | None
    # A designated main exists but has never been successfully refreshed.
    riot_pending: bool
    # Latest successful Riot refresh of the main (any status); None if never.
    riot_last_refreshed: datetime | None


def resolve_rank(signals: Iterable[RankSignal], main_puuid: str | None) -> RankResolution:
    """Resolve the current rank-based estimate input for one player.

    Order (first hit wins): admin override > Riot signal of the designated main > self-reported >
    nothing. Signals of any other Riot account are never consulted, so changing the main cannot
    leave the previous account's rank active. A Riot `unranked` signal supersedes earlier ranked
    ones and falls through to self-reported. Failed refreshes write no signal, so they never get
    here at all.
    """
    ordered = sorted(signals, key=lambda s: (s.created_at, s.id))

    riot_for_main = [s for s in ordered if s.source == "riot" and s.account_key == main_puuid]
    last_riot = riot_for_main[-1] if main_puuid is not None and riot_for_main else None
    pending = main_puuid is not None and last_riot is None
    last_refreshed = last_riot.created_at if last_riot else None

    def result(signal: RankSignal | None) -> RankResolution:
        rank = None
        if signal is not None and signal.tier is not None:
            rank = ResolvedRank(signal.source, signal.tier, signal.division, signal.created_at)
        return RankResolution(rank, pending, last_refreshed)

    admin = [s for s in ordered if s.source == "admin"]
    if admin and admin[-1].status == "ranked":
        return result(admin[-1])

    if last_riot is not None and last_riot.status == "ranked":
        return result(last_riot)

    self_reported = [s for s in ordered if s.source == "self_reported" and s.status == "ranked"]
    return result(self_reported[-1] if self_reported else None)


def rank_age_days(rank: ResolvedRank, now: datetime) -> float:
    return (now - rank.as_of).total_seconds() / 86400


def is_stale(rank: ResolvedRank, now: datetime, params: MatchmakingParams) -> bool:
    """Only Riot-sourced ranks can be stale; manual ones show their age but are not refreshed."""
    return rank.source == "riot" and rank_age_days(rank, now) >= params.rank_stale_after_days
