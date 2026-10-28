"""SQL for linked Riot accounts and their ranked-game observations."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bot.db import Database
from bot.features.ratings.repository import from_db, to_db
from bot.features.riot.client import RankedGame
from bot.features.riot.roles import GameObservation


class PuuidAlreadyLinked(Exception):
    pass


@dataclass(frozen=True)
class AccountRow:
    id: int
    player_id: int
    puuid: str
    game_name: str
    tag_line: str
    platform: str
    is_main: bool
    history_synced_at: datetime | None
    linked_by: int
    linked_at: datetime

    @property
    def riot_id(self) -> str:
        return f"{self.game_name}#{self.tag_line}"


@dataclass(frozen=True)
class StoredGame:
    """One stored ranked Solo/Duo game, with the account (Riot ID) it was played on."""

    match_id: str
    riot_id: str
    played_at: datetime
    team_position: str | None
    individual_position: str | None
    win: bool
    kills: int
    deaths: int
    assists: int
    duration_s: int
    early_surrender: bool
    champion: str | None
    cs: int | None
    damage: int | None
    gold: int | None
    vision_score: int | None
    is_main: bool = False  # played on the player's main account


_COLUMNS = (
    "id, player_id, puuid, game_name, tag_line, platform, is_main, history_synced_at,"
    " linked_by, linked_at"
)


def _account(r: Sequence[Any]) -> AccountRow:
    return AccountRow(
        r[0],
        r[1],
        r[2],
        r[3],
        r[4],
        r[5],
        bool(r[6]),
        from_db(r[7]) if r[7] else None,
        r[8],
        from_db(r[9]),
    )


class RiotRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add_account(
        self,
        player_id: int,
        puuid: str,
        game_name: str,
        tag_line: str,
        platform: str,
        is_main: bool,
        linked_by: int,
        linked_at: datetime,
    ) -> AccountRow:
        try:
            cur = await self.db.conn.execute(
                "INSERT INTO riot_accounts (player_id, puuid, game_name, tag_line, platform,"
                " is_main, linked_by, linked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    player_id,
                    puuid,
                    game_name,
                    tag_line,
                    platform,
                    int(is_main),
                    linked_by,
                    to_db(linked_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            await self.db.conn.rollback()
            raise PuuidAlreadyLinked from exc
        await self.db.conn.commit()
        row = await self.get(cur.lastrowid or 0)
        assert row is not None
        return row

    async def get(self, account_id: int) -> AccountRow | None:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM riot_accounts WHERE id = ?", (account_id,)
        ) as cur:
            row = await cur.fetchone()
        return _account(row) if row else None

    async def get_by_puuid(self, puuid: str) -> AccountRow | None:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM riot_accounts WHERE puuid = ?", (puuid,)
        ) as cur:
            row = await cur.fetchone()
        return _account(row) if row else None

    async def list_accounts(self, player_id: int) -> list[AccountRow]:
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM riot_accounts WHERE player_id = ? ORDER BY is_main DESC, id",
            (player_id,),
        ) as cur:
            return [_account(r) for r in await cur.fetchall()]

    async def list_unsynced(self) -> list[AccountRow]:
        """Accounts whose first full history sync never finished (error, restart, ...)."""
        async with self.db.conn.execute(
            f"SELECT {_COLUMNS} FROM riot_accounts WHERE history_synced_at IS NULL ORDER BY id"
        ) as cur:
            return [_account(r) for r in await cur.fetchall()]

    async def player_ids_with_accounts(self) -> list[int]:
        async with self.db.conn.execute(
            "SELECT DISTINCT player_id FROM riot_accounts ORDER BY player_id"
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def main_puuid(self, player_id: int) -> str | None:
        async with self.db.conn.execute(
            "SELECT puuid FROM riot_accounts WHERE player_id = ? AND is_main = 1", (player_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_main(self, player_id: int, account_id: int) -> None:
        """Clear then set: a single UPDATE could trip the one-main index mid-statement. If this
        is ever interrupted between the two, the player just has no main until it is re-run."""
        conn = self.db.conn
        await conn.execute("UPDATE riot_accounts SET is_main = 0 WHERE player_id = ?", (player_id,))
        await conn.execute(
            "UPDATE riot_accounts SET is_main = 1 WHERE id = ? AND player_id = ?",
            (account_id, player_id),
        )
        await conn.commit()

    async def update_identity(self, account_id: int, game_name: str, tag_line: str) -> None:
        await self.db.conn.execute(
            "UPDATE riot_accounts SET game_name = ?, tag_line = ? WHERE id = ?",
            (game_name, tag_line, account_id),
        )
        await self.db.conn.commit()

    async def delete_account(self, account_id: int) -> None:
        """Hard delete; its role games go with it (ON DELETE CASCADE)."""
        await self.db.conn.execute("DELETE FROM riot_accounts WHERE id = ?", (account_id,))
        await self.db.conn.commit()

    async def mark_synced(self, account_id: int, when: datetime) -> None:
        await self.db.conn.execute(
            "UPDATE riot_accounts SET history_synced_at = ? WHERE id = ?", (to_db(when), account_id)
        )
        await self.db.conn.commit()

    # --- ranked games ----------------------------------------------------------------------

    async def known_match_ids(self, account_id: int) -> set[str]:
        async with self.db.conn.execute(
            "SELECT riot_match_id FROM riot_role_games WHERE riot_account_id = ?", (account_id,)
        ) as cur:
            return {r[0] for r in await cur.fetchall()}

    async def add_game(self, account_id: int, game: RankedGame) -> None:
        await self.db.conn.execute(
            "INSERT OR IGNORE INTO riot_role_games (riot_account_id, riot_match_id, played_at,"
            " team_position, individual_position, win, kills, deaths, assists, duration_s,"
            " early_surrender, champion, cs, damage, gold, vision_score)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                game.match_id,
                to_db(game.played_at),
                game.team_position,
                game.individual_position,
                int(game.win),
                game.kills,
                game.deaths,
                game.assists,
                game.duration_s,
                int(game.early_surrender),
                game.champion,
                game.cs,
                game.damage,
                game.gold,
                game.vision_score,
            ),
        )
        await self.db.conn.commit()

    async def prune(self, account_id: int, now: datetime, window_days: int, keep: int) -> None:
        cutoff = to_db(now - timedelta(days=window_days))
        await self.db.conn.execute(
            "DELETE FROM riot_role_games WHERE riot_account_id = ? AND (played_at < ? OR"
            " riot_match_id NOT IN (SELECT riot_match_id FROM riot_role_games"
            " WHERE riot_account_id = ? ORDER BY played_at DESC LIMIT ?))",
            (account_id, cutoff, account_id, keep),
        )
        await self.db.conn.commit()

    async def count_games(self, account_id: int) -> int:
        async with self.db.conn.execute(
            "SELECT COUNT(*) FROM riot_role_games WHERE riot_account_id = ?", (account_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def recent_games(
        self, player_id: int, limit: int, account_id: int | None = None
    ) -> list["StoredGame"]:
        """Newest stored games across the player's accounts (or one account), newest first."""
        sql = (
            "SELECT g.riot_match_id, a.game_name, a.tag_line, g.played_at, g.team_position,"
            " g.individual_position, g.win, g.kills, g.deaths, g.assists, g.duration_s,"
            " g.early_surrender, g.champion, g.cs, g.damage, g.gold, g.vision_score,"
            " a.is_main"
            " FROM riot_role_games g JOIN riot_accounts a ON a.id = g.riot_account_id"
            " WHERE a.player_id = ?"
        )
        args: list[Any] = [player_id]
        if account_id is not None:
            sql += " AND a.id = ?"
            args.append(account_id)
        sql += " ORDER BY g.played_at DESC, g.riot_match_id DESC LIMIT ?"
        args.append(limit)
        async with self.db.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [
            StoredGame(
                match_id=r[0],
                riot_id=f"{r[1]}#{r[2]}",
                played_at=from_db(r[3]),
                team_position=r[4],
                individual_position=r[5],
                win=bool(r[6]),
                kills=r[7],
                deaths=r[8],
                assists=r[9],
                duration_s=r[10],
                early_surrender=bool(r[11]),
                champion=r[12],
                cs=r[13],
                damage=r[14],
                gold=r[15],
                vision_score=r[16],
                is_main=bool(r[17]),
            )
            for r in rows
        ]

    async def champion_pool(self, player_id: int, limit: int) -> list[tuple[str, int]]:
        """(champion, games) of the stored ranked games across the accounts, most played first."""
        async with self.db.conn.execute(
            "SELECT g.champion, COUNT(*) AS n FROM riot_role_games g"
            " JOIN riot_accounts a ON a.id = g.riot_account_id"
            " WHERE a.player_id = ? AND g.champion IS NOT NULL"
            " GROUP BY g.champion ORDER BY n DESC, g.champion LIMIT ?",
            (player_id, limit),
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    async def observations(
        self, player_id: int, now: datetime, window_days: int, per_account: int
    ) -> list[GameObservation]:
        """Newest `per_account` games of each of the player's accounts inside the window."""
        cutoff = to_db(now - timedelta(days=window_days))
        async with self.db.conn.execute(
            "SELECT played_at, team_position, individual_position, duration_s, early_surrender"
            " FROM (SELECT g.*, ROW_NUMBER() OVER (PARTITION BY g.riot_account_id"
            " ORDER BY g.played_at DESC) AS rn FROM riot_role_games g"
            " JOIN riot_accounts a ON a.id = g.riot_account_id"
            " WHERE a.player_id = ? AND g.played_at >= ?) WHERE rn <= ?",
            (player_id, cutoff, per_account),
        ) as cur:
            rows = await cur.fetchall()
        return [GameObservation(from_db(r[0]), r[1], r[2], r[3], bool(r[4])) for r in rows]
