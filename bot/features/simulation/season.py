"""A season of in-house games played through the *real* services: lobby, ready-check, balancer,
confirmed teams, reported and confirmed results, rating replay, ladder.

Everything the Discord buttons would do is done by calling the same service methods, against an
in-memory database, so a season tests the whole pipeline (not just the rating maths): who gets
matched, how teams land, how confirmed results move ratings and tiers, whether placement hides
tiers until the fifth game, and that the stored ratings stay equal to a full replay.

Outcomes come from the players' *hidden* truth, never from what the bot believes: true skill, how
well each plays the role they were given, and a persistent "form" (good and bad streaks) that no
rating can see. That keeps the judgement honest: the bot's predictions are graded against a world
it cannot read.

Only Discord (messages, buttons, pings) is left out; that part is covered by the cog tests.
"""

import json
import math
import random
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from bot.db import Database
from bot.features.balance.model import BalanceParams, Proposal
from bot.features.lobby.repository import LobbyRepository
from bot.features.lobby.service import LobbyParams, LobbyService
from bot.features.matches.repository import MatchRepository
from bot.features.matches.service import MatchService
from bot.features.players.repository import PlayerRepository
from bot.features.players.service import PlayerService
from bot.features.ratings.engine import RatingEngine
from bot.features.ratings.ladder import DEFAULT_LADDER, LadderConfig, derive_standing, step_rank
from bot.features.ratings.params import EngineParams, MatchmakingParams
from bot.features.ratings.rank import APEX_POINTS, DIVISIONS, TIERS
from bot.features.ratings.repository import RatingRepository
from bot.features.ratings.service import RatingService
from bot.features.simulation.league import spearman
from bot.features.simulation.metrics import BETA, true_blue_win_prob, true_role_strength
from bot.features.simulation.players import SimPlayer

GUILD, CHANNEL = 1, 1
START = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def rank_of(rank_mu: float) -> tuple[str, str | None]:
    """The Riot-style tier and division whose rank-based estimate is closest to `rank_mu`."""
    points = max(0, round((rank_mu - 20.0) / 0.4))
    if points >= APEX_POINTS["MASTER"]:
        return "MASTER", None  # Grandmaster and Challenger are not simulated
    return TIERS[points // 4], DIVISIONS[points % 4]


@dataclass(frozen=True)
class SeasonOptions:
    """Everything about the bot that can be tuned, plus how the hidden world behaves."""

    rank_noise: float = 2.5  # only used for players that come without a Riot rank
    params: MatchmakingParams = field(default_factory=MatchmakingParams)
    engine: EngineParams = field(default_factory=EngineParams)
    ladder: LadderConfig = DEFAULT_LADDER
    balance: BalanceParams = field(default_factory=BalanceParams)
    form_sd: float = 0.0  # hidden day-to-day form, in rating points (0 = no form)
    form_persistence: float = 0.5  # how much of last game's form carries over
    oracle: bool = False  # ratings start equal to the truth (the best any rating could do)


@dataclass(frozen=True)
class SeasonGame:
    index: int
    lobby_id: int
    match_id: int
    blue: tuple[str, ...]
    red: tuple[str, ...]
    winner: str
    predicted_blue: float  # what the balancer believed
    true_blue: float  # what the hidden skills say (without form: what a perfect rating could know)
    outcome_blue: float  # ... and with the day's form, which decided the game
    h_correlation: float  # rating vs true skill, after this game
    e_rmse: float  # error of the matchmaking estimates after this game (centered)
    tier_moves: int  # visible tier changes caused by this game

    @property
    def true_edge(self) -> float:
        return abs(self.true_blue - 0.5)

    @property
    def p_error(self) -> float:
        """How far the balancer's prediction was from the skill-only truth."""
        return abs(self.predicted_blue - self.true_blue)


@dataclass(frozen=True)
class PlayerLine:
    name: str
    true_mu: float
    start_rank: str
    games: int
    wins: int
    standing: str  # the visible tier, or "Colocação n/5"
    h_mu: float
    estimate_mu: float


@dataclass
class SeasonReport:
    games: list[SeasonGame]
    lines: list[PlayerLine]
    start_correlation: float
    end_correlation: float
    estimate_correlation: float
    tiers: Counter[str]
    problems: list[str] = field(default_factory=list)  # broken invariants (should be empty)
    start_rmse: float = 0.0  # estimate error before any game
    tier_error: float = 0.0  # mean |visible step - step of the true skill|, placed players
    tier_within_one: float = 0.0  # share of placed players within one step of the truth
    placed_share: float = 0.0
    tier_moves_per_player: float = 0.0  # promotions + demotions per player over the season

    @property
    def mean_true_edge(self) -> float:
        return sum(g.true_edge for g in self.games) / len(self.games) if self.games else 0.0

    @property
    def lopsided_share(self) -> float:
        """Games where the hidden skills made one side a clear favourite (>= 65%)."""
        if not self.games:
            return 0.0
        return sum(g.true_edge >= 0.15 for g in self.games) / len(self.games)

    @property
    def mean_p_error(self) -> float:
        return statistics.fmean(g.p_error for g in self.games) if self.games else 0.0

    def rmse_after(self, games: int) -> float:
        """Estimate error after `games` games (0 = the start)."""
        if games <= 0 or not self.games:
            return self.start_rmse
        return self.games[min(games, len(self.games)) - 1].e_rmse


class Season:
    """Build with `await Season.create(...)`, play with `await season.run(n)`, then `close()`."""

    def __init__(
        self, db: Database, players: Sequence[SimPlayer], seed: int, options: SeasonOptions
    ):
        if len(players) < 10:
            raise ValueError("A season needs at least ten players")
        if options.oracle:  # let E follow the truth wherever it is: no anchoring to the rank
            options = replace(options, params=replace(options.params, d_up=50.0, d_down=50.0))
        self.db = db
        self.rng = random.Random(seed)
        self.options = options
        self.sims = list(players)
        self.clock = _Clock()
        ratings = RatingService(
            RatingRepository(db),
            options.params,
            RatingEngine(options.engine),
            options.ladder,
            clock=self.clock,
        )
        self.ratings = ratings
        self.players = PlayerService(PlayerRepository(db), ratings, clock=self.clock)
        self.lobby_repo = LobbyRepository(db)
        self.match_repo = MatchRepository(db)
        ratings.matches_source = self.match_repo.confirmed_for_replay
        self.matches = MatchService(
            self.match_repo,
            self.lobby_repo,
            self.players,
            ratings,
            lambda _user_id: False,
            db,
            clock=self.clock,
        )
        self.lobbies = LobbyService(
            self.lobby_repo,
            self.players,
            ratings,
            self.matches,
            is_admin=lambda _user_id: False,
            params=LobbyParams(),
            balance_params=options.balance,
            clock=self.clock,
            rng=random.Random(seed),
        )
        self.sim_of: dict[int, SimPlayer] = {}  # database player id -> synthetic player
        self.discord_of: dict[int, int] = {}
        self.wins: Counter[int] = Counter()
        self.start_rank: dict[int, str] = {}
        self.form: dict[int, float] = {}
        self.games: list[SeasonGame] = []
        self.moves: Counter[int] = Counter()
        self._steps: dict[int, int | None] = {}
        self._start_rmse = 0.0

    @classmethod
    async def create(
        cls,
        players: Sequence[SimPlayer],
        seed: int = 1,
        rank_noise: float = 2.5,
        options: SeasonOptions | None = None,
    ) -> "Season":
        """With `options`, its fields rule (and `rank_noise` is ignored)."""
        db = Database(":memory:")
        await db.connect()
        season = cls(db, players, seed, options or SeasonOptions(rank_noise=rank_noise))
        await season._register_everyone()
        return season

    async def close(self) -> None:
        await self.db.close()

    async def _register_everyone(self) -> None:
        for sim in self.sims:
            discord_id = -sim.player_id
            player = await self.players.register(discord_id, sim.name, is_mock=True)
            for role, level in sim.proficiency.items():
                if level is not None:
                    await self.players.repo.set_declared(player.id, role, level, self.clock())
            if sim.rank is not None:
                tier, division = sim.rank
            else:  # like a Riot rank: the truth plus the noise of how people actually climbed
                tier, division = rank_of(sim.true_mu + self.rng.gauss(0, self.options.rank_noise))
            await self.ratings.set_self_reported_rank(player.id, tier, division, None)
            if self.options.oracle:
                await self.ratings.adjust(player.id, sim.true_mu, 0.5, 0, "oracle")
            self.sim_of[player.id] = sim
            self.discord_of[player.id] = discord_id
            self.start_rank[player.id] = tier.title() + (f" {division}" if division else "")
            self.form[player.id] = (
                self.rng.gauss(0, self.options.form_sd) if self.options.form_sd else 0.0
            )
        self._steps = await self._current_steps()
        self._start_rmse = await self._estimate_rmse()

    # --- the hidden world ----

    def _outcome_probability(self, proposal: Proposal) -> float:
        """P(blue wins) including today's form: what actually decides the game."""
        blue = sum(
            true_role_strength(self.sim_of[s.player_id], s.role) + self.form[s.player_id]
            for s in proposal.blue
        )
        red = sum(
            true_role_strength(self.sim_of[s.player_id], s.role) + self.form[s.player_id]
            for s in proposal.red
        )
        return 0.5 * (1 + math.erf((blue - red) / math.sqrt(10 * BETA**2) / math.sqrt(2)))

    def _advance_form(self, player_ids: Sequence[int]) -> None:
        sd, rho = self.options.form_sd, self.options.form_persistence
        if not sd:
            return
        for player_id in player_ids:
            fresh = self.rng.gauss(0, sd)
            self.form[player_id] = rho * self.form[player_id] + math.sqrt(1 - rho**2) * fresh

    # --- playing ----

    async def play_game(self) -> SeasonGame:
        chosen = self.rng.sample(sorted(self.sim_of), 10)
        creator = self.discord_of[chosen[0]]
        update = await self.lobbies.open(GUILD, CHANNEL, creator)
        lobby_id = update.snapshot.lobby.id
        for player_id in chosen[1:]:
            await self.lobbies.join(lobby_id, self.discord_of[player_id])
        self.clock.advance(seconds=30)
        for player_id in chosen:
            await self.lobbies.ready(lobby_id, self.discord_of[player_id])
        await self.lobbies.confirm(lobby_id, creator)

        lobby = await self.lobby_repo.get(lobby_id)
        match = await self.match_repo.get_by_lobby(lobby_id)
        assert lobby is not None and lobby.proposal_json is not None and match is not None
        proposal = Proposal.from_dict(json.loads(lobby.proposal_json)["current"])
        true_blue = true_blue_win_prob(proposal, self.sim_of)
        outcome_blue = self._outcome_probability(proposal)
        winner = "blue" if self.rng.random() < outcome_blue else "red"
        winners, losers = (
            (proposal.blue, proposal.red) if winner == "blue" else (proposal.red, proposal.blue)
        )
        self.clock.advance(minutes=35)  # the game itself
        await self.lobbies.report_result(lobby_id, self.discord_of[winners[0].player_id], winner)
        await self.lobbies.confirm_result(lobby_id, self.discord_of[losers[0].player_id])
        for slot in winners:
            self.wins[slot.player_id] += 1
        self._advance_form([s.player_id for s in (*proposal.blue, *proposal.red)])
        self.clock.advance(hours=1)

        steps = await self._current_steps()
        moved = sum(
            1
            for pid, step in steps.items()
            if step is not None and self._steps.get(pid) not in (None, step)
        )
        for pid, step in steps.items():
            if step is not None and self._steps.get(pid) not in (None, step):
                self.moves[pid] += 1
        self._steps = steps

        game = SeasonGame(
            len(self.games) + 1,
            lobby_id,
            match.id,
            tuple(self.sim_of[s.player_id].name for s in proposal.blue),
            tuple(self.sim_of[s.player_id].name for s in proposal.red),
            winner,
            match.expected_blue_win,
            true_blue,
            outcome_blue,
            await self._h_correlation(),
            await self._estimate_rmse(),
            moved,
        )
        self.games.append(game)
        return game

    async def _h_correlation(self) -> float:
        stored = {r.player_id: r.mu for r in await self.ratings.repo.list_stored()}
        ids = sorted(self.sim_of)
        return spearman([stored[i] for i in ids], [self.sim_of[i].true_mu for i in ids])

    async def _estimate_rmse(self) -> float:
        """Error of the matchmaking estimates E against true skill, ignoring any common offset."""
        ids = sorted(self.sim_of)
        estimates = [(await self.ratings.inspect(i)).estimate.mu for i in ids]
        truth = [self.sim_of[i].true_mu for i in ids]
        me, mt = statistics.fmean(estimates), statistics.fmean(truth)
        return math.sqrt(
            statistics.fmean(
                ((e - me) - (t - mt)) ** 2 for e, t in zip(estimates, truth, strict=True)
            )
        )

    async def _current_steps(self) -> dict[int, int | None]:
        ladder = self.options.ladder
        return {
            r.player_id: step_rank(ladder, r.tier_key, r.division) if r.placed else None
            for r in await self.ratings.repo.list_stored()
        }

    async def run(self, games: int) -> SeasonReport:
        start = await self._h_correlation()
        for _ in range(games):
            await self.play_game()
        return await self.report(start)

    # --- reporting and invariants ----

    async def report(self, start_correlation: float | None = None) -> SeasonReport:
        ids = sorted(self.sim_of)
        lines = []
        tiers: Counter[str] = Counter()
        estimates = []
        ladder = self.options.ladder
        errors = []
        for player_id in ids:
            inspection = await self.ratings.inspect(player_id)
            stored, standing = inspection.stored, inspection.standing
            label = (
                ladder.label(standing.tier_key or "", standing.division)
                if standing.placed
                else f"Colocação {standing.games}/{ladder.placement_games}"
            )
            tiers[label if standing.placed else "(colocação)"] += 1
            sim = self.sim_of[player_id]
            estimates.append(inspection.estimate.mu)
            if standing.placed:
                ideal = derive_standing(sim.true_mu, ladder.placement_games, None, ladder)
                seen = step_rank(ladder, standing.tier_key, standing.division)
                want = step_rank(ladder, ideal.tier_key, ideal.division)
                if seen is not None and want is not None:
                    errors.append(abs(seen - want))
            lines.append(
                PlayerLine(
                    sim.name,
                    sim.true_mu,
                    self.start_rank[player_id],
                    stored.games,
                    self.wins[player_id],
                    label,
                    stored.mu,
                    inspection.estimate.mu,
                )
            )
        truth = [self.sim_of[i].true_mu for i in ids]
        return SeasonReport(
            list(self.games),
            lines,
            start_correlation if start_correlation is not None else 0.0,
            await self._h_correlation(),
            spearman(estimates, truth),
            tiers,
            await self.verify(),
            self._start_rmse,
            statistics.fmean(errors) if errors else 0.0,
            sum(e <= 1 for e in errors) / len(errors) if errors else 0.0,
            len(errors) / len(ids),
            sum(self.moves.values()) / len(ids),
        )

    async def verify(self) -> list[str]:
        """Invariants of the whole pipeline; an empty list means all hold."""
        problems = []
        stored = await self.ratings.repo.list_stored()
        await self.ratings.rebuild()
        if await self.ratings.repo.list_stored() != stored:
            problems.append("stored ratings differ from a full replay of the confirmed games")
        if await self.match_repo.list_by_status(["in_progress", "reported"]):
            problems.append("a match is still waiting for its result")
        confirmed = await self.match_repo.list_by_status(["confirmed"])
        if len(confirmed) != len(self.games):
            problems.append(f"{len(confirmed)} confirmed matches for {len(self.games)} games")
        if sum(r.games for r in stored) != 10 * len(self.games):
            problems.append("games played per player do not add up to 10 per match")
        for r in stored:
            if r.placed != (r.games >= self.options.ladder.placement_games):
                problems.append(f"player {r.player_id} placement is wrong at {r.games} games")
            if not r.placed and r.tier_key is not None:
                problems.append(f"player {r.player_id} shows a tier during placement")
        for sim_id in self.sim_of:
            if await self.lobby_repo.busy_lobby_of(sim_id) is not None:
                problems.append(f"player {sim_id} is still busy after the season")
        return problems


def format_season(report: SeasonReport, show_games: int = 5) -> str:
    out = [f"{len(report.games)} games played through the real lobby/match/rating services"]
    if report.games:
        out.append(
            f"balance (hidden truth): mean edge {report.mean_true_edge:.3f}, "
            f"clearly lopsided {report.lopsided_share:.0%}, "
            f"prediction error {report.mean_p_error:.3f}"
        )
        out.append("")
        out.append("first games:")
        for g in report.games[:show_games]:
            out.append(
                f"  #{g.index}: {g.winner} won  predicted blue {g.predicted_blue:.0%}  "
                f"truth {g.true_blue:.0%}"
            )
    out += [
        "",
        f"rating vs true skill (rank correlation): start {report.start_correlation:+.2f} -> "
        f"end {report.end_correlation:+.2f}  (matchmaking estimate: "
        f"{report.estimate_correlation:+.2f})",
        "",
        f"{'player':<8}{'true':>7}  {'started as':<14}{'W-L':>7}  {'visible rank':<28}{'H':>7}",
    ]
    for line in sorted(report.lines, key=lambda x: -x.true_mu):
        losses = line.games - line.wins
        out.append(
            f"{line.name:<8}{line.true_mu:>7.1f}  {line.start_rank:<14}"
            f"{f'{line.wins}-{losses}':>7}  {line.standing:<28}{line.h_mu:>7.1f}"
        )
    out.append("")
    out.append("tiers: " + ", ".join(f"{k} x{v}" for k, v in sorted(report.tiers.items())))
    out.append("invariants: " + ("all hold" if not report.problems else "; ".join(report.problems)))
    return "\n".join(out)
