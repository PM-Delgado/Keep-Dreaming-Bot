"""SQL access for the name-of-the-day feature."""

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from bot.db import Database

SEED_DIR = Path(__file__).parent / "seed"


@dataclass(frozen=True)
class Pick:
    day: date
    name: str
    phrase: str


class NameDayRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _count(self, table: str) -> int:
        async with self.db.conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # noqa: S608
            row = await cur.fetchone()
        return row[0] if row else 0

    async def seed_if_empty(self, seed_dir: Path = SEED_DIR) -> None:
        """Load the initial names and phrases into empty tables."""
        conn = self.db.conn
        if await self._count("names") == 0:
            names = json.loads((seed_dir / "names.json").read_text(encoding="utf-8"))
            await conn.executemany(
                "INSERT OR IGNORE INTO names (name) VALUES (?)", [(n,) for n in names]
            )
        if await self._count("phrases") == 0:
            phrases = json.loads((seed_dir / "phrases.json").read_text(encoding="utf-8"))
            await conn.executemany(
                "INSERT OR IGNORE INTO phrases (text) VALUES (?)", [(p,) for p in phrases]
            )
        await conn.commit()

    async def list_names(self) -> list[tuple[int, str]]:
        async with self.db.conn.execute("SELECT id, name FROM names ORDER BY id") as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    async def list_phrases(self) -> list[str]:
        async with self.db.conn.execute("SELECT text FROM phrases ORDER BY id") as cur:
            return [r[0] for r in await cur.fetchall()]

    async def get_pick(self, day: date) -> Pick | None:
        sql = (
            "SELECT n.name, p.phrase FROM picks p JOIN names n ON n.id = p.name_id "
            "WHERE p.picked_on = ?"
        )
        async with self.db.conn.execute(sql, (day.isoformat(),)) as cur:
            row = await cur.fetchone()
        return Pick(day, row[0], row[1]) if row else None

    async def add_pick(self, day: date, name_id: int, phrase: str, picked_at: datetime) -> None:
        """Record the pick for `day`; a no-op if that day already has one."""
        await self.db.conn.execute(
            "INSERT OR IGNORE INTO picks (picked_on, name_id, phrase, picked_at) "
            "VALUES (?, ?, ?, ?)",
            (day.isoformat(), name_id, phrase, picked_at.isoformat()),
        )
        await self.db.conn.commit()

    async def leaderboard(self, limit: int) -> list[tuple[str, int]]:
        sql = (
            "SELECT n.name, COUNT(*) AS c FROM picks p JOIN names n ON n.id = p.name_id "
            "GROUP BY n.id ORDER BY c DESC, n.name LIMIT ?"
        )
        async with self.db.conn.execute(sql, (limit,)) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]
