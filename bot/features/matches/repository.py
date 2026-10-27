"""SQL for matches. Status changes are compare-and-set UPDATEs, like lobbies."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bot.db import Database
from bot.features.ratings.matchmaking import FormGame
from bot.features.ratings.replay import MatchIn
from bot.features.ratings.repository import from_db, to_db

IN_PROGRESS, REPORTED, CONFIRMED, VOIDED = "in_progress", "reported", "confirmed", "voided"
NO_STATS, PENDING, VALIDATED = "none", "pending", "validated"
ROLE_ORDER = ("TOP", "JUNGLE", "MID", "ADC", "SUPPORT")


@dataclass(frozen=True)
class MatchRow:
    id: int
    lobby_id: int
    guild_id: int
    status: str
    winner: str | None
    expected_blue_win: float
    created_at: datetime
    reported_by: int | None
    reported_at: datetime | None
    confirmed_by: int | None
    confirmed_at: datetime | None
    auto_confirmed: bool
    contested: bool
    voided_by: int | None
    voided_at: datetime | None
    void_reason: str | None
    reminder_sent: bool


@dataclass(frozen=True)
class ParticipantRow:
    match_id: int
    player_id: int
    side: str
    role: str
    h_mu_before: float
    h_sigma_before: float
    est_mu: float
    est_sigma: float
    rank_mu: float | None
    # M3b: what the player (or an admin) said about the game. Never inputs to the ratings.
    champion: str | None = None  # Data Dragon id
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    stats_status: str = NO_STATS
    stats_set_by: int | None = None
    stats_validated_by: int | None = None
    played_role: str | None = None  # set when it differs from the role the balancer assigned

    @property
    def has_stats(self) -> bool:
        return self.champion is not None


@dataclass(frozen=True)
class AuditRow:
    match_id: int
    action: str
    actor_id: int | None
    old_value: str | None
    new_value: str | None
    created_at: datetime
    subject_id: int | None = None  # the player a stats action is about


_COLUMNS = (
    "id, lobby_id, guild_id, status, winner, expected_blue_win, created_at, reported_by,"
    " reported_at, confirmed_by, confirmed_at, auto_confirmed, contested, voided_by, voided_at,"
    " void_reason, reminder_sent"
)
_PARTICIPANT_COLUMNS = (
    "match_id, player_id, side, role, h_mu_before, h_sigma_before, est_mu, est_sigma, rank_mu,"
    " champion, kills, deaths, assists, stats_status, stats_set_by, stats_validated_by,"
    " played_role"
)
# Columns a transition may set, with the conversion applied to the value.
_SETTABLE = {
    "winner": str,
    "reported_by": int,
    "reported_at": to_db,
    "confirmed_by": int,
    "confirmed_at": to_db,
    "auto_confirmed": int,
    "contested": int,
    "voided_by": int,
    "voided_at": to_db,
    "void_reason": str,
    "reminder_sent": int,
}


def _opt(value: str | None) -> datetime | None:
    return from_db(value) if value else None


def _match(r: Sequence[Any]) -> MatchRow:
    return MatchRow(
        r[0],
        r[1],
        r[2],
        r[3],
        r[4],
        r[5],
        from_db(r[6]),
        r[7],
        _opt(r[8]),
        r[9],
        _opt(r[10]),
        bool(r[11]),
        bool(r[12]),
        r[13],
        _opt(r[14]),
        r[15],
        bool(r[16]),
    )


class MatchRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        lobby_id: int,
        guild_id: int,
        expected_blue_win: float,
        now: datetime,
        participants: Sequence[ParticipantRow],
    ) -> MatchRow:
        """Insert the match and its ten participants. Run inside `db.transaction()`."""
        cur = await self.db.conn.execute(
            "INSERT INTO matches (lobby_id, guild_id, status, expected_blue_win, created_at)"
            " VALUES (?, ?, 'in_progress', ?, ?)",
            (lobby_id, guild_id, expected_blue_win, to_db(now)),
        )
        match_id = cur.lastrowid or 0
        await self.db.conn.executemany(
            "INSERT INTO match_participants (match_id, player_id, side, role, h_mu_before,"
            " h_sigma_before, est_mu, est_sigma, rank_mu) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    match_id,
                    p.player_id,
                    p.side,
                    p.role,
                    p.h_mu_before,
                    p.h_sigma_before,
                    p.est_mu,
                    p.est_sigma,
                    p.rank_mu,
                )
                for p in participants
            ],
        )
        await self.db.conn.commit()
        row = await self.get(match_id)
        assert row is not None
        return row

    async def get(self, match_id: int) -> MatchRow | None:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM matches WHERE id = ?", (match_id,)
        ) as cur:
            row = await cur.fetchone()
        return _match(row) if row else None

    async def latest_for_guild(self, guild_id: int) -> MatchRow | None:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM matches WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
        return _match(row) if row else None

    async def get_by_lobby(self, lobby_id: int) -> MatchRow | None:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM matches WHERE lobby_id = ?", (lobby_id,)
        ) as cur:
            row = await cur.fetchone()
        return _match(row) if row else None

    async def list_by_status(self, statuses: Sequence[str]) -> list[MatchRow]:
        marks = ",".join("?" * len(statuses))
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM matches WHERE status IN ({marks}) ORDER BY id",
            tuple(statuses),
        ) as cur:
            return [_match(r) for r in await cur.fetchall()]

    async def participants(self, match_id: int) -> list[ParticipantRow]:
        async with self.db.conn.execute(
            f"SELECT {_PARTICIPANT_COLUMNS} FROM match_participants WHERE match_id = ?",
            (match_id,),
        ) as cur:
            rows = [ParticipantRow(*r) for r in await cur.fetchall()]
        return sorted(rows, key=lambda p: (p.side, ROLE_ORDER.index(p.role)))

    # --- champion and KDA (M3b) ----

    async def participant(self, match_id: int, player_id: int) -> ParticipantRow | None:
        async with self.db.conn.execute(
            f"SELECT {_PARTICIPANT_COLUMNS} FROM match_participants"
            " WHERE match_id = ? AND player_id = ?",
            (match_id, player_id),
        ) as cur:
            row = await cur.fetchone()
        return ParticipantRow(*row) if row else None

    async def latest_match_needing_stats(self, player_id: int) -> int | None:
        """The player's latest confirmed game whose stats are not validated yet."""
        async with self.db.conn.execute(
            "SELECT m.id FROM match_participants p JOIN matches m ON m.id = p.match_id"
            " WHERE p.player_id = ? AND m.status = 'confirmed' AND p.stats_status != 'validated'"
            " ORDER BY m.confirmed_at DESC, m.id DESC LIMIT 1",
            (player_id,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def write_stats(
        self,
        match_id: int,
        player_id: int,
        champion: str,
        kills: int,
        deaths: int,
        assists: int,
        status: str,
        set_by: int,
        validated_by: int | None,
    ) -> None:
        await self.db.conn.execute(
            "UPDATE match_participants SET champion = ?, kills = ?, deaths = ?, assists = ?,"
            " stats_status = ?, stats_set_by = ?, stats_validated_by = ?"
            " WHERE match_id = ? AND player_id = ?",
            (champion, kills, deaths, assists, status, set_by, validated_by, match_id, player_id),
        )
        await self.db.conn.commit()

    async def set_played_role(self, match_id: int, player_id: int, role: str | None) -> None:
        await self.db.conn.execute(
            "UPDATE match_participants SET played_role = ? WHERE match_id = ? AND player_id = ?",
            (role, match_id, player_id),
        )
        await self.db.conn.commit()

    async def validate_stats(self, match_id: int, player_id: int, validated_by: int) -> bool:
        """Compare-and-set: True only if the stats were pending."""
        cur = await self.db.conn.execute(
            "UPDATE match_participants SET stats_status = 'validated', stats_validated_by = ?"
            " WHERE match_id = ? AND player_id = ? AND stats_status = 'pending'",
            (validated_by, match_id, player_id),
        )
        await self.db.conn.commit()
        return cur.rowcount == 1

    async def pending_stats(self, guild_id: int, limit: int) -> list[ParticipantRow]:
        """Stats waiting for an admin, oldest game first (confirmed games of this server only)."""
        columns = ", ".join(f"p.{c.strip()}" for c in _PARTICIPANT_COLUMNS.split(","))
        async with self.db.conn.execute(
            f"SELECT {columns} FROM match_participants p JOIN matches m ON m.id = p.match_id"
            " WHERE m.guild_id = ? AND m.status = 'confirmed' AND p.stats_status = 'pending'"
            " ORDER BY m.id, p.player_id LIMIT ?",
            (guild_id, limit),
        ) as cur:
            return [ParticipantRow(*r) for r in await cur.fetchall()]

    async def transition(
        self, match_id: int, from_statuses: Sequence[str], to_status: str, **fields: Any
    ) -> bool:
        """Compare-and-set: True only if the match was in one of `from_statuses`.

        Unlisted columns keep their value; pass `None` to clear one.
        """
        sets = ["status = ?"]
        args: list[Any] = [to_status]
        for name, value in fields.items():
            if name not in _SETTABLE:
                raise ValueError(f"Cannot set {name!r}")
            sets.append(f"{name} = ?")
            args.append(None if value is None else _SETTABLE[name](value))
        marks = ",".join("?" * len(from_statuses))
        args += [match_id, *from_statuses]
        cur = await self.db.conn.execute(
            f"UPDATE matches SET {', '.join(sets)} WHERE id = ? AND status IN ({marks})", args
        )
        await self.db.conn.commit()
        return cur.rowcount == 1

    async def set_flag(self, match_id: int, column: str, statuses: Sequence[str]) -> bool:
        """Set a 0/1 column to 1 once (compare-and-set): True only for the caller that did it."""
        if column not in ("contested", "reminder_sent"):
            raise ValueError(f"Cannot flag {column!r}")
        marks = ",".join("?" * len(statuses))
        cur = await self.db.conn.execute(
            f"UPDATE matches SET {column} = 1 WHERE id = ? AND {column} = 0"
            f" AND status IN ({marks})",
            (match_id, *statuses),
        )
        await self.db.conn.commit()
        return cur.rowcount == 1

    async def add_audit(
        self,
        match_id: int,
        action: str,
        actor_id: int | None,
        old_value: str | None,
        new_value: str | None,
        now: datetime,
        subject_id: int | None = None,
    ) -> None:
        await self.db.conn.execute(
            "INSERT INTO match_audit"
            " (match_id, action, actor_id, old_value, new_value, created_at, subject_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (match_id, action, actor_id, old_value, new_value, to_db(now), subject_id),
        )
        await self.db.conn.commit()

    async def audit(self, match_id: int) -> list[AuditRow]:
        async with self.db.conn.execute(
            "SELECT match_id, action, actor_id, old_value, new_value, created_at, subject_id"
            " FROM match_audit WHERE match_id = ? ORDER BY id",
            (match_id,),
        ) as cur:
            return [
                AuditRow(r[0], r[1], r[2], r[3], r[4], from_db(r[5]), r[6])
                for r in await cur.fetchall()
            ]

    async def confirmed_for_replay(self) -> list[MatchIn]:
        """Confirmed matches as the rating replay wants them (ordered by confirmation time)."""
        async with self.db.conn.execute(
            "SELECT id, confirmed_at, winner FROM matches WHERE status = 'confirmed'"
            " ORDER BY confirmed_at, id"
        ) as cur:
            matches = await cur.fetchall()
        result = []
        for match_id, confirmed_at, winner in matches:
            people = await self.participants(match_id)
            result.append(
                MatchIn(
                    match_id,
                    from_db(confirmed_at),
                    winner,
                    tuple(p.player_id for p in people if p.side == "blue"),
                    tuple(p.player_id for p in people if p.side == "red"),
                )
            )
        return result

    async def recent_results(self, player_id: int, limit: int) -> list[FormGame]:
        """The player's last `limit` confirmed games, oldest first, with the win chance the bot
        predicted for their team beforehand."""
        async with self.db.conn.execute(
            "SELECT m.winner, p.side, m.expected_blue_win FROM match_participants p"
            " JOIN matches m ON m.id = p.match_id"
            " WHERE p.player_id = ? AND m.status = 'confirmed'"
            " ORDER BY m.confirmed_at DESC, m.id DESC LIMIT ?",
            (player_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            FormGame(winner == side, blue if side == "blue" else 1 - blue)
            for winner, side, blue in reversed(list(rows))
        ]

    async def delete_with_mock_players(self) -> int:
        """Delete every match that involves a mock player (test games leave no trace)."""
        async with self.db.conn.execute(
            "SELECT DISTINCT mp.match_id FROM match_participants mp"
            " JOIN players p ON p.id = mp.player_id WHERE p.is_mock = 1"
        ) as cur:
            ids = [r[0] for r in await cur.fetchall()]
        for match_id in ids:
            await self.db.conn.execute("DELETE FROM matches WHERE id = ?", (match_id,))
        await self.db.conn.commit()
        return len(ids)
