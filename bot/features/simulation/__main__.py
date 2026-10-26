"""Command-line reports on how the matchmaker behaves. No Discord, no database, no network.

python -m bot.features.simulation list
python -m bot.features.simulation scenario five_junglers --seed 1
python -m bot.features.simulation scenario all
python -m bot.features.simulation population --style role_stack --lobbies 20 --seed 3
python -m bot.features.simulation league --players 14 --lobbies 30 --seed 2
"""

import argparse
import random
import sys
from dataclasses import replace

from bot.features.balance.model import BalanceParams
from bot.features.simulation.league import League
from bot.features.simulation.players import STYLES, random_roster
from bot.features.simulation.runner import (
    format_aggregate,
    format_outcome,
    run_population,
    run_scenario,
)
from bot.features.simulation.scenarios import build_scenarios


def main(argv: list[str] | None = None) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")  # tier names have accents
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the scenarios")
    sc = sub.add_parser("scenario", help="run one scenario (or 'all') and show the teams")
    sc.add_argument("name")
    sc.add_argument("--seed", type=int, default=1)
    pop = sub.add_parser("population", help="many random lobbies, summarised")
    pop.add_argument("--style", choices=STYLES, default="realistic")
    pop.add_argument("--lobbies", type=int, default=20)
    pop.add_argument("--seed", type=int, default=1)
    pop.add_argument("--estimate-noise", type=float, default=0.0, help="rating error (sd)")
    lg = sub.add_parser("league", help="play matches and watch ratings and tiers move")
    lg.add_argument("--players", type=int, default=14)
    lg.add_argument("--lobbies", type=int, default=30)
    lg.add_argument("--seed", type=int, default=1)
    for p in (sc, pop, lg):
        p.add_argument("--w-lane", type=float, default=BalanceParams.w_lane)
        p.add_argument("--w-role", type=float, default=BalanceParams.w_role)
    args = parser.parse_args(argv)
    params = replace(
        BalanceParams(),
        w_lane=getattr(args, "w_lane", 0.004),
        w_role=getattr(args, "w_role", 0.015),
    )

    scenarios = build_scenarios()
    if args.command == "list":
        for s in scenarios.values():
            print(f"{s.name:<34} {s.description}")
        return 0
    if args.command == "scenario":
        names = list(scenarios) if args.name == "all" else [args.name]
        failed = 0
        for name in names:
            if name not in scenarios:
                print(f"Unknown scenario {name!r}; try: list")
                return 2
            outcome, problems = run_scenario(scenarios[name], params, args.seed)
            print(f"== {name}: {scenarios[name].description}")
            print(format_outcome(outcome))
            print("   as expected" if not problems else "   PROBLEMS: " + "; ".join(problems))
            print()
            failed += bool(problems)
        return 1 if failed else 0
    if args.command == "population":
        agg, _ = run_population(args.lobbies, args.style, args.seed, params, args.estimate_noise)
        print(f"style {args.style}: {format_aggregate(agg)}")
        return 0
    rng = random.Random(args.seed)
    league = League(random_roster(rng, args.players), args.seed, balance_params=params)
    report = league.run(args.lobbies)
    print(f"{args.players} players, {args.lobbies} lobbies, seed {args.seed}")
    print(format_aggregate(report.aggregate))
    print(
        f"rating vs true skill (rank correlation): start {report.start_correlation:.2f}"
        f" -> end {report.end_correlation:.2f}"
    )
    for label, count in sorted(report.tiers.items()):
        print(f"  {label:<26} {'#' * count} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
