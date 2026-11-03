"""Two players' shared history: rivalry (opponents) and synergy (teammates). Pure, no Discord.

Rules:
* only *confirmed* games count (the repository already leaves out voided, contested, reported and
  in-progress ones), so a `/match void` or `/match fix` shows at once;
* two players *shared* a game when both have a row for it; same side = teammates, different
  sides = opponents (a fix only changes the winner, never who played on which side);
* the role of a game is the one actually played (`played_role`), else the assigned one, so "same
  lane" and the role combinations use that role;
* champions and K/D/A appear only with validated stats (`GameRow.has_stats`);
* a percentage is shown only when its relation has at least `MIN_PAIR_PERCENT` games; the counts
  are always shown.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from bot.features.matches.repository import ROLE_ORDER
from bot.features.players.repository import Player
from bot.features.stats.repository import GameRow
from bot.features.stats.tally import Tally

MIN_PAIR_PERCENT = 5  # games in a relation before its percentage (and best combos) are shown
FORM_GAMES = 5  # the head-to-head form strip
PAIR_HISTORY_LIMIT = 10  # lines in the games tab
TOP_COMBOS = 3  # role combinations shown in the synergy tab

TAB_H2H, TAB_SYNERGY, TAB_GAMES = "h2h", "synergy", "games"
TABS = (TAB_H2H, TAB_SYNERGY, TAB_GAMES)

A, B = "a", "b"  # who holds a streak


@dataclass(frozen=True)
class SharedGame:
    """A confirmed game both players played; `a` and `b` are their rows."""

    match_id: int
    confirmed_at: datetime
    a: GameRow
    b: GameRow

    @property
    def together(self) -> bool:
        return self.a.side == self.b.side


@dataclass(frozen=True)
class Streak:
    holder: str | None  # A, B or None (no games)
    length: int = 0


@dataclass(frozen=True)
class HeadToHead:
    a_wins: int = 0
    b_wins: int = 0
    current: Streak = Streak(None)  # the wins in a row of whoever won last
    longest_a: int = 0
    longest_b: int = 0
    last: tuple[bool, ...] = ()  # newest first, True = A won, at most FORM_GAMES
    same_lane: Tally = Tally()  # from A's side, games where both played the same role

    @property
    def games(self) -> int:
        return self.a_wins + self.b_wins


@dataclass(frozen=True)
class RoleCombo:
    roles: tuple[str, str]  # (A's role, B's role)
    tally: Tally


@dataclass(frozen=True)
class Synergy:
    tally: Tally = Tally()
    combos: tuple[RoleCombo, ...] = ()  # most played first, at most TOP_COMBOS


@dataclass(frozen=True)
class PairStats:
    a: Player
    b: Player
    h2h: HeadToHead
    synergy: Synergy
    recent: tuple[SharedGame, ...]  # newest first, at most PAIR_HISTORY_LIMIT
    total: int  # every shared game, teammates or opponents


def shared_games(rows_a: Iterable[GameRow], rows_b: Iterable[GameRow]) -> list[SharedGame]:
    """The games both played, in the order of `rows_a` (newest first)."""
    by_match = {row.match_id: row for row in rows_b}
    return [
        SharedGame(row.match_id, row.confirmed_at, row, other)
        for row in rows_a
        if (other := by_match.get(row.match_id)) is not None
    ]


def head_to_head(games: Sequence[SharedGame]) -> HeadToHead:
    """The opponents' record, streaks included; `games` newest first, teammates are ignored."""
    against = [g for g in games if not g.together]
    if not against:
        return HeadToHead()
    longest = {A: 0, B: 0}
    holder, run = None, 0
    for game in reversed(against):  # oldest first, so the last run is the current one
        winner = A if game.a.won else B
        run = run + 1 if winner == holder else 1
        holder = winner
        longest[winner] = max(longest[winner], run)
    a_wins = sum(g.a.won for g in against)
    lane = [g.a.won for g in against if g.a.role == g.b.role]
    return HeadToHead(
        a_wins,
        len(against) - a_wins,
        Streak(holder, run),
        longest[A],
        longest[B],
        tuple(g.a.won for g in against[:FORM_GAMES]),
        Tally(sum(lane), len(lane) - sum(lane)),
    )


def synergy(games: Sequence[SharedGame]) -> Synergy:
    """The teammates' record and the role combinations they won or lost with."""
    together = [g for g in games if g.together]
    if not together:
        return Synergy()
    by_roles: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for game in together:
        by_roles[(game.a.role, game.b.role)].append(game.a.won)
    combos = [
        RoleCombo(roles, Tally(sum(results), len(results) - sum(results)))
        for roles, results in by_roles.items()
    ]
    combos.sort(
        key=lambda c: (
            -c.tally.games,
            -c.tally.wins,
            ROLE_ORDER.index(c.roles[0]),
            ROLE_ORDER.index(c.roles[1]),
        )
    )
    wins = sum(g.a.won for g in together)
    return Synergy(Tally(wins, len(together) - wins), tuple(combos[:TOP_COMBOS]))


def build_pair(
    a: Player, b: Player, rows_a: Sequence[GameRow], rows_b: Sequence[GameRow]
) -> PairStats:
    games = shared_games(rows_a, rows_b)
    return PairStats(
        a,
        b,
        head_to_head(games),
        synergy(games),
        tuple(games[:PAIR_HISTORY_LIMIT]),
        len(games),
    )


def initial_tab(pair: PairStats) -> str:
    """Open on the relation they share more games in; on a tie, on the confrontations."""
    return TAB_SYNERGY if pair.synergy.tally.games > pair.h2h.games else TAB_H2H
