"""Run the balancer on synthetic players and summarise how the teams land."""

import random
from collections.abc import Sequence
from dataclasses import dataclass

from bot.features.balance.engine import balance
from bot.features.balance.model import BalanceParams, Proposal
from bot.features.players.proficiency import ROLE_NAMES_PT
from bot.features.simulation.metrics import (
    Aggregate,
    ProposalMetrics,
    aggregate,
    evaluate,
)
from bot.features.simulation.players import SimPlayer, random_roster
from bot.features.simulation.scenarios import Scenario


@dataclass(frozen=True)
class Outcome:
    players: list[SimPlayer]
    proposal: Proposal
    metrics: ProposalMetrics


def run_lobby(
    players: Sequence[SimPlayer],
    params: BalanceParams | None = None,
    seed: int = 0,
) -> Outcome:
    """Balance one lobby and evaluate the best proposal against the hidden truth."""
    params = params or BalanceParams()
    result = balance([p.to_input() for p in players], params, random.Random(seed))
    if not result.proposals:
        raise AssertionError(f"no proposal: {result.problems}")
    proposal = result.proposals[0]
    return Outcome(list(players), proposal, evaluate(proposal, players, params))


def run_scenario(
    scenario: Scenario, params: BalanceParams | None = None, seed: int = 0
) -> tuple[Outcome, list[str]]:
    """Outcome plus the scenario's list of problems (empty = as expected)."""
    outcome = run_lobby(scenario.players, params, seed)
    by_id = {p.player_id: p for p in outcome.players}
    return outcome, scenario.check(outcome.proposal, by_id, outcome.metrics)


def run_population(
    lobbies: int,
    style: str = "realistic",
    seed: int = 0,
    params: BalanceParams | None = None,
    estimate_noise: float = 0.0,
) -> tuple[Aggregate, list[Outcome]]:
    """Many random lobbies of one style, summarised. With `estimate_noise` 0 the ratings are
    exactly right, so what is measured is the balancer; raise it to see bad ratings."""
    rng = random.Random(seed)
    outcomes = [
        run_lobby(random_roster(rng, 10, style, estimate_noise), params, rng.randrange(1 << 30))
        for _ in range(lobbies)
    ]
    return aggregate([o.metrics for o in outcomes]), outcomes


# --- text output (used by the CLI and by failing tests, so a failure shows the teams) ----


def format_outcome(outcome: Outcome) -> str:
    by_id = {p.player_id: p for p in outcome.players}
    lines = []
    for label, side in (("AZUL", outcome.proposal.blue), ("VERMELHO", outcome.proposal.red)):
        lines.append(label)
        for slot in side:
            p = by_id[slot.player_id]
            fit = p.proficiency.get(slot.role)
            best = p.best_level
            note = "" if fit == best else f"  (fit {fit}/5, best {best})"
            lines.append(
                f"  {ROLE_NAMES_PT[slot.role]:<8} {p.name:<6} rating {p.mu:5.1f}"
                f" (true {p.true_mu:5.1f}){note}"
            )
    m = outcome.metrics
    lines.append(
        f"predicted blue win {m.p_blue:.2f} | true {m.true_p_blue:.2f} | off best role "
        f"{m.off_best}/10 | avg fit {m.avg_fit:.2f} | lane gap {m.lane_gap:.1f} "
        f"(max {m.max_lane_gap:.1f}) | stars split {'yes' if m.elite_split else 'NO'}"
    )
    return "\n".join(lines)


def format_aggregate(agg: Aggregate) -> str:
    return (
        f"{agg.count} lobbies | mean true edge {agg.mean_true_edge:.3f} "
        f"(max {agg.max_true_edge:.3f}) | lopsided {agg.share_lopsided:.0%} | "
        f"avg fit {agg.mean_avg_fit:.2f} | off best {agg.mean_off_best:.1f}/10 | "
        f"bad-fit players {agg.share_bad_fit_players:.0%} | "
        f"mean lane gap {agg.mean_lane_gap:.1f} | stars split {agg.share_elite_split:.0%}"
    )
