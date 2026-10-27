"""SQL for the public statistics: one row per player per confirmed game. Read-only."""

from dataclasses import dataclass
from datetime import datetime

from bot.db import Database
from bot.features.ratings.repository import from_db


@dataclass(frozen=True)
class GameRow:
    match_id: int
    player_id: int
    side: str
    role: str  # the role actually played (`played_role`), else the one the balancer assigned
    won: bool
    confirmed_at: datetime
    champion: str | None  # Data Dragon id
    kills: int | None
    deaths: int | None
    assists: int | None
    stats_status: str  # none | pending | validated


_COLUMNS = (
    "m.id, p.player_id, p.side, COALESCE(p.played_role, p.role), m.winner = p.side,"
    " m.confirmed_at, p.champion, p.kills, p.deaths, p.assists, p.stats_status"
)


class StatsRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def games(self, guild_id: int, player_id: int | None = None) -> list[GameRow]:
        """Confirmed games of this server, newest first; one player's, or everybody's.

        Voided, contested, reported and in-progress games are never included. A later correction
        (`/match fix`) is picked up because nothing here is cached.
        """
        where = "m.guild_id = ? AND m.status = 'confirmed'"
        args: list[int] = [guild_id]
        if player_id is not None:
            where += " AND p.player_id = ?"
            args.append(player_id)
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM match_participants p JOIN matches m ON m.id = p.match_id"
            f" WHERE {where} ORDER BY m.confirmed_at DESC, m.id DESC",
            args,
        ) as cur:
            rows = await cur.fetchall()
        return [
            GameRow(
                r[0], r[1], r[2], r[3], bool(r[4]), from_db(r[5]), r[6], r[7], r[8], r[9], r[10]
            )
            for r in rows
        ]
