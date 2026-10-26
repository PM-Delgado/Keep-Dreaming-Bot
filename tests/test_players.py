import sqlite3
from datetime import UTC, datetime

import aiosqlite
import pytest

from bot.db import MIGRATIONS, Database
from bot.features.players.proficiency import MAX_ESTIMATED, effective_proficiency
from bot.features.players.repository import PlayerRepository
from bot.features.players.service import PlayerError, PlayerService, clean_nickname
from bot.features.ratings.repository import RatingRepository
from bot.features.ratings.service import RatingService

T0 = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture
def players(db: Database) -> PlayerService:
    ratings = RatingService(RatingRepository(db), clock=lambda: T0)
    return PlayerService(PlayerRepository(db), ratings, clock=lambda: T0)


async def test_register_creates_player_with_a_default_rating(players):
    player = await players.register(1001, "  Ana   Silva ")
    assert player.nickname == "Ana Silva"
    stored = await players.ratings.repo.get_stored(player.id)
    assert stored is not None
    assert (stored.mu, stored.games, stored.placed) == (25.0, 0, False)


@pytest.mark.parametrize("bad", ["", " ", "a", "x" * 25, "tab\there\x00"])
def test_nickname_validation(bad):
    with pytest.raises(PlayerError):
        clean_nickname(bad)


async def test_duplicate_registration_and_nickname_are_rejected_case_insensitively(players):
    await players.register(1, "Ana")
    with pytest.raises(PlayerError, match="Já estás registado"):
        await players.register(1, "Outro")
    with pytest.raises(PlayerError, match="já está em uso"):
        await players.register(2, "ANA")
    assert await players.repo.get_by_discord(2) is None  # nothing half-created


async def test_unregistered_players_are_rejected(players):
    with pytest.raises(PlayerError, match="não está registado"):
        await players.require(404)


async def test_soft_leave_keeps_the_row_but_blocks_use(players):
    player = await players.register(1, "Ana")
    await players.repo.set_left(player.id, T0)
    with pytest.raises(PlayerError):
        await players.require(1)
    assert (await players.repo.get(player.id)) is not None  # history keeps pointing at it
    await players.repo.set_left(player.id, None)
    assert (await players.require(1)).id == player.id


async def test_declared_and_admin_proficiency_with_precedence(players):
    await players.register(1, "Ana")
    await players.set_declared(1, "mid", 4)
    profile = await players.profile(1)
    mid = next(line for line in profile.roles if line.role == "MID")
    assert (mid.proficiency.value, mid.proficiency.source) == (4, "declared")

    await players.set_admin_override(1, "MID", 2)
    mid = next(line for line in (await players.profile(1)).roles if line.role == "MID")
    assert (mid.proficiency.value, mid.proficiency.source) == (2, "admin")

    await players.set_admin_override(1, "MID", None)  # clear the override
    await players.set_declared(1, "MID", None)  # clear the declaration
    mid = next(line for line in (await players.profile(1)).roles if line.role == "MID")
    assert (mid.proficiency.value, mid.proficiency.source) == (None, "unknown")


async def test_invalid_role_or_value(players):
    await players.register(1, "Ana")
    with pytest.raises(PlayerError):
        await players.set_declared(1, "BOT", 3)
    with pytest.raises(PlayerError):
        await players.set_declared(1, "TOP", 6)
    with pytest.raises(PlayerError):
        await players.set_declared(1, "TOP", 0)


async def test_profile_shows_placement_and_no_rank_without_signals(players):
    await players.register(1, "Ana")
    profile = await players.profile(1)
    assert not profile.standing.placed
    assert profile.standing.games == 0
    assert profile.resolution.rank is None
    assert [line.role for line in profile.roles] == ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"]


def test_effective_proficiency_precedence_and_cap():
    assert effective_proficiency(3, 5, 2).source == "admin"
    assert effective_proficiency(3, None, 2) == effective_proficiency(3, None, None)
    assert effective_proficiency(None, None, 5).value == MAX_ESTIMATED  # Riot alone can't say 5
    assert effective_proficiency(None, None, 5).source == "estimated"
    assert effective_proficiency(None, None, None).value is None


async def test_migration_2_upgrades_a_populated_version_1_database(tmp_path):
    path = tmp_path / "kdbot.sqlite3"
    async with aiosqlite.connect(path) as raw:
        await raw.executescript(f"BEGIN;{MIGRATIONS[0]};PRAGMA user_version = 1;COMMIT;")
        await raw.execute("INSERT INTO names (name) VALUES ('Maria')")
        await raw.commit()

    database = Database(path)
    await database.connect()
    try:
        async with database.conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == len(MIGRATIONS)
        async with database.conn.execute("SELECT name FROM names") as cur:
            assert await cur.fetchall() == [("Maria",)]  # existing data untouched
        for table in ("players", "player_roles", "rating_events", "rank_signals", "ratings"):
            async with database.conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                assert await cur.fetchone() == (0,)
    finally:
        await database.close()


async def test_schema_constraints_reject_bad_rows(db):
    conn = db.conn
    await conn.execute(
        "INSERT INTO players (id, discord_id, nickname, created_at) VALUES (1, 1, 'A', 'x')"
    )
    bad_rows = [
        "INSERT INTO player_roles VALUES (1, 'BOT', 3, NULL, 'x')",
        "INSERT INTO player_roles VALUES (1, 'MID', 6, NULL, 'x')",
        "INSERT INTO rank_signals (player_id, source, status, created_at)"
        " VALUES (1,'riot','ranked','x')",  # ranked without a tier
        "INSERT INTO rank_signals (player_id, source, status, tier, created_at)"
        " VALUES (1,'riot','unranked','GOLD','x')",
        "INSERT INTO rank_signals (player_id, source, status, created_at)"
        " VALUES (1,'self_reported','cleared','x')",
        "INSERT INTO rank_signals (player_id, source, status, created_at)"
        " VALUES (1,'admin','unranked','x')",
        "INSERT INTO rating_events (player_id, kind, mu, sigma, source, created_at)"
        " VALUES (1,'bogus',25,8,'default','x')",
        "INSERT INTO rating_history (player_id, mu_after, sigma_after, placed, algo_version,"
        " ladder_version) VALUES (1, 25, 8, 0, 'a', 1)",  # neither event_id nor match_id
    ]
    for sql in bad_rows:
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(sql)
