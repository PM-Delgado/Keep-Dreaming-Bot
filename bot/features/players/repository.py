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


@dataclass(frozen=True)
class RoleRow:
    declared: int | None
    admin_override: int | None


_COLUMNS = "id, discord_id, nickname, created_at, left_at"


def _player(row: Sequence[Any]) -> Player:
    return Player(row[0], row[1], row[2], from_db(row[3]), from_db(row[4]) if row[4] else None)


class PlayerRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, discord_id: int, nickname: str, created_at: datetime) -> Player:
        try:
            cur = await self.db.conn.execute(
                "INSERT INTO players (discord_id, nickname, created_at) VALUES (?, ?, ?)",
                (discord_id, nickname, to_db(created_at)),
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
