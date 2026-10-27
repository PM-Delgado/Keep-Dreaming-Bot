"""Read-only report of the games in a database: who faced whom, and how fair it was.

Reads the file directly with a read-only connection, so it is safe while the bot is running.
It shows the balancer's own numbers (the matchmaking estimate E, in rating points), which the bot
never shows players; this is a console tool for the owner.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from bot.features.players.proficiency import ROLES

LANE_ROWS = (
    "SELECT mp.match_id, mp.side, mp.role, p.nickname, mp.est_mu, mp.h_mu_before, mp.rank_mu"
    " FROM match_participants mp JOIN players p ON p.id = mp.player_id"
    " ORDER BY mp.match_id"
)
GAME_ROWS = (
    "SELECT id, status, created_at, winner, expected_blue_win, contested FROM matches ORDER BY id"
)
NEAR_EVEN = (0.45, 0.55)
TOSS_UP = 0.52  # below this chance for the favourite, "who was the underdog" means nothing


@dataclass(frozen=True)
class Slot:
    nickname: str
    estimate: float  # E: what the balancer saw
    rank_mu: float | None  # the player's reference rank on the same scale, when known


@dataclass(frozen=True)
class Game:
    id: int
    status: str
    created_at: str
    winner: str | None
    expected_blue_win: float
    contested: bool
    blue: dict[str, Slot]
    red: dict[str, Slot]

    @property
    def counted(self) -> bool:
        """Finished with a result: the games that say something about the balance."""
        return self.status == "confirmed" and self.winner is not None

    @property
    def favourite(self) -> str:
        return "blue" if self.expected_blue_win >= 0.5 else "red"

    @property
    def favourite_chance(self) -> float:
        return max(self.expected_blue_win, 1 - self.expected_blue_win)

    def lane_gaps(self) -> dict[str, float]:
        """Blue minus red estimate per lane."""
        return {r: self.blue[r].estimate - self.red[r].estimate for r in ROLES if r in self.blue}


def load_games(path: Path, match_id: int | None = None) -> list[Game]:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        slots: dict[int, dict[str, dict[str, Slot]]] = {}
        for mid, side, role, nickname, est, _h, rank_mu in conn.execute(LANE_ROWS):
            slots.setdefault(mid, {"blue": {}, "red": {}})[side][role] = Slot(
                nickname, est, rank_mu
            )
        games = []
        for mid, status, created, winner, expected, contested in conn.execute(GAME_ROWS):
            if match_id is not None and mid != match_id:
                continue
            sides = slots.get(mid, {"blue": {}, "red": {}})
            games.append(
                Game(
                    mid,
                    status,
                    created,
                    winner,
                    expected,
                    bool(contested),
                    sides["blue"],
                    sides["red"],
                )
            )
        return games
    finally:
        conn.close()


def _name(slot: Slot | None) -> str:
    return "-" if slot is None else f"{slot.nickname} ({slot.estimate:.1f})"


def format_game(game: Game) -> str:
    state = game.status + (" (contested)" if game.contested else "")
    head = f"#{game.id}  {game.created_at[:16].replace('T', ' ')}  {state}"
    if game.winner:
        head += f"  winner: {game.winner}"
    lines = [
        head,
        f"   expected: blue {game.expected_blue_win:.0%} / red {1 - game.expected_blue_win:.0%}",
        f"   {'':<8}{'BLUE':<26}{'RED':<26}gap",
    ]
    for role in ROLES:
        blue, red = game.blue.get(role), game.red.get(role)
        gap = "" if blue is None or red is None else f"{blue.estimate - red.estimate:+.1f}"
        lines.append(f"   {role:<8}{_name(blue):<26}{_name(red):<26}{gap}")
    blue_sum = sum(s.estimate for s in game.blue.values())
    red_sum = sum(s.estimate for s in game.red.values())
    lines.append(f"   {'total':<8}{blue_sum:<26.1f}{red_sum:<26.1f}{blue_sum - red_sum:+.1f}")
    if game.counted:
        if game.favourite_chance < TOSS_UP:
            outcome = "a toss-up"
        else:
            outcome = "the favourite won" if game.winner == game.favourite else "the underdog won"
        lines.append(f"   result: {outcome}")
    return "\n".join(lines)


def summarize(games: list[Game]) -> str:
    counted = [g for g in games if g.counted]
    if not counted:
        return "No finished games to judge yet."
    decisive = [g for g in counted if g.favourite_chance >= TOSS_UP]
    favourite_wins = sum(1 for g in decisive if g.winner == g.favourite)
    near_even = sum(1 for g in counted if NEAR_EVEN[0] <= g.expected_blue_win <= NEAR_EVEN[1])
    gaps = [abs(gap) for g in counted for gap in g.lane_gaps().values()]
    worst = max(counted, key=lambda g: g.favourite_chance)
    return "\n".join(
        [
            f"Finished games judged: {len(counted)} (of {len(games)} listed)",
            f"Near-even games (expected 45-55%): {near_even} ({near_even / len(counted):.0%})",
            f"Average chance of the favourite: "
            f"{sum(g.favourite_chance for g in counted) / len(counted):.0%}",
            f"Favourite won {favourite_wins} of {len(decisive)} games that had one"
            f" (chance of 52% or more)",
            f"Average gap per lane: {sum(gaps) / len(gaps):.1f} rating points",
            f"Most lopsided game: #{worst.id} ({worst.favourite_chance:.0%} for {worst.favourite})",
        ]
    )
