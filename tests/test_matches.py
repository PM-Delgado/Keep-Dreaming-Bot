"""Matches: teams confirmed -> result reported -> confirmed -> ratings; voids and corrections."""

import asyncio

import pytest
import pytest_asyncio
from lobby_fakes import ADMIN, GUILD, LobbyStack, sides_of, start_game

from bot.db import Database
from bot.features.matches.service import MatchParams
from bot.features.players.service import PlayerError


@pytest_asyncio.fixture
async def stack(db: Database) -> LobbyStack:
    return LobbyStack(db)


async def mus(stack: LobbyStack) -> dict[int, float]:
    return {r.player_id: r.mu for r in await stack.ratings.repo.list_stored()}


# --- creation ----


async def test_confirming_the_teams_creates_a_match_with_ten_participants(stack):
    lobby_id, match = await start_game(stack)
    people = await stack.match_repo.participants(match.id)
    assert match.status == "in_progress"
    assert match.winner is None
    assert 0 < match.expected_blue_win < 1
    assert len(people) == 10
    for side in ("blue", "red"):
        roles = [p.role for p in people if p.side == side]
        assert sorted(roles) == sorted(["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"])
    assert all(p.h_mu_before > 0 and p.est_sigma > 0 for p in people)
    lobby = await stack.repo.get(lobby_id)
    assert lobby is not None
    assert lobby.status == "IN_PROGRESS"


async def test_a_failure_creating_the_match_leaves_the_lobby_unconfirmed(stack, monkeypatch):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.ready_everyone(lobby_id, list(range(1, 11)))

    async def boom(*_a, **_k):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(stack.matches, "start", boom)
    with pytest.raises(RuntimeError):
        await stack.lobbies.confirm(lobby_id, 1)
    lobby = await stack.repo.get(lobby_id)
    assert lobby is not None
    assert lobby.status == "TEAMS_PROPOSED"  # rolled back together with the match
    assert await stack.match_repo.get_by_lobby(lobby_id) is None


async def test_confirming_twice_creates_one_match(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.ready_everyone(lobby_id, list(range(1, 11)))
    results = await asyncio.gather(
        stack.lobbies.confirm(lobby_id, 1),
        stack.lobbies.confirm(lobby_id, ADMIN),
        return_exceptions=True,
    )
    assert sum(isinstance(r, PlayerError) for r in results) == 1
    match = await stack.match_repo.get_by_lobby(lobby_id)
    assert match is not None
    assert len(await stack.match_repo.participants(match.id)) == 10


# --- report and confirm ----


async def test_a_participant_reports_and_someone_on_the_other_team_confirms(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    before = await mus(stack)

    update = await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "reported"
    assert update.events == ["reported"]
    assert await mus(stack) == before  # nothing counts until it is confirmed

    update = await stack.lobbies.confirm_result(lobby_id, sides["red"][0])
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "confirmed"
    assert "result_confirmed" in update.events
    assert update.snapshot.lobby.status == "COMPLETED"
    after = await mus(stack)
    assert all(after[p] > before[p] for p in sides["blue"])
    assert all(after[p] < before[p] for p in sides["red"])
    stored = await stack.ratings.repo.get_stored(sides["blue"][0])
    assert stored is not None
    assert stored.games == 1


async def test_strangers_cannot_report_or_confirm(stack):
    lobby_id, match = await start_game(stack)
    await stack.register(1, start=50)  # registered, but not in this game
    with pytest.raises(PlayerError, match="quem jogou"):
        await stack.lobbies.report_result(lobby_id, 50, "blue")
    with pytest.raises(PlayerError):
        await stack.lobbies.report_result(lobby_id, 777, "blue")  # not even registered
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    with pytest.raises(PlayerError, match="quem jogou"):
        await stack.lobbies.confirm_result(lobby_id, 50)


async def test_the_reporting_team_cannot_confirm_its_own_report(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    with pytest.raises(PlayerError, match="outra equipa"):
        await stack.lobbies.confirm_result(lobby_id, sides["blue"][1])
    with pytest.raises(PlayerError, match="outra equipa"):
        await stack.lobbies.confirm_result(lobby_id, sides["blue"][0])


async def test_someone_else_cannot_override_a_report_but_the_reporter_can_change_it(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    with pytest.raises(PlayerError, match="Contestar"):
        await stack.lobbies.report_result(lobby_id, sides["red"][0], "red")
    update = await stack.lobbies.report_result(lobby_id, sides["blue"][0], "red")  # a mistake
    assert update.snapshot.match is not None
    assert update.snapshot.match.winner == "red"


async def test_an_admin_report_is_final_at_once(stack):
    lobby_id, match = await start_game(stack)
    await stack.register(1, start=ADMIN)  # the admin is registered but does not play
    update = await stack.lobbies.report_result(lobby_id, ADMIN, "red")
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "confirmed"
    assert update.snapshot.lobby.status == "COMPLETED"
    assert update.snapshot.match.confirmed_by is not None


async def test_a_closed_result_cannot_be_reported_again(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    await stack.lobbies.confirm_result(lobby_id, sides["red"][0])
    with pytest.raises(PlayerError, match="fechado"):
        await stack.lobbies.report_result(lobby_id, sides["blue"][0], "red")
    with pytest.raises(PlayerError, match="confirmado"):
        await stack.lobbies.confirm_result(lobby_id, sides["red"][1])


async def test_two_simultaneous_confirmations_count_once(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    results = await asyncio.gather(
        stack.lobbies.confirm_result(lobby_id, sides["red"][0]),
        stack.lobbies.confirm_result(lobby_id, sides["red"][1]),
        return_exceptions=True,
    )
    assert sum(isinstance(r, PlayerError) for r in results) == 1
    stored = await stack.ratings.repo.get_stored(sides["blue"][0])
    assert stored is not None
    assert stored.games == 1  # not two games


# --- contest and automatic confirmation ----


async def test_a_contested_result_needs_an_admin(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.register(1, start=ADMIN)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    update = await stack.lobbies.contest_result(lobby_id, sides["red"][0])
    assert update.snapshot.match is not None
    assert update.snapshot.match.contested
    with pytest.raises(PlayerError, match="já foi contestado"):
        await stack.lobbies.contest_result(lobby_id, sides["red"][1])
    with pytest.raises(PlayerError, match="admin"):
        await stack.lobbies.confirm_result(lobby_id, sides["red"][1])
    update = await stack.lobbies.confirm_result(lobby_id, ADMIN)
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "confirmed"


async def test_an_admin_decides_a_contested_result_with_the_winner_buttons(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.register(1, start=ADMIN)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    await stack.lobbies.contest_result(lobby_id, sides["red"][0])
    update = await stack.lobbies.report_result(lobby_id, ADMIN, "red")
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "confirmed"
    assert update.snapshot.match.winner == "red"


async def test_a_quiet_report_is_confirmed_automatically_after_thirty_minutes(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    stack.clock.advance(minutes=29)
    assert await stack.lobbies.process_matches() == []
    stack.clock.advance(minutes=2)
    [update] = await stack.lobbies.process_matches()
    assert "auto_confirmed" in update.events
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "confirmed"
    assert update.snapshot.match.auto_confirmed
    assert update.snapshot.match.confirmed_by is None
    assert update.snapshot.lobby.status == "COMPLETED"
    assert await stack.lobbies.process_matches() == []  # once


async def test_a_contested_report_is_never_auto_confirmed(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    await stack.lobbies.contest_result(lobby_id, sides["red"][0])
    stack.clock.advance(hours=5)
    assert await stack.lobbies.process_matches() == []
    after = await stack.match_repo.get(match.id)
    assert after is not None
    assert after.status == "reported"


async def test_contesting_and_auto_confirming_at_the_same_moment_stay_consistent(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    stack.clock.advance(minutes=31)
    await asyncio.gather(
        stack.lobbies.contest_result(lobby_id, sides["red"][0]),
        stack.lobbies.process_matches(),
        return_exceptions=True,
    )
    final = await stack.match_repo.get(match.id)
    assert final is not None
    assert (final.status, final.contested) in {("confirmed", False), ("reported", True)}
    stored = await stack.ratings.repo.get_stored(sides["blue"][0])
    assert stored is not None
    assert stored.games == (1 if final.status == "confirmed" else 0)


# --- reminders, expiry, voids ----


async def test_an_unreported_game_gets_one_reminder_then_expires_and_frees_the_players(stack):
    await start_game(stack)
    stack.clock.advance(minutes=89)
    assert await stack.lobbies.process_matches() == []
    stack.clock.advance(minutes=2)
    [reminder] = await stack.lobbies.process_matches()
    assert reminder.events == ["result_reminder"]
    assert await stack.lobbies.process_matches() == []  # only once
    stack.clock.advance(hours=11)
    [expired] = await stack.lobbies.process_matches()
    assert expired.events == ["voided"]
    assert expired.snapshot.lobby.status == "CANCELLED"
    assert expired.snapshot.lobby.cancel_reason == "sem resultado"
    assert await stack.repo.busy_lobby_of(1) is None  # free to play again
    assert all(r.games == 0 for r in await stack.ratings.repo.list_stored())


async def test_the_creator_can_cancel_a_game_that_has_no_result_yet(stack):
    lobby_id, match = await start_game(stack)
    update = await stack.lobbies.cancel(lobby_id, 1)
    assert update.snapshot.lobby.status == "CANCELLED"
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "voided"


async def test_after_a_report_only_an_admin_can_cancel_the_game(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.register(1, start=ADMIN)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    with pytest.raises(PlayerError, match="admin"):
        await stack.lobbies.cancel(lobby_id, 1)
    update = await stack.lobbies.cancel(lobby_id, ADMIN)
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "voided"


async def test_voiding_a_confirmed_game_takes_its_effect_off_the_ratings(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.register(1, start=ADMIN)
    initial = await mus(stack)
    await stack.lobbies.report_result(lobby_id, ADMIN, "blue")
    assert await mus(stack) != initial
    update = await stack.lobbies.void_match(match.id, ADMIN, "erro")
    assert update.snapshot.lobby.status == "CANCELLED"
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "voided"
    assert await mus(stack) == initial
    stored = await stack.ratings.repo.get_stored(sides["blue"][0])
    assert stored is not None
    assert stored.games == 0
    with pytest.raises(PlayerError, match="anulado"):
        await stack.lobbies.void_match(match.id, ADMIN, "outra vez")


async def test_only_an_admin_can_void_by_match_number(stack):
    lobby_id, match = await start_game(stack)
    with pytest.raises(PlayerError, match="admin"):
        await stack.lobbies.void_match(match.id, 1, "quero")


async def test_fixing_a_confirmed_result_equals_the_game_always_having_gone_the_other_way(stack):
    lobby_id, match = await start_game(stack)
    await stack.register(1, start=ADMIN)
    await stack.lobbies.report_result(lobby_id, ADMIN, "blue")
    update = await stack.lobbies.fix_match(match.id, ADMIN, "red")
    assert update.events == ["fixed"]
    assert update.snapshot.match is not None
    assert update.snapshot.match.winner == "red"
    corrected = await mus(stack)

    fresh = Database(":memory:")
    await fresh.connect()
    try:
        other = LobbyStack(fresh)
        lobby2, match2 = await start_game(other)
        await other.register(1, start=ADMIN)
        await other.lobbies.report_result(lobby2, ADMIN, "red")  # red won from the start
        assert await sides_of(other, match2.id) == await sides_of(stack, match.id)
        expected = await mus(other)
    finally:
        await fresh.close()
    assert corrected == pytest.approx(expected)


async def test_fixing_to_the_same_winner_is_refused(stack):
    lobby_id, match = await start_game(stack)
    await stack.register(1, start=ADMIN)
    await stack.lobbies.report_result(lobby_id, ADMIN, "blue")
    with pytest.raises(PlayerError, match="já é o resultado"):
        await stack.lobbies.fix_match(match.id, ADMIN, "blue")
    with pytest.raises(PlayerError, match="admin"):
        await stack.lobbies.fix_match(match.id, 1, "red")


async def test_fixing_a_pending_result_decides_it(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.register(1, start=ADMIN)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    await stack.lobbies.contest_result(lobby_id, sides["red"][0])
    update = await stack.lobbies.fix_match(match.id, ADMIN, "red")
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "confirmed"
    assert update.snapshot.match.winner == "red"


# --- ratings and the visible rank ----


async def test_everyone_is_unranked_until_the_fifth_confirmed_game(stack):
    updates = []
    for game in range(5):
        lobby_id, _ = await start_game(stack)
        await stack.register(1, start=ADMIN)  # after the ten, so players keep ids 1..10
        updates.append(
            await stack.lobbies.report_result(lobby_id, ADMIN, "red" if game % 2 else "blue")
        )
        placed = [r.placed for r in await stack.ratings.repo.list_stored() if r.player_id <= 10]
        assert all(placed) if game == 4 else not any(placed)
    assert all(not c.after.placed for c in updates[0].changes)  # only placement progress
    final = [c for c in updates[-1].changes if c.player_id <= 10]
    assert len(final) == 10
    assert all(c.after.placed and c.before is not None and not c.before.placed for c in final)


async def test_the_stored_ratings_equal_a_full_replay_after_many_games(stack):
    await stack.register(1, start=ADMIN)
    for game in range(4):
        lobby_id, _ = await start_game(stack)
        await stack.lobbies.report_result(lobby_id, ADMIN, "red" if game % 3 else "blue")
    stored = await stack.ratings.repo.list_stored()
    await stack.ratings.rebuild()
    assert await stack.ratings.repo.list_stored() == stored


async def test_the_balancer_snapshot_is_frozen_with_the_match(stack):
    lobby_id, match = await start_game(stack)

    async def snapshot():
        people = await stack.match_repo.participants(match.id)
        return {p.player_id: (p.h_mu_before, p.est_mu) for p in people}

    before = await snapshot()
    await stack.register(1, start=ADMIN)
    await stack.lobbies.report_result(lobby_id, ADMIN, "blue")
    assert await snapshot() == before  # an explanation of what the balancer saw, never rewritten


async def test_players_in_a_started_game_are_busy_until_the_result_is_in(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    with pytest.raises(PlayerError, match="lobby ativa"):
        await stack.lobbies.open(GUILD, 1, 1)
    await stack.register(1, start=ADMIN)
    await stack.lobbies.report_result(lobby_id, ADMIN, "blue")
    await stack.lobbies.open(GUILD, 1, sides["blue"][0])  # free again once the result is in


# --- restarts, timings and audit ----


async def test_a_pending_result_survives_a_restart(stack, db):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    restarted = LobbyStack(db, stack.clock, seed=2)
    stack.clock.advance(minutes=31)
    [update] = await restarted.lobbies.process_matches()
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "confirmed"


async def test_the_auto_confirm_delay_is_configurable(stack):
    stack.with_match_params(MatchParams(auto_confirm_minutes=5))
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    stack.clock.advance(minutes=6)
    [update] = await stack.lobbies.process_matches()
    assert update.snapshot.match is not None
    assert update.snapshot.match.status == "confirmed"


async def test_the_audit_trail_records_who_did_what(stack):
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, sides["blue"][0], "blue")
    await stack.lobbies.confirm_result(lobby_id, sides["red"][0])
    audit = await stack.match_repo.audit(match.id)
    assert [a.action for a in audit] == ["report", "confirm"]
    assert audit[0].actor_id == sides["blue"][0]
    assert audit[0].new_value == "blue"


# --- migration ----


async def test_migration_8_adds_the_match_tables_to_a_populated_version_7_database(tmp_path):
    import aiosqlite

    from bot.db import MIGRATIONS

    path = tmp_path / "kdbot.sqlite3"
    async with aiosqlite.connect(path) as raw:
        for i, script in enumerate(MIGRATIONS[:7], start=1):
            await raw.executescript(f"BEGIN;{script};PRAGMA user_version = {i};COMMIT;")
        await raw.execute(
            "INSERT INTO lobbies (id, guild_id, channel_id, kind, status, created_by, created_at,"
            " updated_at) VALUES (1, 1, 1, 'spontaneous', 'IN_PROGRESS', 1, 'x', 'x')"
        )
        await raw.commit()
    database = Database(path)
    await database.connect()
    try:
        async with database.conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone()) == (len(MIGRATIONS),)
        async with database.conn.execute("SELECT COUNT(*) FROM matches") as cur:
            assert (await cur.fetchone()) == (0,)
        async with database.conn.execute("PRAGMA foreign_key_check") as cur:
            assert list(await cur.fetchall()) == []
    finally:
        await database.close()


async def test_a_game_started_before_matches_existed_still_expires_instead_of_blocking_players(
    stack,
):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    # What an M2 database holds: a started lobby with no match row.
    await stack.repo.transition(lobby_id, ["OPEN", "READY_CHECK"], "IN_PROGRESS", stack.clock())
    assert await stack.lobbies.expire_idle() == []
    stack.clock.advance(hours=13)
    [expired] = await stack.lobbies.expire_idle()
    assert expired.snapshot.lobby.status == "CANCELLED"
    assert expired.snapshot.lobby.cancel_reason == "sem resultado"
    assert await stack.repo.busy_lobby_of(1) is None
