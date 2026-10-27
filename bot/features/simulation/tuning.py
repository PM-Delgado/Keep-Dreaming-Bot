"""The tuning lab: run many seasons of a realistic group under different settings and compare.

A season plays full games through the real lobby/match/rating services (see `season.py`) with
outcomes decided by hidden truth. This module runs several seeds per configuration in parallel
(the same seeds for every configuration, so differences come from the setting, not from luck),
and reduces each season to the numbers that matter for tuning:

* balance: how lopsided the games really were, early and late (hidden truth, no form);
* prediction error: |predicted win chance - true win chance| of the lobbies the bot built;
* rating error: RMSE of the matchmaking estimate E against true skill after 0/10/20/40/N games;
* tier accuracy: how far visible tiers are from the tier the true skill deserves, and how often
  tiers change (flapping).

Overrides are plain field names of `MatchmakingParams`, `EngineParams`, `LadderConfig` and
`BalanceParams` (for example `d_up`, `beta`, `placement_games`, `w_lane`).
"""

import asyncio
import random
import statistics
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, fields, replace

from bot.features.balance.model import BalanceParams
from bot.features.ratings.ladder import LadderConfig
from bot.features.ratings.params import EngineParams, MatchmakingParams
from bot.features.simulation.population import realistic_population
from bot.features.simulation.season import Season, SeasonOptions

CHECKPOINTS = (10, 20, 40)


@dataclass(frozen=True)
class World:
    """How the hidden world behaves (not tunable: these are assumptions about reality)."""

    size: int = 20
    deviation_sd: float = 1.2  # true skill vs Riot rank
    outlier_share: float = 0.15  # smurfs / inactive old peaks
    outlier_size: float = 3.0
    form_sd: float = 1.0  # day-to-day form
    form_persistence: float = 0.5


@dataclass(frozen=True)
class Experiment:
    name: str
    overrides: Mapping[str, float] = field(default_factory=dict)
    world: World = World()
    games: int = 60
    seeds: tuple[int, ...] = (1, 2, 3, 4, 5, 6)


@dataclass(frozen=True)
class SeedResult:
    seed: int
    early_edge: float  # mean true edge over the first 20 games
    late_edge: float  # ... and over the last 20
    lopsided: float  # share of games with a clear hidden favourite
    p_error: float
    rmse: tuple[float, ...]  # estimate error at 0, 10, 20, 40 games and at the end
    correlation: float
    tier_error: float
    within_one: float
    placed: float
    moves: float
    problems: int
    outcome_lopsided: float = 0.0  # like `lopsided`, counting the day's form
    outcome_p_error: float = 0.0  # like `p_error`, against the odds that decided the game


def apply_overrides(base: SeasonOptions, overrides: Mapping[str, float]) -> SeasonOptions:
    """Return `base` with each named field replaced; unknown names are an error."""
    sections = {
        "params": (base.params, {f.name for f in fields(MatchmakingParams)}),
        "engine": (base.engine, {f.name for f in fields(EngineParams)}),
        "ladder": (base.ladder, {"placement_games", "demotion_buffer", "division_width"}),
        "balance": (base.balance, {f.name for f in fields(BalanceParams)}),
    }
    updates: dict[str, dict[str, float]] = {name: {} for name in sections}
    oracle = None
    for key, value in overrides.items():
        if key == "oracle":
            oracle = bool(value)
            continue
        for name, (_, known) in sections.items():
            if key in known:
                current_value = getattr(sections[name][0], key)
                is_int = isinstance(current_value, int) and not isinstance(current_value, bool)
                updates[name][key] = int(value) if is_int else value  # e.g. form_games=10.0
                break
        else:
            raise ValueError(f"Unknown setting {key!r}")
    result = base if oracle is None else replace(base, oracle=oracle)
    for name, (current, _) in sections.items():
        if updates[name]:
            if name == "ladder":
                changed = replace(current, **updates[name])
                changed = LadderConfig(
                    changed.version + 1,
                    changed.tiers,
                    changed.division_width,
                    changed.demotion_buffer,
                    changed.placement_games,
                )
                result = replace(result, ladder=changed)
            else:
                result = replace(result, **{name: replace(current, **updates[name])})  # type: ignore[arg-type]
    return result


def known_settings() -> dict[str, list[str]]:
    return {
        "ratings (E vs rank)": [f.name for f in fields(MatchmakingParams)],
        "ratings (OpenSkill)": [f.name for f in fields(EngineParams)],
        "ladder": ["placement_games", "demotion_buffer", "division_width"],
        "balancer": [f.name for f in fields(BalanceParams)],
        "experiment": [
            "oracle (1 = ratings start equal to the truth: the best any rating could do)"
        ],
    }


async def _season(experiment: Experiment, seed: int) -> SeedResult:
    world = experiment.world
    population = realistic_population(
        random.Random(seed * 1000 + 1),
        world.size,
        deviation_sd=world.deviation_sd,
        outlier_share=world.outlier_share,
        outlier_size=world.outlier_size,
    )
    base = SeasonOptions(form_sd=world.form_sd, form_persistence=world.form_persistence)
    season = await Season.create(
        population, seed, options=apply_overrides(base, experiment.overrides)
    )
    try:
        report = await season.run(experiment.games)
    finally:
        await season.close()
    edges = [g.true_edge for g in report.games]
    checkpoints = (0, *CHECKPOINTS, len(report.games))
    return SeedResult(
        seed,
        statistics.fmean(edges[:20]),
        statistics.fmean(edges[-20:]),
        report.lopsided_share,
        report.mean_p_error,
        tuple(report.rmse_after(n) for n in checkpoints),
        report.end_correlation,
        report.tier_error,
        report.tier_within_one,
        report.placed_share,
        report.tier_moves_per_player,
        len(report.problems),
        report.outcome_lopsided_share,
        report.mean_outcome_p_error,
    )


def run_seed(experiment: Experiment, seed: int) -> SeedResult:
    """One season in its own event loop (a worker process runs this)."""
    return asyncio.run(_season(experiment, seed))


@dataclass(frozen=True)
class Summary:
    name: str
    results: tuple[SeedResult, ...]

    def mean(self, attribute: str) -> float:
        return statistics.fmean(getattr(r, attribute) for r in self.results)

    def sd(self, attribute: str) -> float:
        values = [getattr(r, attribute) for r in self.results]
        return statistics.stdev(values) if len(values) > 1 else 0.0

    def mean_rmse(self, index: int) -> float:
        return statistics.fmean(r.rmse[index] for r in self.results)

    @property
    def problems(self) -> int:
        return sum(r.problems for r in self.results)


def run_experiments(experiments: Sequence[Experiment], workers: int | None = None) -> list[Summary]:
    """Every (experiment, seed) is a separate season, spread over the CPU cores."""
    tasks = [(e, s) for e in experiments for s in e.seeds]
    if workers == 1 or len(tasks) == 1:
        outcomes = [run_seed(e, s) for e, s in tasks]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_seed, e, s) for e, s in tasks]
            outcomes = [f.result() for f in futures]
    summaries = []
    for experiment in experiments:
        mine = tuple(r for (e, _), r in zip(tasks, outcomes, strict=True) if e is experiment)
        summaries.append(Summary(experiment.name, mine))
    return summaries


HEADER = (
    f"{'setting':<22}{'lopsided':>9}{'edge1-20':>9}{'edge-end':>9}{'p-err':>7}"
    f"{'E-rmse: 0':>10}{'10':>6}{'20':>6}{'40':>6}{'end':>6}{'corr':>6}"
    f"{'tier-err':>9}{'in±1':>6}{'moves':>7}{'w/form: lop':>12}{'p-err':>7}"
)


def format_summaries(summaries: Sequence[Summary]) -> str:
    lines = [HEADER]
    for s in summaries:
        lines.append(
            f"{s.name:<22}{s.mean('lopsided'):>9.0%}{s.mean('early_edge'):>9.3f}"
            f"{s.mean('late_edge'):>9.3f}{s.mean('p_error'):>7.3f}"
            f"{s.mean_rmse(0):>10.2f}{s.mean_rmse(1):>6.2f}{s.mean_rmse(2):>6.2f}"
            f"{s.mean_rmse(3):>6.2f}{s.mean_rmse(4):>6.2f}{s.mean('correlation'):>6.2f}"
            f"{s.mean('tier_error'):>9.2f}{s.mean('within_one'):>6.0%}{s.mean('moves'):>7.1f}"
            f"{s.mean('outcome_lopsided'):>12.0%}{s.mean('outcome_p_error'):>7.3f}"
            + ("" if not s.problems else f"  !! {s.problems} invariant problems")
        )
    return "\n".join(lines)
