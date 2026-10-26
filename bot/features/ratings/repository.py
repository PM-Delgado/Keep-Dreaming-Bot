"""SQL for rating events, rank signals and the rebuildable rating caches."""

from dataclasses import dataclass
from datetime import UTC, datetime

from bot.db import Database
from bot.features.ratings.rank import RankSignal
from bot.features.ratings.replay import RatingEventIn, ReplayResult


def to_db(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def from_db(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class StoredRating:
    player_id: int
    mu: float
    sigma: float
    games: int
    placed: bool
    tier_key: str | None
    division: int | None
    algo_version: str
    ladder_version: int


class RatingRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- authoritative inputs: single-statement appends --------------------------------

    async def add_event(
        self,
        player_id: int,
        kind: str,
        mu: float,
        sigma: float,
        source: str,
        detail: str | None,
        created_at: datetime,
        created_by: int | None,
    ) -> int:
        cur = await self.db.conn.execute(
            "INSERT INTO rating_events (player_id, kind, mu, sigma, source, detail, created_at,"
            " created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (player_id, kind, mu, sigma, source, detail, to_db(created_at), created_by),
        )
        await self.db.conn.commit()
        return cur.lastrowid or 0

    async def list_events(self) -> list[RatingEventIn]:
        async with self.db.conn.execute(
            "SELECT id, player_id, kind, mu, sigma, created_at FROM rating_events ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        return [RatingEventIn(r[0], r[1], r[2], r[3], r[4], from_db(r[5])) for r in rows]

    async def has_events(self, player_id: int) -> bool:
        async with self.db.conn.execute(
            "SELECT 1 FROM rating_events WHERE player_id = ? LIMIT 1", (player_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def count_events(self) -> int:
        async with self.db.conn.execute("SELECT COUNT(*) FROM rating_events") as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def add_signal(
        self,
        player_id: int,
        source: str,
        status: str,
        tier: str | None,
        division: str | None,
        created_at: datetime,
        created_by: int | None,
        account_key: str | None = None,
        queue_type: str | None = None,
        lp: int | None = None,
        detail: str | None = None,
    ) -> int:
        cur = await self.db.conn.execute(
            "INSERT INTO rank_signals (player_id, source, status, account_key, queue_type, tier,"
            " division, lp, detail, created_at, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                player_id,
                source,
                status,
                account_key,
                queue_type,
                tier,
                division,
                lp,
                detail,
                to_db(created_at),
                created_by,
            ),
        )
        await self.db.conn.commit()
        return cur.lastrowid or 0

    async def delete_riot_signals(self, account_key: str) -> int:
        """Remove every Riot rank signal of one account (used when it is unlinked)."""
        cur = await self.db.conn.execute(
            "DELETE FROM rank_signals WHERE source = 'riot' AND account_key = ?", (account_key,)
        )
        await self.db.conn.commit()
        return cur.rowcount

    async def list_signals(self, player_id: int) -> list[RankSignal]:
        async with self.db.conn.execute(
            "SELECT id, source, status, created_at, account_key, tier, division"
            " FROM rank_signals WHERE player_id = ? ORDER BY id",
            (player_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [RankSignal(r[0], r[1], r[2], from_db(r[3]), r[4], r[5], r[6]) for r in rows]

    # --- derived caches ------------------------------------------------------------------

    async def replace_caches(
        self, result: ReplayResult, algo_version: str, ladder_version: int, updated_at: datetime
    ) -> None:
        """Upsert `ratings` (never emptied, so readers always see a row) and rewrite the history.

        These are caches: a partial write is repaired by the next rebuild, so no transaction
        abstraction is needed.
        """
        conn = self.db.conn
        stamp = to_db(updated_at)
        await conn.executemany(
            "INSERT INTO ratings (player_id, mu, sigma, games, placed, tier_key, division,"
            " algo_version, ladder_version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(player_id) DO UPDATE SET mu=excluded.mu, sigma=excluded.sigma,"
            " games=excluded.games, placed=excluded.placed, tier_key=excluded.tier_key,"
            " division=excluded.division, algo_version=excluded.algo_version,"
            " ladder_version=excluded.ladder_version, updated_at=excluded.updated_at",
            [
                (
                    pid,
                    s.mu,
                    s.sigma,
                    s.games,
                    int(s.standing.placed),
                    s.standing.tier_key,
                    s.standing.division,
                    algo_version,
                    ladder_version,
                    stamp,
                )
                for pid, s in sorted(result.players.items())
            ],
        )
        await conn.execute("DELETE FROM rating_history")
        await conn.executemany(
            "INSERT INTO rating_history (player_id, event_id, match_id, mu_before, sigma_before,"
            " mu_after, sigma_after, placed, tier_key, division, algo_version, ladder_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    h.player_id,
                    h.event_id,
                    h.match_id,
                    h.mu_before,
                    h.sigma_before,
                    h.mu_after,
                    h.sigma_after,
                    int(h.standing.placed),
                    h.standing.tier_key,
                    h.standing.division,
                    algo_version,
                    ladder_version,
                )
                for h in result.history
            ],
        )
        await conn.commit()

    async def get_stored(self, player_id: int) -> StoredRating | None:
        async with self.db.conn.execute(
            "SELECT player_id, mu, sigma, games, placed, tier_key, division, algo_version,"
            " ladder_version FROM ratings WHERE player_id = ?",
            (player_id,),
        ) as cur:
            r = await cur.fetchone()
        return (
            StoredRating(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6], r[7], r[8]) if r else None
        )

    async def list_stored(self) -> list[StoredRating]:
        async with self.db.conn.execute(
            "SELECT player_id, mu, sigma, games, placed, tier_key, division, algo_version,"
            " ladder_version FROM ratings ORDER BY player_id"
        ) as cur:
            rows = await cur.fetchall()
        return [
            StoredRating(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6], r[7], r[8]) for r in rows
        ]

    async def cache_versions(self) -> set[tuple[str, int]]:
        async with self.db.conn.execute(
            "SELECT DISTINCT algo_version, ladder_version FROM ratings"
        ) as cur:
            return {(r[0], r[1]) for r in await cur.fetchall()}

    async def players_without_rating_but_with_events(self) -> int:
        async with self.db.conn.execute(
            "SELECT COUNT(DISTINCT e.player_id) FROM rating_events e"
            " LEFT JOIN ratings r ON r.player_id = e.player_id WHERE r.player_id IS NULL"
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0
