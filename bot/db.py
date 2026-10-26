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
