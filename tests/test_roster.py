"""The roster tools: file checks, seeding, test games with the real players, and the reset."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from riot_fakes import FakeRiot, make_client

from bot.db import Database
from bot.features.players.proficiency import ROLES
from bot.features.players.repository import PlayerRepository
from bot.features.roster.backup import backup_database
from bot.features.roster.games import Clock, Playground, PlaygroundError
from bot.features.roster.loader import (
    RosterEntry,
    RosterError,
    load_roster,
    parse_roster,
    strength_of,
)
from bot.features.roster.report import Game, Slot, format_game, load_games, summarize
from bot.features.roster.repository import RosterRepository
from bot.features.roster.reset import plan_reset, reset_league
from bot.features.roster.seed import seed_roster
from bot.features.roster.stack import Stack

ADMIN = 1000
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
TIERS = [("SILVER", "II"), ("GOLD", "I"), ("PLATINUM", "IV"), ("BRONZE", "III"), ("EMERALD", "II")]


def entries(count: int = 18) -> list[RosterEntry]:
    out = []
    for i in range(count):
        tier, division = TIERS[i % len(TIERS)]
        roles = {ROLES[i % 5]: 5, ROLES[(i + 1) % 5]: 3}
        out.append(RosterEntry(ADMIN + i, f"Amigo{i:02d}", tier, division, roles, (), "euw1", None))
    return out


def doc(*players: dict) -> dict:
    return {"players": list(players)}


def good(**changes) -> dict:
    base = {
        "discord_id": 111,
        "nickname": "Ana",
        "rank": {"tier": "gold", "division": "ii"},
        "roles": {"mid": 5},
        "riot": ["Ana#EUW"],
        "platform": "EUW1",
        "strength": None,
    }
    return base | changes


# --- the file ---------------------------------------------------------------------------------


def test_a_good_entry_is_normalised():
    (entry,) = parse_roster(doc(good()))
    assert (entry.tier, entry.division) == ("GOLD", "II")
    assert dict(entry.roles) == {"MID": 5}
    assert entry.platform == "euw1"
    assert entry.riot == ("Ana#EUW",)
    assert entry.rank_label == "GOLD II"


def test_apex_tiers_need_no_division_and_no_rank_is_allowed():
    master, unranked = parse_roster(
        doc(
            good(rank={"tier": "Master", "division": None}),
            good(discord_id=2, nickname="Bia", rank=None, riot=[]),
        )
    )
    assert (master.tier, master.division) == ("MASTER", None)
    assert unranked.tier is None
    assert unranked.rank_label == "sem rank"


def test_every_problem_is_reported_together():
    with pytest.raises(RosterError) as caught:
        parse_roster(
            doc(
                good(discord_id=0),
                good(discord_id=2, nickname="A"),
                good(discord_id=3, nickname="Cris", rank={"tier": "GOLD"}),
                good(discord_id=4, nickname="Dani", rank={"tier": "WOOD", "division": "I"}),
                good(discord_id=5, nickname="Eva", roles={"MID": 9, "BOT": 3}),
                good(discord_id=6, nickname="Fa", riot=["no-tag"], platform="mars"),
                good(discord_id=7, nickname="Gil", strength=150),
            )
        )
    text = "\n".join(caught.value.problems)
    for expected in (
        "discord_id",
        "nickname must have",
        "needs a division",
        "unknown tier",
        "proficiency of MID",
        "unknown role",
        "not a Riot ID",
        "unknown platform",
        "strength must be",
    ):
        assert expected in text


def test_duplicates_are_rejected():
    with pytest.raises(RosterError) as caught:
        parse_roster(doc(good(), good(nickname="ANA", riot=["ana#euw"])))
    text = "\n".join(caught.value.problems)
    assert "duplicate discord_id" in text
    assert "duplicate nickname" in text
    assert "duplicate Riot ID" in text


@pytest.mark.parametrize("document", [None, [], {}, {"players": []}, {"players": "x"}])
def test_a_file_without_players_is_rejected(document):
    with pytest.raises(RosterError):
        parse_roster(document)


def test_load_roster_reports_unreadable_and_broken_files(tmp_path: Path):
    with pytest.raises(RosterError, match="cannot read"):
        load_roster(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    with pytest.raises(RosterError, match="not valid JSON"):
        load_roster(broken)


def test_the_example_file_has_18_players_and_is_refused_until_filled():
    path = Path(__file__).parent.parent / "tools" / "roster.example.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert len(document["players"]) == 18
    with pytest.raises(RosterError):
        parse_roster(document)  # the ids are still 0
    for n, player in enumerate(document["players"], start=1):
        player["discord_id"] = 100 + n
    assert len(parse_roster(document)) == 18


def test_strength_is_the_files_value_or_comes_from_the_rank():
    by_hand = RosterEntry(1, "A", "GOLD", "II", {}, (), "euw1", 61.5)
    gold = RosterEntry(2, "B", "GOLD", "II", {}, (), "euw1", None)
    iron = RosterEntry(3, "C", "IRON", "IV", {}, (), "euw1", None)
    unranked = RosterEntry(4, "D", None, None, {}, (), "euw1", None)
    assert strength_of(by_hand) == 61.5
    assert strength_of(iron) < strength_of(gold)
    assert strength_of(unranked) > 0


# --- seeding ----------------------------------------------------------------------------------


async def rows(db: Database, sql: str, *args: object) -> list[tuple[Any, ...]]:
    async with db.conn.execute(sql, args) as cur:
        return [tuple(r) for r in await cur.fetchall()]


async def test_seed_registers_everyone_with_rank_roles_and_riot_accounts(db: Database):
    world = FakeRiot()
    world.add_account("Amigo00", "EUW", "puuid-0")
    world.set_rank("puuid-0", "DIAMOND", "IV")
    people = entries(3)
    people[0] = RosterEntry(
        ADMIN, "Amigo00", "SILVER", "II", {"MID": 5, "ADC": 3}, ("Amigo00#EUW",), "euw1", None
    )
    stack = Stack(db, frozenset({ADMIN}), riot_client=make_client(world))

    report = await seed_roster(stack, people, ADMIN)

    assert len(report.created) == 3
    assert report.linked == ["Amigo00: Amigo00#EUW"]
    assert report.problems == []
    profile = await stack.players.profile(ADMIN)
    assert [line.role for line in profile.roles if line.proficiency.value == 5] == ["MID"]
    assert profile.accounts == ["Amigo00#EUW (EUW1) ★"]
    rank = profile.resolution.rank
    assert rank is not None
    assert (rank.tier, rank.division) == ("DIAMOND", "IV")


async def test_seed_can_be_run_again_without_duplicating_anything(db: Database):
    stack = Stack(db, frozenset({ADMIN}))
    people = entries(4)
    await seed_roster(stack, people, ADMIN, link_riot=False)
    before = [await rows(db, f"SELECT COUNT(*) FROM {t}") for t in
              ("players", "rank_signals", "rating_events", "player_roles")]  # fmt: skip

    report = await seed_roster(stack, people, ADMIN, link_riot=False)

    assert report.created == []
    assert len(report.existing) == 4
    assert report.ranks == []
    after = [await rows(db, f"SELECT COUNT(*) FROM {t}") for t in
             ("players", "rank_signals", "rating_events", "player_roles")]  # fmt: skip
    assert after == before


async def test_a_changed_rank_in_the_file_is_applied_on_the_next_run(db: Database):
    stack = Stack(db, frozenset({ADMIN}))
    first = entries(1)
    await seed_roster(stack, first, ADMIN, link_riot=False)
    moved = [RosterEntry(ADMIN, "Amigo00", "EMERALD", "I", {}, (), "euw1", None)]
    report = await seed_roster(stack, moved, ADMIN, link_riot=False)
    assert report.ranks == ["Amigo00: EMERALD I"]


async def test_seed_without_a_riot_key_says_so_and_still_registers(db: Database):
    stack = Stack(db, frozenset({ADMIN}))
    people = [RosterEntry(ADMIN, "Ana", "GOLD", "I", {}, ("Ana#EUW",), "euw1", None)]
    report = await seed_roster(stack, people, ADMIN)
    assert report.created == ["Ana"]
    assert "RIOT_API_KEY is not set" in report.problems[0]
    assert await rows(db, "SELECT COUNT(*) FROM riot_accounts") == [(0,)]


async def test_an_unknown_riot_account_is_reported_and_the_rest_carry_on(db: Database):
    world = FakeRiot()
    world.add_account("Bia", "EUW", "puuid-b")
    people = [
        RosterEntry(1, "Ana", "GOLD", "I", {}, ("Ghost#EUW",), "euw1", None),
        RosterEntry(2, "Bia", "GOLD", "I", {}, ("Bia#EUW",), "euw1", None),
    ]
    stack = Stack(db, frozenset({1}), riot_client=make_client(world))
    report = await seed_roster(stack, people, 1)
    assert report.linked == ["Bia: Bia#EUW"]
    assert len(report.problems) == 1
    assert "Ana: Ghost#EUW" in report.problems[0]


async def test_an_existing_player_with_another_nickname_is_flagged_not_renamed(db: Database):
    stack = Stack(db, frozenset({ADMIN}))
    await stack.players.register(ADMIN, "OldName")
    report = await seed_roster(stack, entries(1), ADMIN, link_riot=False)
    assert report.created == []
    assert "OldName" in report.problems[0]
    assert (await stack.players.require(ADMIN)).nickname == "OldName"


# --- test games and the reset -------------------------------------------------------------------


async def playground(db: Database, people: list[RosterEntry], games_clock_hours: int = 400):
    clock = Clock(NOW - timedelta(hours=games_clock_hours))
    stack = Stack(db, frozenset({ADMIN}), clock=clock, seed=3)
    await seed_roster(stack, people, ADMIN, link_riot=False)
    return stack, Playground(stack, 77, people, ADMIN, clock, 3, NOW)


async def test_the_roster_plays_every_game_state_and_newcomers_stay_in_placement(db: Database):
    people = entries(18)
    stack, play = await playground(db, people)
    await play.prepare()

    played = await play.play_history(games=12, newcomers=2)

    assert played.unfinished == 3
    counts = dict(await rows(db, "SELECT status, COUNT(*) FROM matches GROUP BY status"))
    assert counts["in_progress"] == 1
    assert counts["reported"] == 2  # the contested one is a reported game with a flag
    assert counts["voided"] == 1  # game 8 of the script; the later voids need more games
    assert counts["confirmed"] == 11
    assert await rows(db, "SELECT COUNT(*) FROM matches WHERE contested = 1") == [(1,)]
    # 18 real players cannot fill three unfinished games: the rest are mock players
    assert played.mock_players > 0
    assert len(await rows(db, "SELECT 1 FROM players WHERE is_mock = 0")) == 18
    newcomers = [p.discord_id for p in people[-2:]]
    for discord_id in newcomers:
        stored = await stack.ratings.repo.get_stored((await stack.players.require(discord_id)).id)
        assert stored is not None
        assert not stored.placed  # still in placement
    # the admin is in the live game
    admin_id = (await stack.players.require(ADMIN)).id
    assert await rows(
        db,
        "SELECT 1 FROM match_participants mp JOIN matches m ON m.id = mp.match_id"
        " WHERE m.status = 'in_progress' AND mp.player_id = ?",
        admin_id,
    )


async def test_games_are_reproducible_for_a_seed(db: Database):
    async def run() -> list:
        database = Database(":memory:")
        await database.connect()
        try:
            _, play = await playground(database, entries(18))
            await play.prepare()
            await play.play_history(games=6, newcomers=2)
            return await rows(database, "SELECT id, status, winner FROM matches ORDER BY id")
        finally:
            await database.close()

    assert await run() == await run()


async def test_the_admin_must_be_in_the_roster(db: Database):
    people = entries(12)
    stack = Stack(db, frozenset({999}))
    with pytest.raises(PlaygroundError, match="must be in the roster"):
        Playground(stack, 77, people, 999, Clock(NOW), 1, NOW)


async def test_too_few_players_cannot_play(db: Database):
    stack, play = await playground(db, entries(6))
    await play.prepare()
    with pytest.raises(PlaygroundError, match="at least 10"):
        await play.play_history(games=3, newcomers=0)


async def test_reset_leaves_only_the_roster(db: Database):
    people = entries(18)
    world = FakeRiot()
    world.add_account("Amigo00", "EUW", "puuid-0")
    people[0] = RosterEntry(
        ADMIN, "Amigo00", "SILVER", "II", people[0].roles, ("Amigo00#EUW",), "euw1", None
    )
    clock = Clock(NOW - timedelta(hours=400))
    stack = Stack(db, frozenset({ADMIN}), riot_client=make_client(world), clock=clock, seed=3)
    await seed_roster(stack, people, ADMIN)
    await db.conn.execute("INSERT INTO names (name) VALUES ('Zeca')")
    await db.conn.commit()
    play = Playground(stack, 77, people, ADMIN, clock, 3, NOW)
    await play.prepare()
    await play.play_history(games=8, newcomers=2)
    await stack.ratings.adjust((await stack.players.require(ADMIN)).id, 70, None, ADMIN, "tweak")
    seed_sql = (
        "SELECT COUNT(*) FROM rating_events e JOIN players p ON p.id = e.player_id"
        " WHERE e.kind = 'seed' AND p.is_mock = 0"
    )
    seeds_before = await rows(db, seed_sql)

    repo, players = RosterRepository(db), PlayerRepository(db)
    plan = await plan_reset(repo, players, True)
    assert plan.games.matches == 11
    assert plan.games.adjustments == 1
    assert plan.mock_players > 0

    await reset_league(repo, players, stack.ratings, True)

    for table in ("matches", "match_participants", "match_audit", "lobbies", "lobby_entries"):
        assert await rows(db, f"SELECT COUNT(*) FROM {table}") == [(0,)]
    assert await rows(db, "SELECT COUNT(*) FROM rating_history WHERE match_id IS NOT NULL") == [
        (0,)
    ]
    assert await rows(db, "SELECT COUNT(*) FROM players WHERE is_mock = 1") == [(0,)]
    assert await rows(db, "SELECT COUNT(*) FROM players") == [(18,)]
    assert await rows(db, "SELECT COUNT(*) FROM riot_accounts") == [(1,)]
    assert await rows(db, "SELECT COUNT(*) FROM player_roles") == [(36,)]
    assert await rows(db, "SELECT COUNT(*) FROM rating_events WHERE kind = 'adjust'") == [(0,)]
    assert await rows(db, seed_sql) == seeds_before
    assert await rows(db, "SELECT name FROM names") == [("Zeca",)]
    games = await rows(db, "SELECT DISTINCT games FROM ratings")
    assert games == [(0,)]  # every rating is back to its seed
    assert await rows(db, "SELECT COUNT(*) FROM ratings") == [(18,)]


async def test_reset_on_a_database_without_games_changes_nothing(db: Database):
    stack = Stack(db, frozenset({ADMIN}))
    await seed_roster(stack, entries(5), ADMIN, link_riot=False)
    repo, players = RosterRepository(db), PlayerRepository(db)
    plan = await plan_reset(repo, players, True)
    assert (plan.games.matches, plan.games.lobbies, plan.mock_players) == (0, 0, 0)
    await reset_league(repo, players, stack.ratings, True)
    assert await rows(db, "SELECT COUNT(*) FROM players") == [(5,)]


# --- backup -------------------------------------------------------------------------------------


async def test_backup_copies_the_database_and_skips_a_missing_one(tmp_path: Path):
    assert backup_database(tmp_path / "none.sqlite3", "x") is None
    path = tmp_path / "kdbot.sqlite3"
    database = Database(str(path))
    await database.connect()
    await database.conn.execute("INSERT INTO names (name) VALUES ('Zeca')")
    await database.conn.commit()
    copy = backup_database(path, "pre-test", NOW)
    await database.close()
    assert copy is not None
    assert copy.name == "kdbot.sqlite3.bak-20260601-120000-pre-test"
    other = Database(str(copy))
    await other.connect()
    try:
        assert await rows(other, "SELECT name FROM names") == [("Zeca",)]
    finally:
        await other.close()


async def test_a_roster_seeded_just_now_can_still_get_a_history_in_the_past(db: Database):
    people = entries(14)
    seeding = Stack(db, frozenset({ADMIN}), clock=lambda: NOW)  # dated "now", like the CLI's seed
    await seed_roster(seeding, people, ADMIN, link_riot=False)
    start = NOW - timedelta(days=10)
    clock = Clock(start)
    await RosterRepository(db).date_roster_before(start - timedelta(hours=1))

    stack = Stack(db, frozenset({ADMIN}), clock=clock, seed=3)
    play = Playground(stack, 77, people, ADMIN, clock, 3, NOW)
    await play.prepare()
    await play.play_history(games=6, newcomers=1)

    assert await rows(db, "SELECT COUNT(*) FROM matches WHERE status = 'confirmed'") == [(6,)]
    ratings = await stack.ratings.repo.get_stored((await stack.players.require(ADMIN)).id)
    assert ratings is not None
    assert ratings.games > 0


async def test_date_roster_before_only_moves_newer_rows(db: Database):
    seeding = Stack(db, frozenset({ADMIN}), clock=lambda: NOW)
    await seed_roster(seeding, entries(2), ADMIN, link_riot=False)
    await RosterRepository(db).date_roster_before(NOW + timedelta(days=1))  # later: nothing moves
    older = await rows(db, "SELECT MIN(created_at), MAX(created_at) FROM players")
    await RosterRepository(db).date_roster_before(NOW - timedelta(days=3))
    moved = await rows(db, "SELECT MIN(created_at), MAX(created_at) FROM players")
    assert moved[0][0] == moved[0][1]
    assert moved[0][0] < older[0][0]


async def test_seed_can_leave_the_ranked_history_to_the_bot(db: Database):
    world = FakeRiot()
    puuid = world.add_account("Ana", "EUW", "puuid-a")
    world.set_rank(puuid, "GOLD", "I")
    world.add_game(puuid, "EUW1_1", "MIDDLE", NOW - timedelta(days=2))
    people = [RosterEntry(1, "Ana", "GOLD", "I", {}, ("Ana#EUW",), "euw1", None)]
    lines: list[str] = []

    stack = Stack(db, frozenset({1}), riot_client=make_client(world), sync_history=False)
    report = await seed_roster(stack, people, 1, progress=lines.append)

    assert report.linked == ["Ana: Ana#EUW"]
    assert lines == ["[1/1] Ana"]
    assert await rows(db, "SELECT COUNT(*) FROM riot_role_games") == [(0,)]
    assert await rows(db, "SELECT COUNT(*) FROM riot_accounts WHERE history_synced_at IS NULL") == [
        (1,)
    ]
    # the bot continues from there when it starts
    assert await stack.riot.resume_unfinished_syncs() == 1


async def test_a_full_reset_removes_every_trace_of_the_league_and_keeps_the_rest(db: Database):
    world = FakeRiot()
    puuid = world.add_account("Amigo00", "EUW", "puuid-0")
    world.add_game(puuid, "EUW1_1", "MIDDLE", NOW - timedelta(days=2))
    people = entries(14)
    people[0] = RosterEntry(
        ADMIN, "Amigo00", "SILVER", "II", people[0].roles, ("Amigo00#EUW",), "euw1", None
    )
    clock = Clock(NOW - timedelta(hours=400))
    stack = Stack(db, frozenset({ADMIN}), riot_client=make_client(world), clock=clock, seed=3)
    await seed_roster(stack, people, ADMIN)
    await stack.riot.wait_for_background()
    for sql in (
        "INSERT INTO names (name) VALUES ('Zeca')",
        "INSERT INTO phrases (text) VALUES ('uma frase')",
    ):
        await db.conn.execute(sql)
    await db.conn.commit()
    play = Playground(stack, 77, people, ADMIN, clock, 3, NOW)
    await play.prepare()
    await play.play_history(games=4, newcomers=1)
    champions_before = await rows(db, "SELECT COUNT(*) FROM champions")
    assert champions_before != [(0,)]

    repo, players = RosterRepository(db), PlayerRepository(db)
    plan = await plan_reset(repo, players, False)
    assert plan.roster.players == 14
    assert plan.roster.riot_accounts == 1
    assert plan.roster.riot_games == 1

    await reset_league(repo, players, stack.ratings, False)

    for table in (
        "players", "player_roles", "rating_events", "rank_signals", "ratings", "rating_history",
        "riot_accounts", "riot_role_games", "lobbies", "lobby_entries", "matches",
        "match_participants", "match_audit",
    ):  # fmt: skip
        assert await rows(db, f"SELECT COUNT(*) FROM {table}") == [(0,)], table
    assert await rows(db, "SELECT name FROM names") == [("Zeca",)]
    assert await rows(db, "SELECT text FROM phrases") == [("uma frase",)]
    assert await rows(db, "SELECT COUNT(*) FROM champions") == champions_before

    # an account linked by hand before is free again, and the roster seeds cleanly
    fresh = Stack(db, frozenset({ADMIN}), riot_client=make_client(world))
    report = await seed_roster(fresh, people, ADMIN)
    assert len(report.created) == 14
    assert report.linked == ["Amigo00: Amigo00#EUW"]
    assert report.problems == []


# --- the games report ---------------------------------------------------------------------------


def lopsided(match_id: int, expected: float, winner: str | None, status: str = "confirmed") -> Game:
    blue = {r: Slot(f"B-{r}", 30.0 + i, None) for i, r in enumerate(ROLES)}
    red = {r: Slot(f"R-{r}", 28.0 + i, None) for i, r in enumerate(ROLES)}
    return Game(match_id, status, "2026-06-01T12:00:00", winner, expected, False, blue, red)


def test_a_game_is_shown_lane_by_lane_with_the_gap_and_the_result():
    text = format_game(lopsided(7, 0.70, "red"))
    assert text.startswith("#7  2026-06-01 12:00  confirmed  winner: red")
    assert "expected: blue 70% / red 30%" in text
    top = next(line for line in text.splitlines() if line.strip().startswith("TOP"))
    assert "B-TOP (30.0)" in top
    assert "R-TOP (28.0)" in top
    assert top.rstrip().endswith("+2.0")
    assert "result: the underdog won" in text
    assert "total" in text


def test_an_even_game_is_a_toss_up_and_unfinished_games_have_no_verdict():
    assert "a toss-up" in format_game(lopsided(1, 0.505, "blue"))
    assert "result:" not in format_game(lopsided(2, 0.6, None, status="in_progress"))


def test_the_summary_counts_only_finished_games():
    games = [
        lopsided(1, 0.50, "blue"),  # near even, no favourite
        lopsided(2, 0.60, "blue"),  # favourite won
        lopsided(3, 0.70, "red"),  # underdog won
        lopsided(4, 0.90, None, status="in_progress"),  # not judged
        lopsided(5, 0.95, None, status="voided"),  # not judged
    ]
    text = summarize(games)
    assert "Finished games judged: 3 (of 5 listed)" in text
    assert "Near-even games (expected 45-55%): 1 (33%)" in text
    assert "Favourite won 1 of 2 games that had one" in text
    assert "Most lopsided game: #3 (70% for blue)" in text
    assert "Average gap per lane: 2.0" in text
    assert summarize([lopsided(1, 0.9, None, status="voided")]) == "No finished games to judge yet."


async def test_the_report_reads_a_real_database_file_while_it_is_open(tmp_path: Path):
    path = tmp_path / "kdbot.sqlite3"
    database = Database(str(path))
    await database.connect()
    try:
        people = entries(14)
        clock = Clock(NOW - timedelta(hours=100))
        stack = Stack(database, frozenset({ADMIN}), clock=clock, seed=3)
        await seed_roster(stack, people, ADMIN, link_riot=False)
        play = Playground(stack, 77, people, ADMIN, clock, 3, NOW)
        await play.prepare()
        await play.play_history(games=3, newcomers=0)

        games = load_games(path)  # the bot's connection is still open
        assert [g.status for g in games][:3] == ["confirmed"] * 3
        assert len(games) == 6
        first = games[0]
        assert set(first.blue) == set(ROLES)
        assert set(first.red) == set(ROLES)
        assert 0 < first.expected_blue_win < 1
        assert [g.id for g in load_games(path, match_id=2)] == [2]
        assert "Finished games judged: 3" in summarize(games)
    finally:
        await database.close()
