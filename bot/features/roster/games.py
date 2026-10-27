"""Test games for the roster's real players, played through the real services.

The point is to review the bot's messages with the real group on screen. Ratings, tiers,
placement and every game state are what the bot itself would have produced; only the *results*
are made up (who wins follows each player's hidden strength, see `loader.strength_of`).
Everything here is test data: `reset` removes it.

The unfinished games (contested, reported, in progress) need players who are free, and nobody can
be in two games at once, so they are completed with mock players (nicknames "Mock Extra NN",
removed by `reset` and by `/mock clear`).
"""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.features.players.proficiency import ROLES
from bot.features.ratings.params import MatchmakingParams
from bot.features.ratings.rank import rank_mu
from bot.features.roster.loader import RosterEntry, strength_of
from bot.features.roster.stack import Stack
from bot.features.simulation.metrics import BETA, true_role_strength
from bot.features.simulation.players import SimPlayer

MOCK_ID_BASE = 990_000_000_000_000_000
CHANNEL = 1  # a placeholder: the sweeper only logs a warning when it cannot post there
CHAMPIONS = {
    "TOP": ["Garen", "Darius", "Sett", "Malphite", "Ornn"],
    "JUNGLE": ["Lee Sin", "Vi", "Viego", "Elise", "Graves"],
    "MID": ["Ahri", "Zed", "Yasuo", "Orianna", "Syndra"],
    "ADC": ["Jinx", "Ezreal", "Ashe", "Caitlyn", "Kai'Sa"],
    "SUPPORT": ["Thresh", "Lulu", "Lux", "Leona", "Nami"],
}
# Game number -> how it ends; every other finished game is confirmed by the players.
SPECIAL = {4: "admin", 8: "void_early", 11: "fixed", 14: "auto", 17: "void_after", 20: "admin"}


class PlaygroundError(Exception):
    """The roster cannot play (too few players, the admin is missing, ...)."""


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass(frozen=True)
class Played:
    finished: int
    unfinished: int
    mock_players: int


def sim_of(entry: RosterEntry, params: MatchmakingParams) -> SimPlayer:
    strength = strength_of(entry, params)
    proficiency = {role: entry.roles.get(role) for role in ROLES}
    return SimPlayer(entry.discord_id, entry.nickname, strength, 5.0, strength, proficiency)


class Playground:
    def __init__(
        self,
        stack: Stack,
        guild_id: int,
        entries: Sequence[RosterEntry],
        admin_id: int,
        clock: Clock,
        seed: int,
        real_now: datetime,
    ) -> None:
        if admin_id not in {e.discord_id for e in entries}:
            raise PlaygroundError(
                f"The admin ({admin_id}) must be in the roster: reports and validations are"
                " made as them. Check ADMIN_USER_IDS or pass --admin-id."
            )
        self.real_now = real_now
        self.stack = stack
        self.guild_id = guild_id
        self.admin_id = admin_id
        self.clock = clock
        self.rng = random.Random(seed)
        params = stack.ratings.params
        self.params = params
        self.sims = {e.discord_id: sim_of(e, params) for e in entries}
        self.entries = {e.discord_id: e for e in entries}
        self.pid_of: dict[int, int] = {}  # discord id -> player id
        self.stats_seen: dict[int, int] = {}
        self.mocks = 0

    async def prepare(self) -> None:
        await self.stack.champions.start()
        for discord_id in self.sims:
            player = await self.stack.players.require(discord_id)
            self.pid_of[discord_id] = player.id

    async def add_mock(self) -> int:
        """A mock player (Silver-ish) to complete a game; returns its Discord id."""
        self.mocks += 1
        n = self.mocks
        discord_id = MOCK_ID_BASE + n
        stack = self.stack
        player = await stack.players.register(discord_id, f"Mock Extra {n:02d}", is_mock=True)
        main = ROLES[n % 5]
        await stack.players.set_declared(discord_id, main, 5)
        await stack.ratings.set_self_reported_rank(player.id, "SILVER", "III", None)
        strength = rank_mu("SILVER", "III", self.params)
        self.sims[discord_id] = SimPlayer(
            discord_id, f"Mock Extra {n:02d}", strength, 5.0, strength, {main: 5}
        )
        self.pid_of[discord_id] = player.id
        return discord_id

    # --- one game ------------------------------------------------------------------------------

    async def start_game(
        self, lineup: list[int], minutes: float
    ) -> tuple[int, int, dict[str, list[int]]]:
        """Run a lobby up to confirmed teams. Returns (lobby id, match id, discord ids by side)."""
        lobbies = self.stack.lobbies
        creator = lineup[0]
        update = await lobbies.open(self.guild_id, CHANNEL, creator)
        lobby_id = update.snapshot.lobby.id
        for discord_id in lineup[1:]:
            await lobbies.join(lobby_id, discord_id)
        self.clock.advance(seconds=30)
        for discord_id in lineup:
            await lobbies.ready(lobby_id, discord_id)
        await lobbies.confirm(lobby_id, creator)
        match = await self.stack.match_repo.get_by_lobby(lobby_id)
        assert match is not None
        discord_of = {pid: did for did, pid in self.pid_of.items()}
        sides: dict[str, list[int]] = {"blue": [], "red": []}
        for row in await self.stack.match_repo.participants(match.id):
            sides[row.side].append(discord_of[row.player_id])
        self.clock.advance(minutes=minutes)  # the game itself
        return lobby_id, match.id, sides

    async def _winner(self, match_id: int) -> str:
        """Blue wins with the probability the hidden strengths (and roles) give it."""
        discord_of = {pid: did for did, pid in self.pid_of.items()}
        total = {"blue": 0.0, "red": 0.0}
        for row in await self.stack.match_repo.participants(match_id):
            sim = self.sims[discord_of[row.player_id]]
            total[row.side] += true_role_strength(sim, row.role)
        diff = (total["blue"] - total["red"]) / math.sqrt(10 * BETA**2)
        blue = 0.5 * (1 + math.erf(diff / math.sqrt(2)))
        return "blue" if self.rng.random() < blue else "red"

    async def play(self, lineup: list[int], how: str = "players", minutes: float = 35) -> int:
        """One game that lasted `minutes`, ended in the way `how` says. Returns the match id."""
        lobbies = self.stack.lobbies
        lobby_id, match_id, sides = await self.start_game(lineup, minutes)
        winner = await self._winner(match_id)
        loser = "red" if winner == "blue" else "blue"
        # An admin's report is final at once, so players reporting or confirming are never admins.
        win = next(d for d in sides[winner] if d != self.admin_id)
        lose = next(d for d in sides[loser] if d != self.admin_id)
        confirmed = False
        if how == "in_progress":
            return match_id
        if how == "void_early":
            await lobbies.void_match(match_id, self.admin_id, "jogador desistiu")
        elif how == "reported":
            await lobbies.report_result(lobby_id, win, winner)
        elif how == "contested":
            await lobbies.report_result(lobby_id, win, winner)
            await lobbies.contest_result(lobby_id, lose)
        elif how == "admin":
            await lobbies.report_result(lobby_id, self.admin_id, winner)
            confirmed = True
        elif how == "auto":
            await lobbies.report_result(lobby_id, win, winner)
            self.clock.advance(minutes=31)
            await lobbies.process_matches()
            confirmed = True
        else:  # players: the winners report, someone on the other team confirms
            await lobbies.report_result(lobby_id, win, winner)
            self.clock.advance(minutes=2)
            await lobbies.confirm_result(lobby_id, lose)
            confirmed = True
            if how == "fixed":
                await self.stack.matches.fix(match_id, self.admin_id, loser)
            elif how == "void_after":
                await self.stack.matches.void(match_id, self.admin_id, "resultado errado")
                confirmed = False
        if confirmed:
            await self.add_stats(match_id, sides)
        return match_id

    async def add_stats(self, match_id: int, sides: dict[str, list[int]]) -> None:
        """Champion and KDA for a confirmed game: validated, pending or missing (per player)."""
        matches = self.stack.matches
        roles = {p.player_id: p.role for p in await self.stack.match_repo.participants(match_id)}
        for discord_id in (*sides["blue"], *sides["red"]):
            role = roles[self.pid_of[discord_id]]
            seen = self.stats_seen[discord_id] = self.stats_seen.get(discord_id, -1) + 1
            if discord_id == self.admin_id:  # the admin sees every state, game after game
                status = ("validated", "validated", "pending", "none")[seen % 4]
            else:
                status = self.rng.choices(("validated", "pending", "none"), (5, 2, 3))[0]
            if status == "none":
                continue
            favourites = CHAMPIONS[role]
            champion = (
                favourites[discord_id % len(favourites)]
                if self.rng.random() < 0.6
                else self.rng.choice(favourites)
            )
            kills, deaths, assists = (self.rng.randint(0, 14), self.rng.randint(0, 10),
                                      self.rng.randint(0, 18))  # fmt: skip
            if status == "pending":
                await matches.enter_stats(discord_id, champion, kills, deaths, assists, match_id)
                continue
            played = None
            if discord_id == self.admin_id and seen == 1:  # one game with a different played role
                played = next(r for r in ROLES if r != role)
            await matches.set_stats(
                self.admin_id, match_id, discord_id, champion, kills, deaths, assists,
                played_role=played,
            )  # fmt: skip

    # --- a whole history -----------------------------------------------------------------------

    async def play_history(self, games: int, newcomers: int) -> Played:
        """`games` finished games, then the three unfinished ones "now"."""
        others = [d for d in self.sims if d != self.admin_id]
        if len(others) < 9:
            raise PlaygroundError("The roster needs at least 10 players to play a game.")
        fresh = others[len(others) - newcomers :] if newcomers else []
        veterans = [d for d in others if d not in fresh]
        if len(veterans) < 9:
            raise PlaygroundError("Too many newcomers: fewer than 9 other players would be left.")
        for index in range(games):
            if index < 3 and fresh:  # newcomers only play the first games: they stay in placement
                lineup = [self.admin_id, *fresh, *self.rng.sample(veterans, 9 - len(fresh))]
            else:
                lineup = [self.admin_id, *self.rng.sample(veterans, 9)]
            spare = [d for d in veterans if d not in lineup]
            if index in (6, 15) and spare:  # nobody is always there
                lineup = [*lineup[1:], spare[0]]
            self.rng.shuffle(lineup)
            await self.play(lineup, SPECIAL.get(index, "players"))
            self.clock.advance(hours=12)

        # The unfinished games happen "now", with players that are free.
        self.clock.now = max(self.clock.now, self.real_now - timedelta(minutes=10))
        free = self.rng.sample(veterans, len(veterans))
        contested = free[:10]
        while len(contested) < 10:
            contested.append(await self.add_mock())
        self.clock.advance(minutes=2)
        await self.play(contested, "contested", minutes=1)
        reported = free[10:]
        while len(reported) < 10:
            reported.append(await self.add_mock())
        self.clock.advance(minutes=2)
        await self.play(reported, "reported", minutes=1)
        live = [self.admin_id]
        while len(live) < 10:
            live.append(await self.add_mock())
        self.clock.advance(minutes=1)
        await self.play(live, "in_progress", minutes=1)
        return Played(games, 3, self.mocks)
