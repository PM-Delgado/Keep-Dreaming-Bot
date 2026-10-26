"""SQL for players and their persistent role proficiency."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bot.db import Database
from bot.features.ratings.repository import from_db, to_db


class NicknameTaken(Exception):
    pass


class AlreadyRegistered(Exception):
    pass


@dataclass(frozen=True)
class Player:
    id: int
    discord_id: int
    nickname: str
    created_at: datetime
    left_at: datetime | None
    is_mock: bool = False  # a fake player for testing lobbies; never a real Discord user


@dataclass(frozen=True)
class RoleRow:
    declared: int | None
    admin_override: int | None


_COLUMNS = "id, discord_id, nickname, created_at, left_at, is_mock"


def _player(row: Sequence[Any]) -> Player:
    return Player(
        row[0], row[1], row[2], from_db(row[3]), from_db(row[4]) if row[4] else None, bool(row[5])
    )


class PlayerRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self, discord_id: int, nickname: str, created_at: datetime, is_mock: bool = False
    ) -> Player:
        try:
            cur = await self.db.conn.execute(
                "INSERT INTO players (discord_id, nickname, created_at, is_mock)"
                " VALUES (?, ?, ?, ?)",
                (discord_id, nickname, to_db(created_at), int(is_mock)),
            )
        except sqlite3.IntegrityError as exc:
            await self.db.conn.rollback()
            if "players.discord_id" in str(exc):
                raise AlreadyRegistered from exc
            raise NicknameTaken from exc
        await self.db.conn.commit()
        player = await self.get(cur.lastrowid or 0)
        assert player is not None
        return player

    async def get(self, player_id: int) -> Player | None:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM players WHERE id = ?", (player_id,)
        ) as cur:
            row = await cur.fetchone()
        return _player(row) if row else None

    async def get_many(self, player_ids: Sequence[int]) -> dict[int, Player]:
        if not player_ids:
            return {}
        marks = ",".join("?" * len(player_ids))
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM players WHERE id IN ({marks})", tuple(player_ids)
        ) as cur:
            return {r[0]: _player(r) for r in await cur.fetchall()}

    async def list_mock(self) -> list[Player]:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM players WHERE is_mock = 1 ORDER BY id"
        ) as cur:
            return [_player(r) for r in await cur.fetchall()]

    async def delete_mock_players(self) -> int:
        """Remove every mock player and the rows that hang off them. Real players are untouched.

        Callers must first make sure no active lobby contains a mock (see MockPlayers.clear).
        """
        mock = "(SELECT id FROM players WHERE is_mock = 1)"
        conn = self.db.conn
        async with conn.execute("SELECT COUNT(*) FROM players WHERE is_mock = 1") as cur:
            row = await cur.fetchone()
        count = row[0] if row else 0
        for table in (
            "lobby_entries",
            "rating_history",
            "rating_events",
            "rank_signals",
            "ratings",
            "player_roles",
        ):
            await conn.execute(f"DELETE FROM {table} WHERE player_id IN {mock}")
        await conn.execute("DELETE FROM players WHERE is_mock = 1")
        await conn.commit()
        return count

    async def get_by_discord(self, discord_id: int) -> Player | None:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM players WHERE discord_id = ?", (discord_id,)
        ) as cur:
            row = await cur.fetchone()
        return _player(row) if row else None

    async def set_left(self, player_id: int, when: datetime | None) -> None:
        """Soft-leave only: history references players, so rows are never deleted."""
        await self.db.conn.execute(
            "UPDATE players SET left_at = ? WHERE id = ?",
            (to_db(when) if when else None, player_id),
        )
        await self.db.conn.commit()

    async def roles(self, player_id: int) -> dict[str, RoleRow]:
        async with self.db.conn.execute(
            "SELECT role, declared, admin_override FROM player_roles WHERE player_id = ?",
            (player_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {r[0]: RoleRow(r[1], r[2]) for r in rows}

    async def set_declared(
        self, player_id: int, role: str, value: int | None, when: datetime
    ) -> None:
        await self.db.conn.execute(
            "INSERT INTO player_roles (player_id, role, declared, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(player_id, role) DO UPDATE SET declared = excluded.declared,"
            " updated_at = excluded.updated_at",
            (player_id, role, value, to_db(when)),
        )
        await self.db.conn.commit()

    async def set_admin_override(
        self, player_id: int, role: str, value: int | None, when: datetime
    ) -> None:
        await self.db.conn.execute(
            "INSERT INTO player_roles (player_id, role, admin_override, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(player_id, role) DO UPDATE SET"
            " admin_override = excluded.admin_override, updated_at = excluded.updated_at",
            (player_id, role, value, to_db(when)),
        )
        await self.db.conn.commit()
