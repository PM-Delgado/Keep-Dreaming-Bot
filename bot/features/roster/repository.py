"""SQL for wiping the test games and what they produced. The roster itself is never touched."""

from dataclasses import dataclass
from datetime import datetime

from bot.db import Database
from bot.features.ratings.repository import to_db

# The roster's own dated rows: (table, column).
ROSTER_DATES = (
    ("rating_events", "created_at"),
    ("rank_signals", "created_at"),
    ("players", "created_at"),
    ("riot_accounts", "linked_at"),
)


@dataclass(frozen=True)
class WipeCounts:
    matches: int
    lobbies: int
    adjustments: int


@dataclass(frozen=True)
class RosterCounts:
    players: int  # real players
    riot_accounts: int
    riot_games: int
    rank_signals: int


class RosterRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def count_games(self) -> WipeCounts:
        return WipeCounts(
            await self._count("matches"),
            await self._count("lobbies"),
            await self._count("rating_events WHERE kind = 'adjust'"),
        )

    async def count_roster(self) -> RosterCounts:
        return RosterCounts(
            await self._count("players WHERE is_mock = 0"),
            await self._count("riot_accounts"),
            await self._count("riot_role_games"),
            await self._count("rank_signals"),
        )

    async def date_roster_before(self, moment: datetime) -> None:
        """Make no roster row newer than `moment`, so test games can be dated after them.

        Games must come after the players' seed ratings in the replay timeline, and a roster just
        seeded is dated "now": the games' history is in the past. Only newer rows are moved.
        """
        stamp = to_db(moment)
        async with self.db.transaction():
            for table, column in ROSTER_DATES:
                await self.db.conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {column} > ?", (stamp, stamp)
                )

    async def wipe_games(self) -> None:
        """Delete every match, lobby and rating adjustment (the seeds from ranks stay).

        The ratings caches are rebuilt by the caller afterwards.
        """
        async with self.db.transaction():
            conn = self.db.conn
            await conn.execute("DELETE FROM match_audit")
            await conn.execute("DELETE FROM match_participants")
            await conn.execute("DELETE FROM matches")
            await conn.execute("DELETE FROM lobby_entries")
            await conn.execute("DELETE FROM lobbies")
            await conn.execute("DELETE FROM rating_history")
            await conn.execute("DELETE FROM rating_events WHERE kind = 'adjust'")

    async def wipe_league(self) -> None:
        """Delete everything the in-house system stores: games, lobbies, ratings, ranks, Riot
        accounts and their history, role proficiency and the players themselves.

        What is not league data stays: names, phrases and picks (/puta, /putaindor), the
        Data Dragon champion list and the uploaded champion and role icon ids.
        """
        async with self.db.transaction():
            conn = self.db.conn
            for table in (
                "match_audit",
                "match_participants",
                "matches",
                "lobby_entries",
                "lobbies",
                "rating_history",
                "rating_events",
                "rank_signals",
                "ratings",
                "riot_role_games",
                "riot_accounts",
                "player_roles",
                "players",
            ):
                await conn.execute(f"DELETE FROM {table}")

    async def _count(self, table: str) -> int:
        async with self.db.conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            row = await cur.fetchone()
        return row[0] if row else 0
