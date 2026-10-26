"""Named matchmaking scenarios: a roster plus what a good outcome looks like.

Each scenario's `check` returns a list of problems (empty = the outcome is what we want). They
encode the rules agreed for the matchmaker, so they double as regression tests and as a catalogue
to browse with `python -m bot.features.simulation scenario <name>`. Add a scenario whenever a
real lobby surprises you.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from bot.features.balance.model import Proposal
from bot.features.simulation.metrics import ProposalMetrics
from bot.features.simulation.players import SimPlayer, make_player

Check = Callable[[Proposal, Mapping[int, SimPlayer], ProposalMetrics], list[str]]


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    players: list[SimPlayer]
    check: Check


def role_of(proposal: Proposal, player_id: int) -> str:
    return next(s.role for s in (*proposal.blue, *proposal.red) if s.player_id == player_id)


def side_of(proposal: Proposal, player_id: int) -> str:
    return "blue" if player_id in {s.player_id for s in proposal.blue} else "red"


def players_in(proposal: Proposal, role: str) -> list[int]:
    return [s.player_id for s in (*proposal.blue, *proposal.red) if s.role == role]


# --- checks ----


def _everyone_on_best(p: Proposal, by_id, m: ProposalMetrics) -> list[str]:
    problems = []
    if m.off_best:
        problems.append(f"{m.off_best} player(s) are not on their best role")
    if m.true_edge > 0.10:
        problems.append(f"the game is lopsided (true edge {m.true_edge:.2f})")
    return problems


def _one_per_team(role: str, ids: list[int], min_fit: int | None = None) -> Check:
    def check(p: Proposal, by_id, m: ProposalMetrics) -> list[str]:
        problems = []
        holders = players_in(p, role)
        if len(holders) != 2 or {side_of(p, h) for h in holders} != {"blue", "red"}:
            problems.append(f"{role} should be played once per team, got {holders}")
        if min_fit is not None:
            for pid in ids:
                fit = by_id[pid].proficiency.get(role_of(p, pid))
                if role_of(p, pid) != role and (fit is None or fit < min_fit):
                    problems.append(f"{by_id[pid].name} was moved to a role they know at {fit}")
        return problems

    return check


def _masters_meet(ids: list[int], role: str) -> Check:
    def check(p: Proposal, by_id, m: ProposalMetrics) -> list[str]:
        problems = []
        if not all(role_of(p, i) == role for i in ids):
            problems.append(f"the strongest players should both play {role}")
        if len({side_of(p, i) for i in ids}) != 2:
            problems.append("the strongest players should be on opposite teams")
        return problems

    return check


def _balanced_and_split(max_edge: float) -> Check:
    def check(p: Proposal, by_id, m: ProposalMetrics) -> list[str]:
        problems = []
        if not m.elite_split:
            problems.append("the three strongest players are all on one team")
        if m.true_edge > max_edge:
            problems.append(f"true edge {m.true_edge:.2f} is above {max_edge:.2f}")
        return problems

    return check


def _valid_and_fair(max_edge: float) -> Check:
    def check(p: Proposal, by_id, m: ProposalMetrics) -> list[str]:
        return (
            [f"true edge {m.true_edge:.2f} is above {max_edge:.2f}"]
            if m.true_edge > max_edge
            else []
        )

    return check


def _scarce_role(player_id: int, role: str) -> Check:
    def check(p: Proposal, by_id, m: ProposalMetrics) -> list[str]:
        problems = []
        if role_of(p, player_id) != role:
            problems.append(f"the only {role} player was not put on {role}")
        if m.true_edge > 0.12:
            problems.append(f"true edge {m.true_edge:.2f} is above 0.12")
        return problems

    return check


def _stars_apart(ids: list[int], role: str) -> Check:
    def check(p: Proposal, by_id, m: ProposalMetrics) -> list[str]:
        problems = []
        roles = {role_of(p, i) for i in ids}
        sides = {side_of(p, i) for i in ids}
        if len(sides) == 1:
            problems.append("the two strongest players are on the same team")
        elif len(roles) == 1 and role_of(p, ids[0]) != role:
            problems.append("they face each other but off their shared main role")
        return problems

    return check


# --- the catalogue ----


def build_scenarios() -> dict[str, Scenario]:
    roles = ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"]
    scenarios: list[Scenario] = []

    def add(name: str, description: str, players: list[SimPlayer], check: Check) -> None:
        scenarios.append(Scenario(name, description, players, check))

    add(
        "balanced_mains",
        "Two players per main role, ratings spread 22-29: everyone should get their main.",
        [
            make_player(i, mu, main=roles[(i - 1) % 5], rest=2, true_mu=mu)
            for i, mu in enumerate([22, 24, 26, 28, 23, 25, 27, 29, 24, 26], start=1)
        ],
        _everyone_on_best,
    )

    seconds = {1: "TOP", 2: "MID", 3: "ADC", 4: "SUPPORT", 5: "TOP"}
    add(
        "five_junglers",
        "Five players main jungle: one per team plays it, the others go to their second role.",
        [make_player(i, 25, main="JUNGLE", second=seconds[i]) for i in range(1, 6)]
        + [
            make_player(6, 25, main="TOP"),
            make_player(7, 25, main="MID"),
            make_player(8, 25, main="ADC"),
            make_player(9, 25, main="SUPPORT"),
            make_player(10, 25, main="TOP", second="MID"),
        ],
        _one_per_team("JUNGLE", [1, 2, 3, 4, 5], min_fit=4),
    )

    add(
        "four_junglers",
        "Four junglers: two keep the role, two move to their second-best.",
        [
            make_player(i, 25, main="JUNGLE", second=r)
            for i, r in enumerate(roles[:1] + roles[2:], 1)
        ]
        + [make_player(i, 25, main=roles[i % 5]) for i in range(5, 11)],
        _one_per_team("JUNGLE", [1, 2, 3, 4], min_fit=4),
    )

    add(
        "all_mid",
        "Everyone mains mid but has a decent second and third role.",
        [
            make_player(i, 25, main="MID", second=roles[i % 5], third=roles[(i + 2) % 5], rest=2)
            for i in range(1, 11)
        ],
        _one_per_team("MID", list(range(1, 11)), min_fit=3),
    )

    add(
        "two_masters_mid",
        "Two Masters who both main mid among eight Golds: they should face each other in mid.",
        [make_player(1, 31, main="MID", second="TOP"), make_player(2, 31, main="MID", second="ADC")]
        + [make_player(i, 25, main=roles[i % 5], second=roles[(i + 1) % 5]) for i in range(3, 11)],
        _masters_meet([1, 2], "MID"),
    )

    add(
        "master_among_golds",
        "One Master and nine equal Golds: inherently lopsided (nobody can offset the Master), "
        "so the best possible edge is about 0.12-0.15 and the split must not be worse.",
        [make_player(1, 31.2, main="MID", rest=3)]
        + [make_player(i, 25, main=roles[i % 5], rest=3) for i in range(2, 11)],
        _valid_and_fair(0.16),
    )

    add(
        "three_masters",
        "Three Masters and seven Silvers: the Masters must not be stacked on one team.",
        [make_player(i, 31, main=roles[i % 5], rest=3) for i in range(1, 4)]
        + [make_player(i, 24, main=roles[i % 5], rest=3) for i in range(4, 11)],
        _balanced_and_split(0.25),
    )

    add(
        "masters_slumping",
        "Three Masters on a losing streak: the estimate stays high (28.2), the truth is 31.",
        [make_player(i, 28.2, main=roles[i % 5], rest=3, true_mu=31) for i in range(1, 4)]
        + [make_player(i, 23.2, main=roles[i % 5], rest=3) for i in range(4, 11)],
        _balanced_and_split(0.25),
    )

    add(
        "unknown_profiles",
        "Nobody has role data yet (new server): any valid, fair split will do.",
        [make_player(i, mu, rest=None) for i, mu in enumerate(range(22, 32), start=1)],
        _valid_and_fair(0.08),
    )

    add(
        "one_tricks",
        "Each player only knows one role (everything else 1): everyone must get it.",
        [make_player(i, 25, main=roles[(i - 1) % 5], rest=1) for i in range(1, 11)],
        _everyone_on_best,
    )

    add(
        "role_scarcity",
        "Only one player can play support well: they get it, the other team makes do.",
        [make_player(3, 25, main="SUPPORT", rest=2)]
        + [make_player(i, 25, main=roles[i % 4], rest=3) for i in (1, 2, 4, 5, 6, 7, 8, 9, 10)],
        _scarce_role(3, "SUPPORT"),
    )

    add(
        "wide_skill_spread",
        "Ratings from 15 to 35: a hard lobby, but the split should still be even.",
        [
            make_player(i, 15 + (i - 1) * 20 / 9, main=roles[(i - 1) % 5], rest=2)
            for i in range(1, 11)
        ],
        _valid_and_fair(0.08),
    )

    add(
        "newcomers_with_high_uncertainty",
        "Four brand-new players (very uncertain ratings) with six established ones.",
        [make_player(i, 25, main=roles[i % 5], rest=2, sigma=8.3) for i in range(1, 5)]
        + [make_player(i, 24 + i % 4, main=roles[i % 5], rest=2, sigma=3.0) for i in range(5, 11)],
        _valid_and_fair(0.10),
    )

    add(
        "star_role_collision",
        "The two best players both main ADC: they should meet in ADC, or at least be split up.",
        [
            make_player(1, 32, main="ADC", rest=2),
            make_player(2, 31, main="ADC", rest=2),
            *[
                make_player(i, 24, main=roles[i % 5], second=roles[(i + 1) % 5])
                for i in range(3, 11)
            ],
        ],
        _stars_apart([1, 2], "ADC"),
    )

    return {s.name: s for s in scenarios}
