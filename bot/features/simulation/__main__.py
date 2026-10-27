"""Command-line reports on how the matchmaker behaves. No Discord, no database, no network.

python -m bot.features.simulation list
python -m bot.features.simulation scenario five_junglers --seed 1
python -m bot.features.simulation scenario all
python -m bot.features.simulation population --style role_stack --lobbies 20 --seed 3
python -m bot.features.simulation league --players 14 --lobbies 30 --seed 2
python -m bot.features.simulation season --players 14 --games 40 --seed 2
python -m bot.features.simulation tune baseline
python -m bot.features.simulation tune sweep d_up 3 4.5 6
python -m bot.features.simulation tune compare "tight:d_up=3,d_down=2" "loose:d_up=6,d_down=4"
python -m bot.features.simulation tune settings
"""

import argparse
import asyncio
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
from bot.features.simulation.season import Season, format_season
from bot.features.simulation.tuning import (
    Experiment,
    World,
    format_summaries,
    known_settings,
    run_experiments,
)


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
    se = sub.add_parser(
        "season", help="play games through the real lobby/match/rating services (in memory)"
    )
    se.add_argument("--players", type=int, default=14)
    se.add_argument("--games", type=int, default=30)
    se.add_argument("--seed", type=int, default=1)
    se.add_argument("--style", choices=STYLES, default="realistic")
    se.add_argument(
        "--rank-noise", type=float, default=2.5, help="how far starting ranks are from the truth"
    )
    tu = sub.add_parser(
        "tune",
        help="compare settings over many seasons of a realistic group (see the module docs)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    tu.add_argument("mode", choices=("baseline", "sweep", "compare", "settings", "population"))
    tu.add_argument("rest", nargs="*", help="sweep: PARAM v1 v2 ... | compare: 'name:k=v,k=v' ...")
    tu.add_argument("--games", type=int, default=60)
    tu.add_argument("--seeds", type=int, default=6)
    tu.add_argument("--workers", type=int, default=None, help="processes (default: all cores)")
    tu.add_argument("--set", action="append", default=[], metavar="K=V", help="base override")
    tu.add_argument("--size", type=int, default=20, help="players in the group")
    tu.add_argument("--deviation-sd", type=float, default=World.deviation_sd)
    tu.add_argument("--outlier-share", type=float, default=World.outlier_share)
    tu.add_argument("--form-sd", type=float, default=World.form_sd)
    tu.add_argument(
        "--form-persistence",
        type=float,
        default=World.form_persistence,
        help="how much of a game's form carries into the next (0..1)",
    )
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
    if args.command == "season":
        return asyncio.run(_season(args))
    if args.command == "tune":
        return _tune(args)
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


async def _season(args: argparse.Namespace) -> int:
    roster = random_roster(random.Random(args.seed), args.players, args.style)
    season = await Season.create(roster, args.seed, args.rank_noise)
    try:
        report = await season.run(args.games)
    finally:
        await season.close()
    print(format_season(report))
    return 1 if report.problems else 0


def _parse_overrides(items: list[str]) -> dict[str, float]:
    out = {}
    for item in items:
        key, _, value = item.partition("=")
        out[key.strip()] = float(value)
    return out


def _tune(args: argparse.Namespace) -> int:
    if args.mode == "settings":
        for group, names in known_settings().items():
            print(f"{group}: {', '.join(names)}")
        return 0
    world = World(
        args.size,
        args.deviation_sd,
        args.outlier_share,
        World.outlier_size,
        args.form_sd,
        args.form_persistence,
    )
    seeds = tuple(range(1, args.seeds + 1))
    base = _parse_overrides(args.set)
    if args.mode == "population":
        from bot.features.simulation.population import describe_population, realistic_population

        people = realistic_population(
            random.Random(1001),
            world.size,
            deviation_sd=world.deviation_sd,
            outlier_share=world.outlier_share,
            outlier_size=world.outlier_size,
        )
        print(describe_population(people))
        return 0
    experiments = [Experiment("baseline", base, world, args.games, seeds)]
    try:
        if args.mode == "sweep":
            if len(args.rest) < 2:
                print("usage: tune sweep PARAM VALUE [VALUE ...]")
                return 2
            param, values = args.rest[0], args.rest[1:]
            experiments += [
                Experiment(f"{param}={v}", {**base, param: float(v)}, world, args.games, seeds)
                for v in values
            ]
        elif args.mode == "compare":
            for spec in args.rest:
                name, _, body = spec.partition(":")
                overrides = {**base, **_parse_overrides([x for x in body.split(",") if x])}
                experiments.append(Experiment(name, overrides, world, args.games, seeds))
        for e in experiments:  # fail on a typo before spending minutes
            from bot.features.simulation.season import SeasonOptions
            from bot.features.simulation.tuning import apply_overrides

            apply_overrides(SeasonOptions(), e.overrides)
    except ValueError as exc:
        print(exc)
        return 2
    print(
        f"{world.size} players, {args.games} games, seeds 1..{args.seeds}, "
        f"rank deviation sd {world.deviation_sd}, outliers {world.outlier_share:.0%}, "
        f"form sd {world.form_sd}"
    )
    print(format_summaries(run_experiments(experiments, args.workers)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
