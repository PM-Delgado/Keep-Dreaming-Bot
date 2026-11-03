"""Two players' ranked Solo/Duo games on the same team (`/riot duo`). Pure: no Discord, no DB.

What it counts, and what it does not claim:
* A *shared game* is a stored ranked game that an account of each player played. Two players in
  one match are **teammates** only if they were on the same team; opponents are left out.
* Same team is told by Riot's `teamId` when both rows have it (games stored since migration 17),
  else by `win`: one team wins every finished game, so equal `win` means the same team. A
  contradiction between the two is settled in favour of `teamId`.
* Being on the same team is **not proof of a premade duo**: Match-V5 has no party information.
  The wording of the embeds says "na mesma equipa".
* Remakes are never counted. A match in which one of the players has two rows (two linked accounts
  in one game: at least one of them is not that person's) is ambiguous and is skipped, and counted
  in `DuoStats.ambiguous`.
* Each account keeps only its newest games (`complete_since` says from when an account is
  complete), so the comparison with/without the other player starts at `coverage` and nothing
  before it is compared. Shared games older than that are still stored (see `prune`) and counted
  in the totals, with a note.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.features.players.proficiency import ROLES
from bot.features.riot.roles import RoleHistoryParams, position_role
from bot.features.stats.tally import Tally

LISBON = ZoneInfo("Europe/Lisbon")

MIN_PERCENT = 5  # games together before a percentage is shown (counts are always shown)
MIN_COMPARE = 10  # games with and without each, before the comparison is shown
MIN_ROLE_RECORD = 5  # games of a role pair before it can be the "best"
MIN_CHAMPION_RECORD = 3  # the same for a champion pair
RECENT_GAMES = 10
TOP_PAIRS = 3


@dataclass(frozen=True)
class DuoRow:
    """One stored ranked game of one linked account (`riot_role_games` joined to its account)."""

    player_id: int
    account_id: int
    riot_id: str
    is_main: bool
    match_id: str
    played_at: datetime
    team_position: str | None
    win: bool
    kills: int
    deaths: int
    assists: int
    duration_s: int
    early_surrender: bool
    champion: str | None
    team_id: int | None = None  # None for games stored before migration 17

    @property
    def role(self) -> str | None:
        return position_role(self.team_position)

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(self.deaths, 1)


@dataclass(frozen=True)
class DuoGame:
    """A game both played on the same team; `a` and `b` are their rows."""

    match_id: str
    played_at: datetime
    a: DuoRow
    b: DuoRow

    @property
    def won(self) -> bool:
        return self.a.win


@dataclass(frozen=True)
class Streak:
    won: bool | None = None  # None: no games
    length: int = 0


@dataclass(frozen=True)
class Run:
    """The longest winning or losing run: how many games, from when to when."""

    length: int
    first_at: datetime
    last_at: datetime


@dataclass(frozen=True)
class PairTally:
    key: tuple[str, str]  # (A's role or champion, B's)
    tally: Tally


@dataclass(frozen=True)
class PlayerKda:
    games: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0

    @property
    def ratio(self) -> float:
        return (self.kills + self.assists) / max(self.deaths, 1)

    def average(self) -> tuple[float, float, float]:
        n = max(self.games, 1)
        return self.kills / n, self.deaths / n, self.assists / n


@dataclass(frozen=True)
class WithWithout:
    """One player's record with the other on the team and without them, since the coverage date."""

    with_: Tally = Tally()
    without: Tally = Tally()

    @property
    def shown(self) -> bool:
        return self.with_.games >= MIN_COMPARE and self.without.games >= MIN_COMPARE


@dataclass(frozen=True)
class BestGame:
    """The best KDA a player had in a game together."""

    game: DuoGame
    row: DuoRow  # that player's row


@dataclass(frozen=True)
class DuoStats:
    tally: Tally = Tally()
    current: Streak = Streak()
    longest_win: Run | None = None
    longest_loss: Run | None = None
    role_pairs: tuple[PairTally, ...] = ()  # most played first
    champion_pairs: tuple[PairTally, ...] = ()
    best_role_pair: PairTally | None = None
    best_champion_pair: PairTally | None = None
    kda_a: PlayerKda = PlayerKda()
    kda_b: PlayerKda = PlayerKda()
    best_a: BestGame | None = None
    best_b: BestGame | None = None
    busiest_day: tuple[str, int] | None = None  # (Lisbon date YYYY-MM-DD, games)
    first_game: datetime | None = None
    recent: tuple[DuoGame, ...] = ()  # newest first
    coverage: datetime | None = None  # complete from here; None = not known (still syncing)
    compare_a: WithWithout | None = None  # None without a coverage date
    compare_b: WithWithout | None = None
    before_coverage: bool = False  # some shared games predate the coverage date
    ambiguous: int = 0  # matches skipped: a player had two rows in them
    syncing: tuple[str, ...] = ()  # Riot IDs whose first history sync has not finished

    @property
    def games(self) -> int:
        return self.tally.games


def is_remake(row: DuoRow) -> bool:
    return row.early_surrender or row.duration_s < RoleHistoryParams().min_duration_s


def same_team(a: DuoRow, b: DuoRow) -> bool:
    """Whether two rows of one match were on the same team (see the module docstring)."""
    if a.team_id is not None and b.team_id is not None:
        return a.team_id == b.team_id
    return a.win == b.win


def _by_match(rows: Iterable[DuoRow]) -> dict[str, list[DuoRow]]:
    grouped: dict[str, list[DuoRow]] = defaultdict(list)
    for row in rows:
        grouped[row.match_id].append(row)
    return grouped


def shared_games(rows_a: Iterable[DuoRow], rows_b: Iterable[DuoRow]) -> tuple[list[DuoGame], int]:
    """(the games they played on the same team, newest first; how many matches were skipped as
    ambiguous). Games against each other and remakes are in neither."""
    a_by, b_by = _by_match(rows_a), _by_match(rows_b)
    together: list[DuoGame] = []
    ambiguous = 0
    for match_id, a_rows in a_by.items():
        b_rows = b_by.get(match_id)
        if not b_rows:
            continue
        if len(a_rows) > 1 or len(b_rows) > 1:
            ambiguous += 1
            continue
        a, b = a_rows[0], b_rows[0]
        if is_remake(a) or is_remake(b) or not same_team(a, b):
            continue
        together.append(DuoGame(match_id, a.played_at, a, b))
    together.sort(key=lambda g: (g.played_at, g.match_id), reverse=True)
    return together, ambiguous


def _streaks(oldest_first: Sequence[DuoGame]) -> tuple[Streak, Run | None, Run | None]:
    """Current streak and the longest winning and losing runs."""
    best: dict[bool, Run | None] = {True: None, False: None}
    run: list[DuoGame] = []
    for game in oldest_first:
        if run and run[-1].won != game.won:
            run = []
        run.append(game)
        known = best[game.won]
        if known is None or len(run) > known.length:
            best[game.won] = Run(len(run), run[0].played_at, run[-1].played_at)
    current = Streak(run[-1].won, len(run)) if run else Streak()
    return current, best[True], best[False]


def _pairs(
    games: Sequence[DuoGame], key: Callable[[DuoGame], tuple[str, str] | None]
) -> list[PairTally]:
    results: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for game in games:
        k = key(game)
        if k is not None:
            results[k].append(game.won)
    pairs = [PairTally(k, Tally(sum(r), len(r) - sum(r))) for k, r in results.items()]
    return pairs


def _role_key(game: DuoGame) -> tuple[str, str] | None:
    return (game.a.role, game.b.role) if game.a.role and game.b.role else None


def _champion_key(game: DuoGame) -> tuple[str, str] | None:
    return (game.a.champion, game.b.champion) if game.a.champion and game.b.champion else None


def _role_order(pair: PairTally) -> tuple[int, int]:
    return ROLES.index(pair.key[0]), ROLES.index(pair.key[1])


def _best_pair(pairs: Sequence[PairTally], minimum: int) -> PairTally | None:
    """Highest win rate among the pairs with enough games; then more games; then the key."""
    eligible = [p for p in pairs if p.tally.games >= minimum]
    if not eligible:
        return None
    return min(eligible, key=lambda p: (-(p.tally.win_rate or 0), -p.tally.games, p.key))


def _kda(rows: Iterable[DuoRow]) -> PlayerKda:
    rows = list(rows)
    return PlayerKda(
        len(rows),
        sum(r.kills for r in rows),
        sum(r.deaths for r in rows),
        sum(r.assists for r in rows),
    )


def _best_game(games: Sequence[DuoGame], pick: Callable[[DuoGame], DuoRow]) -> BestGame | None:
    if not games:
        return None
    best = max(games, key=lambda g: (pick(g).kda, pick(g).kills, g.played_at))
    return BestGame(best, pick(best))


def _busiest_day(games: Sequence[DuoGame]) -> tuple[str, int] | None:
    days: dict[str, int] = defaultdict(int)
    for game in games:
        days[game.played_at.astimezone(LISBON).date().isoformat()] += 1
    if not days:
        return None
    day = min(days, key=lambda d: (-days[d], d))  # most games; the earlier day on a tie
    return day, days[day]


def _compare(
    own_rows: Iterable[DuoRow],
    other_match_ids: set[str],
    together: Sequence[DuoGame],
    pick: Callable[[DuoGame], DuoRow],
    since: datetime,
) -> WithWithout:
    """A player's record with and without the other, from `since`.

    "Without" is the player's own finished games from then on in which the other player has no
    row at all: games against them, and matches we can't read, are in neither column. A match in
    which the player has two rows is skipped (we can't tell which row is theirs).
    """
    with_wins = [pick(g).win for g in together if g.played_at >= since]
    without: list[bool] = []
    for match_id, rows in _by_match(r for r in own_rows if r.played_at >= since).items():
        if match_id in other_match_ids or len(rows) > 1 or is_remake(rows[0]):
            continue
        without.append(rows[0].win)
    return WithWithout(
        Tally(sum(with_wins), len(with_wins) - sum(with_wins)),
        Tally(sum(without), len(without) - sum(without)),
    )


def build_duo(
    rows_a: Sequence[DuoRow],
    rows_b: Sequence[DuoRow],
    coverage: datetime | None = None,
    syncing: Sequence[str] = (),
) -> DuoStats:
    """Everything `/riot duo` shows, from the stored rows of both players (all their accounts).

    `coverage` is the date from which every linked account of both players is complete (None when
    that is not known), `syncing` the Riot IDs whose first sync has not finished.
    """
    games, ambiguous = shared_games(rows_a, rows_b)
    compare_a = compare_b = None
    if coverage is not None:
        compare_a = _compare(rows_a, {r.match_id for r in rows_b}, games, lambda g: g.a, coverage)
        compare_b = _compare(rows_b, {r.match_id for r in rows_a}, games, lambda g: g.b, coverage)
    if not games:
        return DuoStats(
            coverage=coverage,
            compare_a=compare_a,
            compare_b=compare_b,
            ambiguous=ambiguous,
            syncing=tuple(syncing),
        )
    oldest_first = list(reversed(games))
    current, longest_win, longest_loss = _streaks(oldest_first)
    role_pairs = sorted(
        _pairs(games, _role_key),
        key=lambda p: (-p.tally.games, -p.tally.wins, _role_order(p)),
    )
    champion_pairs = sorted(
        _pairs(games, _champion_key),
        key=lambda p: (-p.tally.games, -p.tally.wins, p.key),
    )
    wins = sum(g.won for g in games)
    return DuoStats(
        tally=Tally(wins, len(games) - wins),
        current=current,
        longest_win=longest_win,
        longest_loss=longest_loss,
        role_pairs=tuple(role_pairs),
        champion_pairs=tuple(champion_pairs),
        best_role_pair=_best_pair(role_pairs, MIN_ROLE_RECORD),
        best_champion_pair=_best_pair(champion_pairs, MIN_CHAMPION_RECORD),
        kda_a=_kda(g.a for g in games),
        kda_b=_kda(g.b for g in games),
        best_a=_best_game(games, lambda g: g.a),
        best_b=_best_game(games, lambda g: g.b),
        busiest_day=_busiest_day(games),
        first_game=oldest_first[0].played_at,
        recent=tuple(games[:RECENT_GAMES]),
        coverage=coverage,
        compare_a=compare_a,
        compare_b=compare_b,
        before_coverage=coverage is not None and oldest_first[0].played_at < coverage,
        ambiguous=ambiguous,
        syncing=tuple(syncing),
    )
