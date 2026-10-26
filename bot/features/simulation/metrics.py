"""How good is a proposal? Judged against the hidden truth, not against the balancer's own view."""

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from bot.features.balance.engine import role_cost, role_strength
from bot.features.balance.model import BalanceParams, Proposal, Slot
from bot.features.players.proficiency import ROLES
from bot.features.simulation.players import SimPlayer

TRUE_LOSS_PER_LEVEL = 0.5  # hidden truth: each proficiency level below 5 costs this many points
UNKNOWN_TRUE_LEVEL = 2.5  # ... and an unknown role is assumed to be about a 2-3
BETA = 25 / 6


def true_role_strength(player: SimPlayer, role: str) -> float:
    """The player's *real* strength on a role. Deliberately a different formula from the
    balancer's loss table, so the balancer is tested against something it did not fit to."""
    level = player.proficiency.get(role)
    level = UNKNOWN_TRUE_LEVEL if level is None else level
    return player.true_mu - TRUE_LOSS_PER_LEVEL * (5 - level)


def _lane_strengths(
    slots: Sequence[Slot], by_id: Mapping[int, SimPlayer], strength
) -> dict[str, float]:
    return {s.role: strength(by_id[s.player_id], s.role) for s in slots}


def true_blue_win_prob(proposal: Proposal, by_id: Mapping[int, SimPlayer]) -> float:
    """P(blue wins) if the hidden truth decided it (what a real game would converge to)."""
    blue = sum(true_role_strength(by_id[s.player_id], s.role) for s in proposal.blue)
    red = sum(true_role_strength(by_id[s.player_id], s.role) for s in proposal.red)
    spread = math.sqrt(10 * BETA**2)
    return 0.5 * (1 + math.erf((blue - red) / spread / math.sqrt(2)))


@dataclass(frozen=True)
class ProposalMetrics:
    p_blue: float  # what the balancer predicted
    true_p_blue: float  # what the hidden truth says
    edge: float  # |p_blue - 0.5|
    true_edge: float  # |true_p_blue - 0.5|
    role_cost: float
    avg_fit: float  # mean proficiency on the assigned role (unknown counts as 2)
    off_best: int  # players not on one of their best roles (of 10)
    bad_fit: int  # players on a role they know at 2 or less (or not at all)
    lane_gap: float  # sum over roles of the believed strength gap between opponents
    max_lane_gap: float
    true_lane_gap: float  # same, with the hidden truth
    elite_split: bool  # the three strongest players are not all on one team
    star_gap: float  # difference between the teams' top-2 role strengths


def evaluate(
    proposal: Proposal, players: Sequence[SimPlayer], params: BalanceParams | None = None
) -> ProposalMetrics:
    params = params or BalanceParams()
    by_id = {p.player_id: p for p in players}
    everyone = (*proposal.blue, *proposal.red)

    fits, off_best, bad_fit, cost = [], 0, 0, 0.0
    for slot in everyone:
        player = by_id[slot.player_id]
        fit = player.proficiency.get(slot.role)
        fits.append(2 if fit is None else fit)
        if player.best_level is not None and (fit is None or fit < player.best_level):
            off_best += 1
        if fit is None or fit <= 2:
            bad_fit += 1
        cost += role_cost(player.to_input(), slot.role, params)

    def believed(player: SimPlayer, role: str) -> float:
        return role_strength(player.to_input(), role, params)

    blue_b = _lane_strengths(proposal.blue, by_id, believed)
    red_b = _lane_strengths(proposal.red, by_id, believed)
    blue_t = _lane_strengths(proposal.blue, by_id, true_role_strength)
    red_t = _lane_strengths(proposal.red, by_id, true_role_strength)
    gaps = [abs(blue_b[r] - red_b[r]) for r in ROLES]

    top3 = sorted(players, key=lambda p: (-p.mu, p.player_id))[:3]
    blue_ids = {s.player_id for s in proposal.blue}
    on_blue = sum(p.player_id in blue_ids for p in top3)
    true_p = true_blue_win_prob(proposal, by_id)

    return ProposalMetrics(
        p_blue=proposal.blue_win_prob,
        true_p_blue=true_p,
        edge=abs(proposal.blue_win_prob - 0.5),
        true_edge=abs(true_p - 0.5),
        role_cost=cost,
        avg_fit=statistics.fmean(fits),
        off_best=off_best,
        bad_fit=bad_fit,
        lane_gap=sum(gaps),
        max_lane_gap=max(gaps),
        true_lane_gap=sum(abs(blue_t[r] - red_t[r]) for r in ROLES),
        elite_split=0 < on_blue < len(top3),
        star_gap=abs(
            sum(sorted(blue_b.values(), reverse=True)[:2])
            - sum(sorted(red_b.values(), reverse=True)[:2])
        ),
    )


@dataclass(frozen=True)
class Aggregate:
    """Summary of many proposals (a population run or a league)."""

    count: int
    mean_true_edge: float
    max_true_edge: float
    share_lopsided: float  # share of games whose true edge is 15 points or more
    mean_avg_fit: float
    mean_off_best: float
    share_bad_fit_players: float  # of all players, how many are on a 2-or-less role
    mean_lane_gap: float
    share_elite_split: float


def aggregate(metrics: Sequence[ProposalMetrics]) -> Aggregate:
    n = len(metrics)
    if n == 0:
        raise ValueError("Nothing to aggregate")
    return Aggregate(
        count=n,
        mean_true_edge=statistics.fmean(m.true_edge for m in metrics),
        max_true_edge=max(m.true_edge for m in metrics),
        share_lopsided=sum(m.true_edge >= 0.15 for m in metrics) / n,
        mean_avg_fit=statistics.fmean(m.avg_fit for m in metrics),
        mean_off_best=statistics.fmean(m.off_best for m in metrics),
        share_bad_fit_players=sum(m.bad_fit for m in metrics) / (10 * n),
        mean_lane_gap=statistics.fmean(m.lane_gap for m in metrics),
        share_elite_split=sum(m.elite_split for m in metrics) / n,
    )
