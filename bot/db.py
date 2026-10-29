"""SQLite connection wrapper with a tiny versioned-migration scheme."""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

import aiosqlite
from aiosqlite.context import contextmanager

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
    # 2: in-house roster, role proficiency, rating inputs (events, rank signals) and the
    # rebuildable rating caches. `rating_events` and `rank_signals` are authoritative; `ratings`
    # and `rating_history` can always be rebuilt by replay.
    """
    CREATE TABLE players (
        id         INTEGER PRIMARY KEY,
        discord_id INTEGER NOT NULL UNIQUE,
        nickname   TEXT NOT NULL UNIQUE COLLATE NOCASE,
        created_at TEXT NOT NULL,
        left_at    TEXT
    );
    CREATE TABLE player_roles (
        player_id      INTEGER NOT NULL REFERENCES players(id),
        role           TEXT NOT NULL CHECK (role IN ('TOP','JUNGLE','MID','ADC','SUPPORT')),
        declared       INTEGER CHECK (declared BETWEEN 1 AND 5),
        admin_override INTEGER CHECK (admin_override BETWEEN 1 AND 5),
        updated_at     TEXT NOT NULL,
        PRIMARY KEY (player_id, role)
    );
    CREATE TABLE rating_events (
        id         INTEGER PRIMARY KEY,
        player_id  INTEGER NOT NULL REFERENCES players(id),
        kind       TEXT NOT NULL CHECK (kind IN ('seed','adjust')),
        mu         REAL NOT NULL,
        sigma      REAL NOT NULL,
        source     TEXT NOT NULL CHECK (source IN ('riot','self_reported','default','admin')),
        detail     TEXT,  -- never contains a Riot ID
        created_at TEXT NOT NULL,
        created_by INTEGER  -- Discord user ID of whoever triggered it
    );
    CREATE TABLE rank_signals (
        id          INTEGER PRIMARY KEY,
        player_id   INTEGER NOT NULL REFERENCES players(id),
        source      TEXT NOT NULL CHECK (source IN ('riot','self_reported','admin')),
        status      TEXT NOT NULL CHECK (status IN ('ranked','unranked','cleared')),
        account_key TEXT,  -- puuid, riot source only; no FK (riot_accounts arrives later)
        queue_type  TEXT,
        tier        TEXT,
        division    TEXT,
        lp          INTEGER,
        detail      TEXT,
        created_at  TEXT NOT NULL,
        created_by  INTEGER,
        CHECK ((status = 'ranked') = (tier IS NOT NULL)),
        CHECK (status != 'cleared' OR source = 'admin'),
        CHECK (status != 'unranked' OR source = 'riot')
    );
    CREATE INDEX rank_signals_by_player ON rank_signals(player_id);
    CREATE TABLE ratings (
        player_id      INTEGER PRIMARY KEY REFERENCES players(id),
        mu             REAL NOT NULL,
        sigma          REAL NOT NULL,
        games          INTEGER NOT NULL,
        placed         INTEGER NOT NULL,
        tier_key       TEXT,
        division       INTEGER,
        algo_version   TEXT NOT NULL,
        ladder_version INTEGER NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE TABLE rating_history (
        id             INTEGER PRIMARY KEY,
        player_id      INTEGER NOT NULL REFERENCES players(id),
        event_id       INTEGER REFERENCES rating_events(id),
        match_id       INTEGER,  -- no FK: matches arrive in a later migration
        mu_before      REAL,
        sigma_before   REAL,
        mu_after       REAL NOT NULL,
        sigma_after    REAL NOT NULL,
        placed         INTEGER NOT NULL,
        tier_key       TEXT,
        division       INTEGER,
        algo_version   TEXT NOT NULL,
        ladder_version INTEGER NOT NULL,
        CHECK ((event_id IS NULL) != (match_id IS NULL))
    );
    CREATE INDEX rating_history_by_player ON rating_history(player_id);
    """,
    # 3: optional Riot integration. Unlinking an account deletes its row, its role games (cascade)
    # and its Riot rank signals, so nothing references a removed puuid.
    """
    CREATE TABLE riot_accounts (
        id                INTEGER PRIMARY KEY,
        player_id         INTEGER NOT NULL REFERENCES players(id),
        puuid             TEXT NOT NULL UNIQUE,
        game_name         TEXT NOT NULL,
        tag_line          TEXT NOT NULL,
        platform          TEXT NOT NULL,
        is_main           INTEGER NOT NULL DEFAULT 0 CHECK (is_main IN (0, 1)),
        history_synced_at TEXT,
        linked_by         INTEGER NOT NULL,  -- Discord user ID of whoever linked it
        linked_at         TEXT NOT NULL
    );
    CREATE UNIQUE INDEX riot_accounts_one_main ON riot_accounts(player_id) WHERE is_main = 1;
    CREATE INDEX riot_accounts_by_player ON riot_accounts(player_id);
    CREATE TABLE riot_role_games (
        riot_account_id     INTEGER NOT NULL REFERENCES riot_accounts(id) ON DELETE CASCADE,
        riot_match_id       TEXT NOT NULL,
        played_at           TEXT NOT NULL,
        team_position       TEXT,
        individual_position TEXT,
        win                 INTEGER NOT NULL,
        kills               INTEGER NOT NULL,
        deaths              INTEGER NOT NULL,
        assists             INTEGER NOT NULL,
        duration_s          INTEGER NOT NULL,
        early_surrender     INTEGER NOT NULL DEFAULT 0,
        champion            TEXT,
        PRIMARY KEY (riot_account_id, riot_match_id)
    );
    """,
    # 4: extra per-game stats for /riot history. Games stored before this have NULLs.
    """
    ALTER TABLE riot_role_games ADD COLUMN cs INTEGER;
    ALTER TABLE riot_role_games ADD COLUMN damage INTEGER;
    ALTER TABLE riot_role_games ADD COLUMN gold INTEGER;
    ALTER TABLE riot_role_games ADD COLUMN vision_score INTEGER;
    """,
    # 5: lobbies (M2). Operational state, not history: every transition is a compare-and-set
    # UPDATE, and the one-open-lobby rule is a partial unique index. `is_mock` marks the fake
    # players used to test lobbies on a server with too few real people.
    """
    ALTER TABLE players ADD COLUMN is_mock INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE lobbies (
        id             INTEGER PRIMARY KEY,
        guild_id       INTEGER NOT NULL,
        channel_id     INTEGER NOT NULL,
        message_id     INTEGER,
        kind           TEXT NOT NULL CHECK (kind IN ('spontaneous', 'scheduled')),
        status         TEXT NOT NULL CHECK (status IN
                       ('OPEN', 'READY_CHECK', 'TEAMS_PROPOSED', 'IN_PROGRESS', 'CANCELLED',
                        'COMPLETED')),
        scheduled_for  TEXT,
        ready_deadline TEXT,
        roster_json    TEXT,  -- player ids of the current roster (first ten sign-ups)
        proposal_json  TEXT,  -- the team proposal being shown or confirmed
        created_by     INTEGER NOT NULL,  -- Discord user ID
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        closed_at      TEXT,
        cancel_reason  TEXT
    );
    CREATE UNIQUE INDEX lobbies_one_open_spontaneous ON lobbies(guild_id)
        WHERE kind = 'spontaneous' AND status IN ('OPEN', 'READY_CHECK', 'TEAMS_PROPOSED');
    CREATE UNIQUE INDEX lobbies_by_message ON lobbies(message_id) WHERE message_id IS NOT NULL;
    CREATE TABLE lobby_entries (
        lobby_id  INTEGER NOT NULL REFERENCES lobbies(id),
        player_id INTEGER NOT NULL REFERENCES players(id),
        status    TEXT NOT NULL CHECK (status IN ('signed_up', 'ready', 'dropped')),
        joined_at TEXT NOT NULL,
        PRIMARY KEY (lobby_id, player_id)
    );
    CREATE TABLE lobby_entry_roles (
        lobby_id  INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        role      TEXT NOT NULL CHECK (role IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')),
        pref      TEXT NOT NULL CHECK (pref IN ('prefer', 'ok', 'avoid', 'no')),
        PRIMARY KEY (lobby_id, player_id, role),
        FOREIGN KEY (lobby_id, player_id) REFERENCES lobby_entries(lobby_id, player_id)
    );
    """,
    # 6: players never choose roles per lobby (roles come from their profile proficiency), so
    # the per-lobby preference table from migration 5 is not needed.
    """
    DROP TABLE lobby_entry_roles;
    """,
    # 7: scheduled lobbies (M2b). `reminder_stage` makes reminders idempotent across restarts:
    # 0 = nothing sent, 1 = the "starts in 30 min" reminder, 2 = the "time is up" notice.
    """
    ALTER TABLE lobbies ADD COLUMN reminder_stage INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE lobbies ADD COLUMN note TEXT;
    """,
    # 8: matches (M3a). A match is created when a lobby's teams are confirmed. The result is
    # reported by a participant and confirmed by someone on the other team (or an admin, or
    # automatically after a quiet period); only confirmed matches feed the ratings. `h_*`/`est_*`/
    # `rank_mu` freeze what the balancer saw, for explainability only (never inputs to replay).
    """
    CREATE TABLE matches (
        id                INTEGER PRIMARY KEY,
        lobby_id          INTEGER NOT NULL UNIQUE REFERENCES lobbies(id),
        guild_id          INTEGER NOT NULL,
        status            TEXT NOT NULL CHECK (status IN
                          ('in_progress', 'reported', 'confirmed', 'voided')),
        winner            TEXT CHECK (winner IN ('blue', 'red')),
        expected_blue_win REAL NOT NULL,
        created_at        TEXT NOT NULL,
        reported_by       INTEGER REFERENCES players(id),
        reported_at       TEXT,
        confirmed_by      INTEGER REFERENCES players(id),  -- NULL when confirmed automatically
        confirmed_at      TEXT,
        auto_confirmed    INTEGER NOT NULL DEFAULT 0,
        contested         INTEGER NOT NULL DEFAULT 0,
        voided_by         INTEGER REFERENCES players(id),
        voided_at         TEXT,
        void_reason       TEXT,
        reminder_sent     INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX matches_by_status ON matches(status);
    CREATE TABLE match_participants (
        match_id     INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
        player_id    INTEGER NOT NULL REFERENCES players(id),
        side         TEXT NOT NULL CHECK (side IN ('blue', 'red')),
        role         TEXT NOT NULL CHECK (role IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')),
        h_mu_before  REAL NOT NULL,
        h_sigma_before REAL NOT NULL,
        est_mu       REAL NOT NULL,
        est_sigma    REAL NOT NULL,
        rank_mu      REAL,
        PRIMARY KEY (match_id, player_id),
        UNIQUE (match_id, side, role)
    );
    CREATE INDEX match_participants_by_player ON match_participants(player_id);
    CREATE TABLE match_audit (
        id         INTEGER PRIMARY KEY,
        match_id   INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
        action     TEXT NOT NULL CHECK (action IN
                   ('report', 'confirm', 'auto_confirm', 'contest', 'set_winner', 'void')),
        actor_id   INTEGER,  -- Discord user ID; NULL for the bot itself
        old_value  TEXT,
        new_value  TEXT,
        created_at TEXT NOT NULL
    );
    """,
    # 9: champion and KDA per player per game (M3b). Players enter their own stats (`pending`); an
    # admin validates them (`validated`, also what an admin-entered row is). `champion` is a Data
    # Dragon id with no foreign key, so a champion dropped from a later patch never breaks history.
    # `match_audit` is recreated because SQLite cannot alter a CHECK: it gains the stats actions and
    # `subject_id`, the player whose stats the action is about (NULL for match-level actions).
    """
    CREATE TABLE champions (
        id    TEXT PRIMARY KEY,
        name  TEXT NOT NULL,
        patch TEXT NOT NULL
    );
    ALTER TABLE match_participants ADD COLUMN champion TEXT;
    ALTER TABLE match_participants ADD COLUMN kills   INTEGER CHECK (kills   BETWEEN 0 AND 99);
    ALTER TABLE match_participants ADD COLUMN deaths  INTEGER CHECK (deaths  BETWEEN 0 AND 99);
    ALTER TABLE match_participants ADD COLUMN assists INTEGER CHECK (assists BETWEEN 0 AND 99);
    ALTER TABLE match_participants ADD COLUMN stats_status TEXT NOT NULL DEFAULT 'none'
        CHECK (stats_status IN ('none', 'pending', 'validated'));
    ALTER TABLE match_participants ADD COLUMN stats_set_by INTEGER REFERENCES players(id);
    ALTER TABLE match_participants ADD COLUMN stats_validated_by INTEGER REFERENCES players(id);
    ALTER TABLE match_participants ADD COLUMN played_role TEXT
        CHECK (played_role IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT'));
    CREATE TABLE match_audit_new (
        id         INTEGER PRIMARY KEY,
        match_id   INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
        action     TEXT NOT NULL CHECK (action IN
                   ('report', 'confirm', 'auto_confirm', 'contest', 'set_winner', 'void',
                    'stats', 'stats_set', 'stats_validate')),
        actor_id   INTEGER,  -- Discord user ID; NULL for the bot itself
        old_value  TEXT,
        new_value  TEXT,
        created_at TEXT NOT NULL,
        subject_id INTEGER REFERENCES players(id)
    );
    INSERT INTO match_audit_new (id, match_id, action, actor_id, old_value, new_value, created_at)
        SELECT id, match_id, action, actor_id, old_value, new_value, created_at FROM match_audit;
    DROP TABLE match_audit;
    ALTER TABLE match_audit_new RENAME TO match_audit;
    CREATE INDEX match_participants_pending ON match_participants(stats_status)
        WHERE stats_status = 'pending';
    """,
    # 10: which champion icons are already uploaded as application emojis. Apart from `champions`,
    # which is replaced on every new patch; a champion id keeps its emoji across patches.
    """
    CREATE TABLE champion_emojis (
        champion_id TEXT PRIMARY KEY,
        emoji_id    INTEGER NOT NULL,
        emoji_name  TEXT NOT NULL
    );
    """,
    # 11: the same for the five role icons (bundled PNGs, uploaded once as application emojis).
    """
    CREATE TABLE role_emojis (
        role       TEXT PRIMARY KEY CHECK (role IN ('TOP','JUNGLE','MID','ADC','SUPPORT')),
        emoji_id   INTEGER NOT NULL,
        emoji_name TEXT NOT NULL
    );
    """,
    # 12: Riot feed (daily leaderboard, post-game cards). `rank_*` is the last Solo/Duo rank the
    # feed saw for the account (tier NULL + `rank_checked_at` set = unranked): the leaderboard reads
    # it and the post-game LP change is computed against it. `riot_rank_snapshots` is one row per
    # account per day (written when the leaderboard is posted) for the daily LP trend.
    # Unlinking deletes both (cascade).
    """
    ALTER TABLE riot_accounts ADD COLUMN feed_since TEXT;
    ALTER TABLE riot_accounts ADD COLUMN feed_enabled INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE riot_accounts ADD COLUMN rank_tier TEXT;
    ALTER TABLE riot_accounts ADD COLUMN rank_division TEXT;
    ALTER TABLE riot_accounts ADD COLUMN rank_lp INTEGER;
    ALTER TABLE riot_accounts ADD COLUMN rank_checked_at TEXT;
    CREATE TABLE riot_rank_snapshots (
        riot_account_id INTEGER NOT NULL REFERENCES riot_accounts(id) ON DELETE CASCADE,
        day             TEXT NOT NULL,  -- Lisbon date, YYYY-MM-DD
        tier            TEXT,
        division        TEXT,
        lp              INTEGER,
        PRIMARY KEY (riot_account_id, day)
    );
    """,
]

_OWNER: ContextVar[object | None] = ContextVar("db_transaction_owner", default=None)


class _Connection(aiosqlite.Connection):
    """The one shared connection, made safe for multi-statement transactions.

    Everything else in the bot writes with single statements followed by `commit()`. A transaction
    spanning several statements would normally be torn apart by those commits (and by their
    statements being swept into a rollback), because all coroutines share this connection. So while
    a `transaction()` is open every other task's statement waits for it to finish, and the owning
    task's own `commit()`/`rollback()` calls are deferred to the end of the transaction. That lets
    the existing repository methods run unchanged inside one atomic transaction.

    Keep transaction bodies to database work: awaiting something that itself needs the database
    from another task would deadlock.
    """

    def __init__(self, connector: Callable[[], sqlite3.Connection], iter_chunk_size: int) -> None:
        super().__init__(connector, iter_chunk_size)
        self._tx_lock = asyncio.Lock()
        self._quiet = asyncio.Event()  # set while no transaction holds back other tasks
        self._quiet.set()
        self._draining = False  # a transaction is starting; stragglers may still commit
        self._owner: object | None = None

    def _owns(self) -> bool:
        return self._owner is not None and _OWNER.get() is self._owner

    async def _gate(self, finishing: bool = False) -> None:
        if not self._owns() and not (finishing and self._draining):
            await self._quiet.wait()

    @contextmanager
    async def execute(self, sql, parameters=None):  # type: ignore[override]
        await self._gate()
        return await super().execute(sql, parameters)

    @contextmanager
    async def executemany(self, sql, parameters):  # type: ignore[override]
        await self._gate()
        return await super().executemany(sql, parameters)

    @contextmanager
    async def executescript(self, sql_script):  # type: ignore[override]
        await self._gate()
        return await super().executescript(sql_script)

    async def commit(self) -> None:
        if self._owns():
            return  # the transaction commits as a whole when it ends
        await self._gate(finishing=True)
        await super().commit()

    async def rollback(self) -> None:
        if self._owns():
            return  # the failing statement was already undone; the transaction rolls back at exit
        await self._gate(finishing=True)
        await super().rollback()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """All-or-nothing: everything written inside is committed together or not at all."""
        if self._owns():
            yield  # nested use joins the outer transaction
            return
        async with self._tx_lock:
            self._draining = True
            self._quiet.clear()
            try:
                await self._wait_for_others()
                await super().execute("BEGIN IMMEDIATE")
                owner = object()
                self._owner = owner
                self._draining = False
                token = _OWNER.set(owner)
                try:
                    yield
                except BaseException:
                    await super().rollback()
                    raise
                else:
                    await super().commit()
                finally:
                    _OWNER.reset(token)
            finally:
                self._owner = None
                self._draining = False
                self._quiet.set()

    async def _wait_for_others(self) -> None:
        # Statements queued before the gate closed still run first; let their own implicit
        # transactions finish (they may commit) before starting ours.
        async with super().execute("SELECT 1") as cur:
            await cur.fetchall()
        for _ in range(1000):
            if not self.in_transaction:
                return
            await asyncio.sleep(0.001)
        log.warning("Committing a write that was left uncommitted before a transaction.")
        await super().commit()


# Tables whose size is shown at startup (fixed names, never user input).
SUMMARY_TABLES = ("names", "phrases", "picks", "players", "lobbies", "matches", "champions")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._conn: _Connection | None = None

    @property
    def conn(self) -> _Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected.")
        return self._conn

    def transaction(self) -> AbstractAsyncContextManager[None]:
        """`async with db.transaction():` makes the writes inside atomic (see `_Connection`)."""
        return self.conn.transaction()

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        path = self.path
        conn = _Connection(lambda: sqlite3.connect(path), 64)
        await conn
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

    async def describe(self) -> str:
        """Location, schema version and size of the main tables, for the startup log."""
        async with self.conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        counts = []
        for table in SUMMARY_TABLES:
            async with self.conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # noqa: S608
                row_count = await cur.fetchone()
            counts.append(f"{table}={row_count[0] if row_count else 0}")
        return f"{self.path} schema v{row[0] if row else 0}/{len(MIGRATIONS)} " + " ".join(counts)
