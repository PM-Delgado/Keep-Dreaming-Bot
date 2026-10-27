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


@dataclass(frozen=True)
class DataSnapshot:
    """Size of each table and SQLite's verdict on the file (for the production check)."""

    names: int
    phrases: int
    picks: int
    players: int  # real players
    mock_players: int
    ratings: int
    rank_signals: int
    riot_accounts: int
    matches: int
    lobbies: int
    adjustments: int
    champions: int
    integrity: str  # "ok" or what PRAGMA integrity_check reported
    foreign_key_problems: int


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

    async def snapshot(self) -> DataSnapshot:
        async with self.db.conn.execute("PRAGMA integrity_check") as cur:
            integrity = "; ".join(str(r[0]) for r in await cur.fetchall())
        async with self.db.conn.execute("PRAGMA foreign_key_check") as cur:
            foreign_key_problems = len(list(await cur.fetchall()))
        return DataSnapshot(
            names=await self._count("names"),
            phrases=await self._count("phrases"),
            picks=await self._count("picks"),
            players=await self._count("players WHERE is_mock = 0"),
            mock_players=await self._count("players WHERE is_mock = 1"),
            ratings=await self._count("ratings"),
            rank_signals=await self._count("rank_signals"),
            riot_accounts=await self._count("riot_accounts"),
            matches=await self._count("matches"),
            lobbies=await self._count("lobbies"),
            adjustments=await self._count("rating_events WHERE kind = 'adjust'"),
            champions=await self._count("champions"),
            integrity=integrity,
            foreign_key_problems=foreign_key_problems,
        )

    async def wipe_picks(self) -> int:
        """Forget which name was picked on which day (names and phrases stay). Returns the count."""
        async with self.db.transaction():
            count = await self._count("picks")
            await self.db.conn.execute("DELETE FROM picks")
        return count

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
