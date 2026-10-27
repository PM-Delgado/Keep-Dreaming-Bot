"""The go-live preparation: roster only, /puta names and phrases kept, picks and games gone."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from bot.db import Database
from bot.features.nameday.repository import NameDayRepository
from bot.features.players.proficiency import ROLES
from bot.features.roster.games import Clock, Playground
from bot.features.roster.loader import RosterEntry
from bot.features.roster.production import check_production, prepare_production
from bot.features.roster.repository import RosterRepository
from bot.features.roster.seed import seed_roster
from bot.features.roster.stack import Stack

ADMIN = 1000
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
TIERS = [("SILVER", "II"), ("GOLD", "I"), ("PLATINUM", "IV"), ("BRONZE", "III"), ("EMERALD", "II")]


def entries(count: int = 14) -> list[RosterEntry]:
    out = []
    for i in range(count):
        tier, division = TIERS[i % len(TIERS)]
        roles = {ROLES[i % 5]: 5, ROLES[(i + 1) % 5]: 3}
        out.append(RosterEntry(ADMIN + i, f"Amigo{i:02d}", tier, division, roles, (), "euw1", None))
    return out


@pytest.fixture
def seed_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "seed"
    folder.mkdir()
    (folder / "names.json").write_text(json.dumps(["Ana", "Bia"]), encoding="utf-8")
    (folder / "phrases.json").write_text(json.dumps(["Olá {name}"]), encoding="utf-8")
    return folder


async def scalar(db: Database, sql: str) -> int:
    async with db.conn.execute(sql) as cur:
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def dirty_database(db: Database, seed_dir: Path) -> None:
    """Roster seeded, test games played, /puta used and edited by hand."""
    people = entries()
    clock = Clock(NOW - timedelta(hours=400))
    stack = Stack(db, frozenset({ADMIN}), clock=clock, seed=3)
    await NameDayRepository(db).seed_if_empty(seed_dir)
    await seed_roster(stack, people, ADMIN, link_riot=False)
    play = Playground(stack, 77, people, ADMIN, clock, 3, NOW)
    await play.prepare()
    await play.play_history(games=4, newcomers=1)
    nameday = NameDayRepository(db)
    await nameday.add_pick(date(2026, 5, 30), 1, "Olá Ana", NOW)
    await db.conn.execute("INSERT INTO phrases (text) VALUES ('uma frase feita à mão')")
    await db.conn.commit()


async def test_production_keeps_the_puta_data_and_the_roster_and_drops_the_rest(
    db: Database, seed_dir: Path
):
    await dirty_database(db, seed_dir)
    assert await scalar(db, "SELECT COUNT(*) FROM picks") == 1
    assert await scalar(db, "SELECT COUNT(*) FROM matches") > 0
    assert await scalar(db, "SELECT COUNT(*) FROM players WHERE is_mock = 1") > 0

    result = await prepare_production(
        db, Stack(db, frozenset({ADMIN})), entries(), ADMIN, link_riot=False, seed_dir=seed_dir
    )

    assert result.ok, [c for c in result.checks if not c.ok]
    for table in ("picks", "matches", "lobbies", "match_audit"):
        assert await scalar(db, f"SELECT COUNT(*) FROM {table}") == 0, table
    assert await scalar(db, "SELECT COUNT(*) FROM players") == 14
    assert await scalar(db, "SELECT COUNT(*) FROM players WHERE is_mock = 1") == 0
    assert await scalar(db, "SELECT COUNT(*) FROM ratings") == 14
    # names and phrases are exactly what they were, including the hand-written phrase
    assert await scalar(db, "SELECT COUNT(*) FROM names") == 2
    assert await scalar(db, "SELECT COUNT(*) FROM phrases") == 2


async def test_production_on_a_fresh_database_seeds_the_puta_data_from_the_files(
    db: Database, seed_dir: Path
):
    result = await prepare_production(
        db, Stack(db, frozenset({ADMIN})), entries(), ADMIN, link_riot=False, seed_dir=seed_dir
    )
    assert result.ok
    assert result.after.names == 2 and result.after.phrases == 1 and result.after.picks == 0


async def test_production_can_be_run_twice(db: Database, seed_dir: Path):
    for _ in range(2):
        result = await prepare_production(
            db, Stack(db, frozenset({ADMIN})), entries(), ADMIN, link_riot=False, seed_dir=seed_dir
        )
        assert result.ok
    assert await scalar(db, "SELECT COUNT(*) FROM players") == 14


async def test_a_pick_only_goes_away_when_production_runs(db: Database, seed_dir: Path):
    await NameDayRepository(db).seed_if_empty(seed_dir)
    await NameDayRepository(db).add_pick(date(2026, 5, 30), 1, "Olá Ana", NOW)
    assert await RosterRepository(db).wipe_picks() == 1
    assert await scalar(db, "SELECT COUNT(*) FROM names") == 2


async def test_the_check_flags_what_would_make_a_bad_launch(db: Database, seed_dir: Path):
    result = await prepare_production(
        db, Stack(db, frozenset({ADMIN})), entries(), ADMIN, link_riot=False, seed_dir=seed_dir
    )
    good = result.after
    people = entries()

    def failing(snap, **kw):
        args = {"link_riot": False, "missing_names": set(), "missing_phrases": set()} | kw
        return {c.label for c in check_production(snap, people, **args) if not c.ok}

    assert failing(good) == set()
    assert failing(replace(good, picks=1)) == {"/puta picks"}
    assert failing(replace(good, mock_players=2)) == {"mock players"}
    assert failing(replace(good, matches=1)) == {"games and lobbies"}
    assert failing(replace(good, players=13, ratings=13)) == {"players"}
    assert failing(good, missing_names={"Ana"}) == {"/puta names"}
    assert failing(replace(good, integrity="database disk image is malformed")) == {"integrity"}
    with_riot = [replace(people[0], riot=("Amigo00#EUW",)), *people[1:]]
    labels = {
        c.label
        for c in check_production(
            good, with_riot, link_riot=True, missing_names=set(), missing_phrases=set()
        )
        if not c.ok
    }
    assert labels == {"Riot accounts"}
