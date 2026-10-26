"""SQL for lobbies. Every state change is a compare-and-set UPDATE (`WHERE status IN (...)`)
whose row count says whether this caller won, so two simultaneous interactions can never both
perform the same transition."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bot.db import Database
from bot.features.ratings.repository import from_db, to_db

OPEN, READY_CHECK, TEAMS_PROPOSED, IN_PROGRESS = (
    "OPEN",
    "READY_CHECK",
    "TEAMS_PROPOSED",
    "IN_PROGRESS",
)
CANCELLED, COMPLETED = "CANCELLED", "COMPLETED"
BLOCKING = (OPEN, READY_CHECK, TEAMS_PROPOSED)  # at most one per server (spontaneous lobbies)
ACTIVE = (OPEN, READY_CHECK, TEAMS_PROPOSED, IN_PROGRESS)  # a player can be in one of these

SIGNED_UP, READY, DROPPED = "signed_up", "ready", "dropped"


class OpenLobbyExists(Exception):
    pass


@dataclass(frozen=True)
class LobbyRow:
    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    kind: str
    status: str
    scheduled_for: datetime | None
    ready_deadline: datetime | None
    roster_json: str | None
    proposal_json: str | None
    created_by: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    cancel_reason: str | None
    reminder_stage: int
    note: str | None


@dataclass(frozen=True)
class EntryRow:
    lobby_id: int
    player_id: int
    status: str
    joined_at: datetime


_COLUMNS = (
    "id, guild_id, channel_id, message_id, kind, status, scheduled_for, ready_deadline,"
    " roster_json, proposal_json, created_by, created_at, updated_at, closed_at, cancel_reason,"
    " reminder_stage, note"
)


def _opt(value: str | None) -> datetime | None:
    return from_db(value) if value else None


def _lobby(r: Sequence[Any]) -> LobbyRow:
    return LobbyRow(
        r[0],
        r[1],
        r[2],
        r[3],
        r[4],
        r[5],
        _opt(r[6]),
        _opt(r[7]),
        r[8],
        r[9],
        r[10],
        from_db(r[11]),
        from_db(r[12]),
        _opt(r[13]),
        r[14],
        r[15],
        r[16],
    )


# Columns a transition may set alongside the status, mapped to how they are stored.
_SETTABLE = {
    "ready_deadline": to_db,
    "roster_json": str,
    "proposal_json": str,
    "closed_at": to_db,
    "cancel_reason": str,
}


class LobbyRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- lobbies ---------------------------------------------------------------------------

    async def create(
        self,
        guild_id: int,
        channel_id: int,
        kind: str,
        created_by: int,
        now: datetime,
        scheduled_for: datetime | None = None,
        note: str | None = None,
    ) -> LobbyRow:
        try:
            cur = await self.db.conn.execute(
                "INSERT INTO lobbies (guild_id, channel_id, kind, status, scheduled_for,"
                " created_by, created_at, updated_at, note)"
                " VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)",
                (
                    guild_id,
                    channel_id,
                    kind,
                    to_db(scheduled_for) if scheduled_for else None,
                    created_by,
                    to_db(now),
                    to_db(now),
                    note,
                ),
            )
        except sqlite3.IntegrityError as exc:
            await self.db.conn.rollback()
            raise OpenLobbyExists from exc
        await self.db.conn.commit()
        row = await self.get(cur.lastrowid or 0)
        assert row is not None
        return row

    async def get(self, lobby_id: int) -> LobbyRow | None:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM lobbies WHERE id = ?", (lobby_id,)
        ) as cur:
            row = await cur.fetchone()
        return _lobby(row) if row else None

    async def get_by_message(self, message_id: int) -> LobbyRow | None:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM lobbies WHERE message_id = ?", (message_id,)
        ) as cur:
            row = await cur.fetchone()
        return _lobby(row) if row else None

    async def open_for_guild(self, guild_id: int) -> LobbyRow | None:
        marks = ",".join("?" * len(BLOCKING))
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM lobbies WHERE guild_id = ? AND kind = 'spontaneous'"
            f" AND status IN ({marks})",
            (guild_id, *BLOCKING),
        ) as cur:
            row = await cur.fetchone()
        return _lobby(row) if row else None

    async def list_scheduled(self, guild_id: int) -> list[LobbyRow]:
        """Scheduled lobbies still waiting for their time (or for enough players), soonest first."""
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM lobbies WHERE guild_id = ? AND kind = 'scheduled'"
            " AND status = 'OPEN' ORDER BY scheduled_for, id",
            (guild_id,),
        ) as cur:
            return [_lobby(r) for r in await cur.fetchall()]

    async def set_reminder_stage(self, lobby_id: int, stage: int) -> bool:
        """Compare-and-set: True only for the caller that moves the stage forward."""
        cur = await self.db.conn.execute(
            "UPDATE lobbies SET reminder_stage = ? WHERE id = ? AND reminder_stage < ?",
            (stage, lobby_id, stage),
        )
        await self.db.conn.commit()
        return cur.rowcount == 1

    async def list_by_status(self, statuses: Sequence[str]) -> list[LobbyRow]:
        marks = ",".join("?" * len(statuses))
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM lobbies WHERE status IN ({marks}) ORDER BY id",
            tuple(statuses),
        ) as cur:
            return [_lobby(r) for r in await cur.fetchall()]

    async def set_message(self, lobby_id: int, message_id: int, channel_id: int | None = None):
        await self.db.conn.execute(
            "UPDATE lobbies SET message_id = ?, channel_id = COALESCE(?, channel_id) WHERE id = ?",
            (message_id, channel_id, lobby_id),
        )
        await self.db.conn.commit()

    async def transition(
        self,
        lobby_id: int,
        from_statuses: Sequence[str],
        to_status: str,
        now: datetime,
        **fields: Any,
    ) -> bool:
        """Compare-and-set: True only if the lobby was in one of `from_statuses`.

        Unlisted columns keep their value; pass `None` to clear one (e.g. ready_deadline=None).
        """
        sets = ["status = ?", "updated_at = ?"]
        args: list[Any] = [to_status, to_db(now)]
        for name, value in fields.items():
            if name not in _SETTABLE:
                raise ValueError(f"Cannot set {name!r}")
            sets.append(f"{name} = ?")
            args.append(None if value is None else _SETTABLE[name](value))
        marks = ",".join("?" * len(from_statuses))
        args += [lobby_id, *from_statuses]
        cur = await self.db.conn.execute(
            f"UPDATE lobbies SET {', '.join(sets)} WHERE id = ? AND status IN ({marks})", args
        )
        await self.db.conn.commit()
        return cur.rowcount == 1

    async def touch(self, lobby_id: int, now: datetime) -> None:
        await self.db.conn.execute(
            "UPDATE lobbies SET updated_at = ? WHERE id = ?", (to_db(now), lobby_id)
        )
        await self.db.conn.commit()

    # --- entries -----------------------------------------------------------------------------

    async def entries(self, lobby_id: int) -> list[EntryRow]:
        """Everyone who ever signed up, oldest sign-up first (dropped entries included)."""
        async with self.db.conn.execute(
            "SELECT lobby_id, player_id, status, joined_at FROM lobby_entries WHERE lobby_id = ?"
            " ORDER BY joined_at, rowid",
            (lobby_id,),
        ) as cur:
            return [EntryRow(r[0], r[1], r[2], from_db(r[3])) for r in await cur.fetchall()]

    async def join(self, lobby_id: int, player_id: int, now: datetime) -> bool:
        """Sign up, or re-join after leaving (back of the line). False if already signed up."""
        cur = await self.db.conn.execute(
            "INSERT INTO lobby_entries (lobby_id, player_id, status, joined_at)"
            " VALUES (?, ?, 'signed_up', ?)"
            " ON CONFLICT(lobby_id, player_id) DO UPDATE SET status = 'signed_up',"
            " joined_at = excluded.joined_at WHERE lobby_entries.status = 'dropped'",
            (lobby_id, player_id, to_db(now)),
        )
        await self.db.conn.commit()
        return cur.rowcount == 1

    async def set_entry_status(
        self, lobby_id: int, player_id: int, from_statuses: Sequence[str], to_status: str
    ) -> bool:
        marks = ",".join("?" * len(from_statuses))
        cur = await self.db.conn.execute(
            f"UPDATE lobby_entries SET status = ? WHERE lobby_id = ? AND player_id = ?"
            f" AND status IN ({marks})",
            (to_status, lobby_id, player_id, *from_statuses),
        )
        await self.db.conn.commit()
        return cur.rowcount == 1

    async def reset_ready(self, lobby_id: int) -> None:
        await self.db.conn.execute(
            "UPDATE lobby_entries SET status = 'signed_up' WHERE lobby_id = ? AND status = 'ready'",
            (lobby_id,),
        )
        await self.db.conn.commit()

    async def busy_lobby_of(self, player_id: int, exclude_lobby_id: int | None = None):
        """A lobby that stops the player from joining another right now: a game in progress
        (ready-check, proposed teams, started) or an open spontaneous lobby. Sign-ups to
        scheduled lobbies that have not started do not count."""
        async with self.db.conn.execute(
            f"SELECT {', '.join('l.' + c.strip() for c in _COLUMNS.split(','))}"
            " FROM lobbies l JOIN lobby_entries e ON e.lobby_id = l.id"
            " WHERE e.player_id = ? AND e.status != 'dropped' AND l.id != ? AND ("
            " l.status IN ('READY_CHECK', 'TEAMS_PROPOSED', 'IN_PROGRESS')"
            " OR (l.status = 'OPEN' AND l.kind = 'spontaneous'))"
            " ORDER BY l.id DESC LIMIT 1",
            (player_id, exclude_lobby_id or 0),
        ) as cur:
            row = await cur.fetchone()
        return _lobby(row) if row else None

    async def active_lobby_of(self, player_id: int) -> LobbyRow | None:
        """The lobby (if any) in which the player is signed up and that has not finished."""
        marks = ",".join("?" * len(ACTIVE))
        async with self.db.conn.execute(
            f"SELECT {', '.join('l.' + c.strip() for c in _COLUMNS.split(','))}"
            f" FROM lobbies l JOIN lobby_entries e ON e.lobby_id = l.id"
            f" WHERE e.player_id = ? AND e.status != 'dropped' AND l.status IN ({marks})"
            f" ORDER BY l.id DESC LIMIT 1",
            (player_id, *ACTIVE),
        ) as cur:
            row = await cur.fetchone()
        return _lobby(row) if row else None
