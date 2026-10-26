"""SQLite connection wrapper with a tiny versioned-migration scheme."""

import logging
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

# Each entry upgrades the schema by one version (PRAGMA user_version = index + 1).
MIGRATIONS: list[str] = [
    """
    CREATE TABLE names (
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE
    );
    CREATE TABLE phrases (
        id   INTEGER PRIMARY KEY,
        text TEXT NOT NULL UNIQUE
    );
    CREATE TABLE picks (
        id        INTEGER PRIMARY KEY,
        picked_on TEXT NOT NULL UNIQUE,  -- local (Europe/Lisbon) date, YYYY-MM-DD
        name_id   INTEGER NOT NULL REFERENCES names(id),
        phrase    TEXT NOT NULL,
        picked_at TEXT NOT NULL          -- ISO timestamp with offset
    );
    """,
]


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected.")
        return self._conn

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        await conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            await conn.execute("PRAGMA journal_mode = WAL")
        self._conn = conn
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _migrate(self) -> None:
        async with self.conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        version = row[0] if row else 0
        for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
            log.info("Migrating database to version %d", target)
            await self.conn.executescript(f"BEGIN;{script};PRAGMA user_version = {target};COMMIT;")
