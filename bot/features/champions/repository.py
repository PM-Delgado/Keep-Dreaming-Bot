"""SQL for the cached champion list."""

import asyncio
import json
from pathlib import Path

from bot.db import Database
from bot.features.champions.client import Champion, ChampionList

SEED_FILE = Path(__file__).parent / "seed" / "champions.json"


class ChampionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def seed_if_empty(self, seed_file: Path = SEED_FILE) -> bool:
        """Load the bundled list into an empty table (so autocomplete works offline)."""
        async with self.db.conn.execute("SELECT COUNT(*) FROM champions") as cur:
            row = await cur.fetchone()
        if row and row[0]:
            return False
        data = json.loads(await asyncio.to_thread(seed_file.read_text, encoding="utf-8"))
        await self.replace_all(
            ChampionList(
                data["patch"], tuple(Champion(c["id"], c["name"]) for c in data["champions"])
            )
        )
        return True

    async def current_patch(self) -> str | None:
        async with self.db.conn.execute("SELECT patch FROM champions LIMIT 1") as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def replace_all(self, champions: ChampionList) -> None:
        """Swap the whole list for `champions`, all or nothing."""
        async with self.db.transaction():
            await self.db.conn.execute("DELETE FROM champions")
            await self.db.conn.executemany(
                "INSERT INTO champions (id, name, patch) VALUES (?, ?, ?)",
                [(c.id, c.name, champions.patch) for c in champions.champions],
            )
            await self.db.conn.commit()

    async def all(self) -> list[Champion]:
        async with self.db.conn.execute("SELECT id, name FROM champions ORDER BY name") as cur:
            return [Champion(r[0], r[1]) for r in await cur.fetchall()]

    async def emojis(self, ids: list[str] | None = None) -> dict[str, str]:
        """Champion id -> the emoji markup (`<:name:id>`) of every uploaded icon (or just `ids`)."""
        sql = "SELECT champion_id, emoji_name, emoji_id FROM champion_emojis"
        args: list[str] = []
        if ids is not None:
            if not ids:
                return {}
            sql += f" WHERE champion_id IN ({','.join('?' * len(ids))})"
            args = ids
        async with self.db.conn.execute(sql, args) as cur:
            return {r[0]: f"<:{r[1]}:{r[2]}>" for r in await cur.fetchall()}

    async def uploaded(self) -> set[str]:
        async with self.db.conn.execute("SELECT champion_id FROM champion_emojis") as cur:
            return {r[0] for r in await cur.fetchall()}

    async def save_emoji(self, champion_id: str, emoji_id: int, emoji_name: str) -> None:
        await self.db.conn.execute(
            "INSERT OR REPLACE INTO champion_emojis (champion_id, emoji_id, emoji_name)"
            " VALUES (?, ?, ?)",
            (champion_id, emoji_id, emoji_name),
        )
        await self.db.conn.commit()

    async def names(self, ids: list[str]) -> dict[str, str]:
        """Display names for the given ids (unknown ids are left out)."""
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        async with self.db.conn.execute(
            f"SELECT id, name FROM champions WHERE id IN ({marks})", ids
        ) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}
