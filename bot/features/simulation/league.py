"""A small simulated league: players with hidden true skill play balanced lobbies and their
ratings move with the results.

This is the base for testing everything that depends on match results (rating gains, rank
progression, the rank-informed matchmaking estimate) without Discord, a database or ten friends.
It uses the real pieces: the balancer, the matchmaking estimate E, the OpenSkill engine and the
ladder. Outcomes are sampled from the *hidden truth*, so we can ask whether the system learns.
"""

import random
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from bot.features.balance.engine import balance
from bot.features.balance.model import BalanceParams, PlayerInput
from bot.features.ratings.engine import Rating, RatingEngine
from bot.features.ratings.ladder import DEFAULT_LADDER, LadderConfig, derive_standing
from bot.features.ratings.matchmaking import Estimate, effective_estimate, seed_from_rank
from bot.features.ratings.params import MatchmakingParams
from bot.features.simulation.metrics import (
    Aggregate,
    ProposalMetrics,
    aggregate,
    evaluate,
    true_blue_win_prob,
)
from bot.features.simulation.players import SimPlayer


@dataclass
class LeaguePlayer:
    sim: SimPlayer  # `sim.mu` is not used here: the estimate is recomputed from `h` and `rank_mu`
    rank_mu: float  # the rank-based estimate R (a noisy view of the truth, like a Riot rank)
    h: Rating  # the in-house rating
    games: int = 0
    wins: int = 0
    mu_history: list[float] = field(default_factory=list)  # H.mu after every game


@dataclass(frozen=True)
class MatchRecord:
    index: int
    blue_ids: tuple[int, ...]
    red_ids: tuple[int, ...]
    winner: str  # blue | red
    metrics: ProposalMetrics
    mu_change: dict[int, float]  # change of each player's in-house mu


@dataclass(frozen=True)
class LeagueReport:
    records: list[MatchRecord]
    aggregate: Aggregate
    start_correlation: float  # rank correlation of rating vs true skill before any game
    end_correlation: float  # ... and after the last one (does the system learn?)
    tiers: Counter[str]  # visible in-house tiers at the end ("(colocação)" = still placing)


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Rank correlation (ties share their average rank)."""

    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out

    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else 0.0


class League:
    def __init__(
        self,
        players: Sequence[SimPlayer],
        seed: int = 0,
        rank_noise: float = 2.5,
        params: MatchmakingParams | None = None,
        balance_params: BalanceParams | None = None,
        engine: RatingEngine | None = None,
        ladder: LadderConfig = DEFAULT_LADDER,
    ) -> None:
        if len(players) < 10:
            raise ValueError("A league needs at least ten players")
        self.rng = random.Random(seed)
        self.params = params or MatchmakingParams()
        self.balance_params = balance_params or BalanceParams()
        self.engine = engine or RatingEngine()
        self.ladder = ladder
        self.players: dict[int, LeaguePlayer] = {}
        for sim in players:
            rank_mu = sim.true_mu + self.rng.gauss(0, rank_noise)
            mu, sigma = seed_from_rank(rank_mu, self.params)
            self.players[sim.player_id] = LeaguePlayer(sim, rank_mu, Rating(mu, sigma))
        self.records: list[MatchRecord] = []

    def estimate(self, player: LeaguePlayer) -> Estimate:
        return effective_estimate(player.h.mu, player.h.sigma, player.rank_mu, self.params)

    def correlation(self) -> float:
        everyone = list(self.players.values())
        return spearman([p.h.mu for p in everyone], [p.sim.true_mu for p in everyone])

    def play_lobby(self, lobby_ids: Sequence[int] | None = None) -> MatchRecord:
        ids = list(lobby_ids) if lobby_ids else self.rng.sample(sorted(self.players), 10)
        believed = []
        for pid in ids:
            lp = self.players[pid]
            est = self.estimate(lp)
            believed.append(replace(lp.sim, mu=est.mu, sigma=est.sigma))
        inputs: list[PlayerInput] = [p.to_input() for p in believed]
        result = balance(inputs, self.balance_params, self.rng)
        proposal = result.proposals[0]
        metrics = evaluate(proposal, believed, self.balance_params)

        by_id = {p.player_id: p for p in believed}
        winner = "blue" if self.rng.random() < true_blue_win_prob(proposal, by_id) else "red"
        blue_ids = tuple(s.player_id for s in proposal.blue)
        red_ids = tuple(s.player_id for s in proposal.red)
        win_ids, lose_ids = (blue_ids, red_ids) if winner == "blue" else (red_ids, blue_ids)
        new_w, new_l = self.engine.update(
            [self.players[i].h for i in win_ids], [self.players[i].h for i in lose_ids]
        )
        change: dict[int, float] = {}
        for pid, new, won in (
            *((i, r, True) for i, r in zip(win_ids, new_w, strict=True)),
            *((i, r, False) for i, r in zip(lose_ids, new_l, strict=True)),
        ):
            lp = self.players[pid]
            change[pid] = new.mu - lp.h.mu
            lp.h = new
            lp.games += 1
            lp.wins += won
            lp.mu_history.append(new.mu)
        record = MatchRecord(len(self.records) + 1, blue_ids, red_ids, winner, metrics, change)
        self.records.append(record)
        return record

    def run(self, lobbies: int) -> LeagueReport:
        start = self.correlation()
        for _ in range(lobbies):
            self.play_lobby()
        tiers: Counter[str] = Counter()
        for lp in self.players.values():
            standing = derive_standing(lp.h.mu, lp.games, None, self.ladder)
            label = (
                self.ladder.label(standing.tier_key or "", standing.division)
                if standing.placed
                else "(colocação)"
            )
            tiers[label] += 1
        return LeagueReport(
            self.records,
            aggregate([r.metrics for r in self.records]),
            start,
            self.correlation(),
            tiers,
        )
