"""Public numbers: history, personal statistics and the leaderboards. Read-only, no Discord.

Rules (agreed with the owner, see docs/status.md):
* only *confirmed* games count; win/loss counts as soon as the result is confirmed;
* champion and KDA numbers use *validated* stats only (pending ones are never counted);
* the role of a game is the one actually played (`played_role`), else the assigned one;
* the KDA ratio is `(K + A) / max(1, D)` over the totals, not an average of per-game ratios;
* raw ratings are never exposed: the ladder board uses the visible tier and division only.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from bot.features.matches.repository import PENDING, ROLE_ORDER, VALIDATED
from bot.features.players.repository import Player
from bot.features.players.service import PlayerService
from bot.features.ratings.ladder import step_progress, step_rank
from bot.features.ratings.service import RatingService
from bot.features.stats.repository import GameRow, StatsRepository

MIN_BOARD_GAMES = 5  # for the win-rate and KDA boards (KDA counts validated games)
HISTORY_LIMIT = 10
TOP_BREAKDOWN = 5  # champions and champion x role lines shown in /stats

RANK, WIN_RATE, KDA = "rank", "winrate", "kda"
BOARDS = (RANK, WIN_RATE, KDA)


@dataclass(frozen=True)
class Tally:
    wins: int = 0
    losses: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games if self.games else None


@dataclass(frozen=True)
class Kda:
    """Totals over the games whose stats were validated."""

    games: int
    kills: int
    deaths: int
    assists: int

    @property
    def ratio(self) -> float:
        return (self.kills + self.assists) / max(1, self.deaths)

    @property
    def average(self) -> tuple[float, float, float]:
        n = max(1, self.games)
        return self.kills / n, self.deaths / n, self.assists / n


@dataclass(frozen=True)
class Breakdown:
    key: tuple[str, ...]  # (role,), (champion,) or (champion, role)
    tally: Tally
    kda: Kda | None


@dataclass(frozen=True)
class PlayerStats:
    player: Player
    overall: Tally
    kda: Kda | None
    pending: int  # games whose stats still wait for an admin (not counted anywhere)
    by_role: list[Breakdown] = field(default_factory=list)
    by_champion: list[Breakdown] = field(default_factory=list)  # validated games only
    by_champion_role: list[Breakdown] = field(default_factory=list)  # validated games only


@dataclass(frozen=True)
class GameLine:
    match_id: int
    won: bool
    side: str
    role: str
    confirmed_at: datetime
    champion: str | None = None  # validated stats only
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    pending: bool = False


@dataclass(frozen=True)
class History:
    player: Player
    lines: list[GameLine]
    overall: Tally  # over every confirmed game, not only the ones shown


@dataclass(frozen=True)
class BoardEntry:
    position: int  # equal scores share a position (1, 2, 2, 4)
    player: Player
    tally: Tally
    kda: Kda | None = None
    tier_key: str | None = None
    division: int | None = None
    progress: float | None = None  # rank board: 0..1 inside the step (1.0 on the top step)


def _has_stats(row: GameRow) -> bool:
    """Validated stats with every number present (the database keeps them together)."""
    return (
        row.stats_status == VALIDATED
        and row.champion is not None
        and row.kills is not None
        and row.deaths is not None
        and row.assists is not None
    )


def _game_line(row: GameRow) -> GameLine:
    line = GameLine(
        row.match_id,
        row.won,
        row.side,
        row.role,
        row.confirmed_at,
        pending=row.stats_status == PENDING,
    )
    if _has_stats(row):
        line = replace(
            line,
            champion=row.champion,
            kills=row.kills,
            deaths=row.deaths,
            assists=row.assists,
        )
    return line


def tally_of(rows: Iterable[GameRow]) -> Tally:
    wins = losses = 0
    for row in rows:
        if row.won:
            wins += 1
        else:
            losses += 1
    return Tally(wins, losses)


def kda_of(rows: Iterable[GameRow]) -> Kda | None:
    counted = [r for r in rows if _has_stats(r)]
    if not counted:
        return None
    return Kda(
        len(counted),
        sum(r.kills or 0 for r in counted),
        sum(r.deaths or 0 for r in counted),
        sum(r.assists or 0 for r in counted),
    )


def _breakdown(groups: dict[tuple[str, ...], list[GameRow]]) -> list[Breakdown]:
    items = [Breakdown(key, tally_of(rows), kda_of(rows)) for key, rows in groups.items()]
    return sorted(items, key=lambda b: (-b.tally.games, -b.tally.wins, b.key))


def _group(
    rows: Iterable[GameRow], key: Callable[[GameRow], tuple[str, ...]]
) -> dict[tuple[str, ...], list[GameRow]]:
    groups: dict[tuple[str, ...], list[GameRow]] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return groups


def _with_positions(scored: Sequence[tuple[float, BoardEntry]]) -> list[BoardEntry]:
    """Best score first, nicknames A-Z inside a tie; tied entries share the first position."""
    ordered = sorted(scored, key=lambda item: (-item[0], item[1].player.nickname.casefold()))
    entries: list[BoardEntry] = []
    last: float | None = None
    position = 0
    for index, (score, entry) in enumerate(ordered, start=1):
        if score != last:
            position, last = index, score
        entries.append(replace(entry, position=position))
    return entries


class StatsService:
    def __init__(
        self, repo: StatsRepository, players: PlayerService, ratings: RatingService
    ) -> None:
        self.repo = repo
        self.players = players
        self.ratings = ratings

    async def history(self, guild_id: int, discord_id: int, limit: int = HISTORY_LIMIT) -> History:
        player = await self.players.require(discord_id)
        rows = await self.repo.games(guild_id, player.id)
        lines = [_game_line(r) for r in rows[:limit]]
        return History(player, lines, tally_of(rows))

    async def stats(self, guild_id: int, discord_id: int) -> PlayerStats:
        player = await self.players.require(discord_id)
        rows = await self.repo.games(guild_id, player.id)
        validated = [r for r in rows if _has_stats(r)]
        return PlayerStats(
            player,
            tally_of(rows),
            kda_of(rows),
            sum(r.stats_status == PENDING for r in rows),
            by_role=sorted(
                _breakdown(_group(rows, lambda r: (r.role,))),
                key=lambda b: ROLE_ORDER.index(b.key[0]),
            ),
            by_champion=_breakdown(_group(validated, lambda r: (r.champion or "",)))[
                :TOP_BREAKDOWN
            ],
            by_champion_role=_breakdown(_group(validated, lambda r: (r.champion or "", r.role)))[
                :TOP_BREAKDOWN
            ],
        )

    async def leaderboard(self, guild_id: int, board: str) -> list[BoardEntry]:
        """One of the boards (`BOARDS`). Mock players and players who left are never listed."""
        if board not in BOARDS:
            raise ValueError(f"Unknown board {board!r}")
        by_player = _group_by_player(await self.repo.games(guild_id))
        people = await self.players.repo.get_many(list(by_player))
        eligible = {pid: p for pid, p in people.items() if not p.is_mock and p.left_at is None}
        scored: list[tuple[float, BoardEntry]] = []
        if board == RANK:
            ladder = self.ratings.ladder
            for stored in await self.ratings.repo.list_stored():
                player = eligible.get(stored.player_id)
                if player is None or not stored.placed:
                    continue
                step = step_rank(ladder, stored.tier_key, stored.division)
                progress = step_progress(ladder, stored.tier_key, stored.division, stored.mu)
                entry = BoardEntry(
                    0,
                    player,
                    tally_of(by_player.get(player.id, ())),
                    tier_key=stored.tier_key,
                    division=stored.division,
                    progress=1.0 if step is not None and progress is None else progress,
                )
                # Step first, then the rating inside it (never shown: the bar is its picture).
                scored.append((-1000.0 if step is None else step * 1000.0 + stored.mu, entry))
            return _with_positions(scored)
        for pid, player in eligible.items():
            rows = by_player[pid]
            tally, kda = tally_of(rows), kda_of(rows)
            if board == WIN_RATE and tally.games >= MIN_BOARD_GAMES:
                scored.append((tally.win_rate or 0.0, BoardEntry(0, player, tally, kda)))
            elif board == KDA and kda is not None and kda.games >= MIN_BOARD_GAMES:
                scored.append((kda.ratio, BoardEntry(0, player, tally, kda)))
        return _with_positions(scored)


def _group_by_player(rows: Iterable[GameRow]) -> dict[int, list[GameRow]]:
    groups: dict[int, list[GameRow]] = defaultdict(list)
    for row in rows:
        groups[row.player_id].append(row)
    return groups
