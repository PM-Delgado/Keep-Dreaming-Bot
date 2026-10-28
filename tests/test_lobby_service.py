import asyncio
import json
from datetime import timedelta

import aiosqlite
import pytest
import pytest_asyncio
from lobby_fakes import ADMIN, CHANNEL, GUILD, LobbyStack

from bot.db import MIGRATIONS, Database
from bot.features.lobby.repository import OpenLobbyExists
from bot.features.lobby.service import LobbyParams, LobbyUpdate
from bot.features.players.service import PlayerError


@pytest_asyncio.fixture
async def stack(db: Database) -> LobbyStack:
    return LobbyStack(db)


def names(views) -> list[int]:
    return [v.discord_id for v in views]


async def lobby_status(stack: LobbyStack, lobby_id: int) -> str:
    lobby = await stack.repo.get(lobby_id)
    assert lobby is not None
    return lobby.status


# --- opening ----


async def test_opening_a_lobby_joins_the_creator(stack):
    await stack.register(3)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    snap = update.snapshot
    assert snap.lobby.status == "OPEN"
    assert snap.lobby.created_by == 1
    assert names(snap.roster) == [1]
    assert snap.reserves == []


async def test_one_open_lobby_per_server_but_other_servers_are_independent(stack):
    await stack.register(3)
    await stack.lobbies.open(GUILD, CHANNEL, 1)
    with pytest.raises(PlayerError, match="Já existe uma lobby aberta"):
        await stack.lobbies.open(GUILD, CHANNEL, 2)
    other = await stack.lobbies.open(GUILD + 1, CHANNEL, 2)
    assert other.snapshot.lobby.guild_id == GUILD + 1
    with pytest.raises(OpenLobbyExists):  # the database enforces it too, not just the service
        await stack.repo.create(GUILD, CHANNEL, "spontaneous", 3, stack.clock())


async def test_unregistered_or_busy_players_cannot_open_or_join(stack):
    await stack.register(2)
    with pytest.raises(PlayerError, match="não registado"):
        await stack.lobbies.open(GUILD, CHANNEL, 404)
    first = await stack.lobbies.open(GUILD, CHANNEL, 1)
    with pytest.raises(PlayerError, match="não registado"):
        await stack.lobbies.join(first.snapshot.lobby.id, 404)
    second = await stack.lobbies.open(GUILD + 1, CHANNEL, 2)
    with pytest.raises(PlayerError, match="outra lobby"):
        await stack.lobbies.join(first.snapshot.lobby.id, 2)  # already in a lobby elsewhere
    with pytest.raises(PlayerError, match="lobby ativa"):
        await stack.lobbies.open(GUILD + 2, CHANNEL, 1)
    assert second.snapshot.lobby.id != first.snapshot.lobby.id


# --- joining, leaving, reserves ----


async def test_duplicate_join_is_rejected_and_order_is_by_signup_time(stack):
    await stack.register(5)
    update = await stack.lobbies.open(GUILD, CHANNEL, 3)
    lobby_id = update.snapshot.lobby.id
    for i in (1, 5, 2):
        stack.clock.advance(seconds=1)
        update = await stack.lobbies.join(lobby_id, i)
    with pytest.raises(PlayerError, match="Já estás nesta lobby"):
        await stack.lobbies.join(lobby_id, 1)
    assert names(update.snapshot.roster) == [3, 1, 5, 2]


async def test_the_tenth_signup_starts_the_ready_check_exactly_once(stack):
    update = await stack.full_lobby(10)
    assert update.events.count("ready_check_started") == 1
    lobby = update.snapshot.lobby
    assert lobby.status == "READY_CHECK"
    assert lobby.ready_deadline == stack.clock.now + timedelta(seconds=120)
    assert sorted(json.loads(lobby.roster_json or "[]")) == list(range(1, 11))


async def test_an_eleventh_player_is_a_reserve(stack):
    update = await stack.full_lobby(10)
    await stack.register(1, start=11)
    update = await stack.lobbies.join(update.snapshot.lobby.id, 11)
    assert names(update.snapshot.roster) == list(range(1, 11))
    assert names(update.snapshot.reserves) == [11]
    assert update.snapshot.lobby.status == "READY_CHECK"  # joining as a reserve changes nothing


async def test_joining_after_leaving_goes_to_the_back_of_the_line(stack):
    await stack.register(4)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    for i in (2, 3):
        stack.clock.advance(seconds=1)
        await stack.lobbies.join(lobby_id, i)
    await stack.lobbies.leave(lobby_id, 1)
    stack.clock.advance(seconds=1)
    update = await stack.lobbies.join(lobby_id, 1)
    assert names(update.snapshot.roster) == [2, 3, 1]


async def test_leaving_when_not_a_member_is_an_error(stack):
    await stack.register(2)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    with pytest.raises(PlayerError, match="Não estás nesta lobby"):
        await stack.lobbies.leave(update.snapshot.lobby.id, 2)


async def test_ten_simultaneous_joins_create_one_ready_check(stack):
    await stack.register(10)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    results = await asyncio.gather(
        *(stack.lobbies.join(lobby_id, i) for i in range(2, 11)), return_exceptions=True
    )
    updates = [r for r in results if isinstance(r, LobbyUpdate)]
    assert len(updates) == 9  # nobody errored
    assert sum("ready_check_started" in u.events for u in updates) == 1
    snap = await stack.lobbies.snapshot(lobby_id)
    assert snap.lobby.status == "READY_CHECK"
    assert len(snap.roster) == 10


async def test_twelve_simultaneous_joins_make_ten_players_and_two_reserves(stack):
    await stack.register(12)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    await asyncio.gather(*(stack.lobbies.join(lobby_id, i) for i in range(2, 13)))
    snap = await stack.lobbies.snapshot(lobby_id)
    assert (len(snap.roster), len(snap.reserves)) == (10, 2)
    assert len({v.player_id for v in snap.roster + snap.reserves}) == 12  # nobody twice


async def test_the_same_player_joining_twice_at_once_only_counts_once(stack):
    await stack.register(2)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    results = await asyncio.gather(
        stack.lobbies.join(lobby_id, 2), stack.lobbies.join(lobby_id, 2), return_exceptions=True
    )
    assert sum(isinstance(r, PlayerError) for r in results) == 1
    assert len((await stack.lobbies.snapshot(lobby_id)).roster) == 2


# --- ready-check: ready, decline, timeout ----


async def test_only_roster_members_can_ready_and_only_once(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.register(1, start=11)
    await stack.lobbies.join(lobby_id, 11)  # reserve
    with pytest.raises(PlayerError, match="entre os 10"):
        await stack.lobbies.ready(lobby_id, 11)
    await stack.lobbies.ready(lobby_id, 1)
    with pytest.raises(PlayerError, match="Já confirmaste"):
        await stack.lobbies.ready(lobby_id, 1)


async def test_ready_before_the_ready_check_is_refused(stack):
    await stack.register(2)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    with pytest.raises(PlayerError, match="Não há ready-check"):
        await stack.lobbies.ready(update.snapshot.lobby.id, 1)


async def test_everyone_ready_proposes_teams(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    update = await stack.ready_everyone(lobby_id, list(range(1, 11)))
    assert "teams_proposed" in update.events
    snap = update.snapshot
    assert snap.lobby.status == "TEAMS_PROPOSED"
    assert snap.proposal is not None
    both = [s.player_id for s in snap.proposal.blue + snap.proposal.red]
    assert sorted(both) == list(range(1, 11))
    assert [s.role for s in snap.proposal.blue] == ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"]
    assert snap.proposal.band in ("even", "slight", "clear", "lopsided")


async def test_simultaneous_final_ready_presses_make_one_proposal(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.ready_everyone(lobby_id, list(range(1, 6)))
    results = await asyncio.gather(*(stack.lobbies.ready(lobby_id, i) for i in range(6, 11)))
    assert sum("teams_proposed" in r.events for r in results) == 1
    assert await lobby_status(stack, lobby_id) == "TEAMS_PROPOSED"


async def test_declining_backfills_from_the_reserves_with_a_fresh_round(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.register(1, start=11)
    await stack.lobbies.join(lobby_id, 11)
    await stack.ready_everyone(lobby_id, [1, 2, 3])
    stack.clock.advance(seconds=100)
    update = await stack.lobbies.leave(lobby_id, 4)  # 4 declines
    assert "roster_changed" in update.events
    snap = update.snapshot
    assert snap.lobby.status == "READY_CHECK"
    assert names(snap.roster) == [1, 2, 3, 5, 6, 7, 8, 9, 10, 11]  # the reserve took the place
    assert [v.status for v in snap.roster[:3]] == ["ready"] * 3  # earlier confirmations stay
    assert snap.lobby.ready_deadline == stack.clock.now + timedelta(seconds=120)


async def test_declining_without_reserves_returns_to_open_and_clears_ready_marks(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.ready_everyone(lobby_id, [1, 2, 3])
    update = await stack.lobbies.leave(lobby_id, 4)
    assert "back_to_open" in update.events
    snap = update.snapshot
    assert snap.lobby.status == "OPEN"
    assert snap.lobby.ready_deadline is None
    assert snap.lobby.roster_json is None
    assert all(v.status == "signed_up" for v in snap.roster)
    # ...and when someone joins again the ready-check restarts from scratch.
    await stack.register(1, start=11)
    update = await stack.lobbies.join(lobby_id, 11)
    assert "ready_check_started" in update.events


async def test_deadline_drops_non_responders_and_backfills(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.register(2, start=11)
    for i in (11, 12):
        await stack.lobbies.join(lobby_id, i)
    await stack.ready_everyone(lobby_id, [1, 2, 3, 4, 5, 6, 7, 8])  # 9 and 10 stay silent
    assert await stack.lobbies.process_deadlines() == []  # not yet
    stack.clock.advance(seconds=121)
    updates = await stack.lobbies.process_deadlines()
    assert len(updates) == 1
    snap = updates[0].snapshot
    assert "timeout" in updates[0].events
    assert snap.lobby.status == "READY_CHECK"
    assert names(snap.roster) == [1, 2, 3, 4, 5, 6, 7, 8, 11, 12]
    assert [v.status for v in snap.roster[:8]] == ["ready"] * 8
    assert snap.lobby.ready_deadline == stack.clock.now + timedelta(seconds=120)
    assert await stack.lobbies.process_deadlines() == []  # a fresh deadline: nothing to do


async def test_deadline_without_enough_reserves_returns_to_open(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.ready_everyone(lobby_id, [1, 2, 3, 4, 5, 6, 7, 8, 9])  # only 10 is silent
    stack.clock.advance(seconds=121)
    [update] = await stack.lobbies.process_deadlines()
    assert update.snapshot.lobby.status == "OPEN"
    assert names(update.snapshot.roster) == list(range(1, 10))
    assert all(v.status == "signed_up" for v in update.snapshot.roster)


async def test_a_deadline_that_arrives_twice_is_applied_once(stack):
    update = await stack.full_lobby(10)
    stack.clock.advance(seconds=121)
    first, second = await asyncio.gather(
        stack.lobbies.process_deadlines(), stack.lobbies.process_deadlines()
    )
    assert len(first) + len(second) == 1
    snap = await stack.lobbies.snapshot(update.snapshot.lobby.id)
    assert snap.lobby.status == "OPEN"
    assert snap.roster == []


# --- proposals come from the players' profiles ----


async def test_leaving_after_the_proposal_goes_back_to_ready_check(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.register(1, start=11)
    await stack.lobbies.join(lobby_id, 11)
    await stack.ready_everyone(lobby_id, list(range(1, 11)))
    assert await lobby_status(stack, lobby_id) == "TEAMS_PROPOSED"
    update = await stack.lobbies.leave(lobby_id, 7)
    assert update.snapshot.lobby.status == "READY_CHECK"
    assert update.snapshot.proposal is None
    assert 11 in names(update.snapshot.roster)


async def declare(stack: LobbyStack, player: int, profile: dict[str, int]) -> None:
    for role, value in profile.items():
        await stack.players.set_declared(player, role, value)


def slot_of(proposal, player_id: int):
    return next(s for s in (*proposal.blue, *proposal.red) if s.player_id == player_id)


async def test_players_cannot_choose_roles_and_teams_cannot_be_rerolled(stack):
    # Roles come only from profile proficiency; nothing in the service lets a player pick one,
    # and there is no way to ask for a different split (cancel and reopen instead).
    for gone in ("set_pref", "set_pref_player", "reroll"):
        assert not hasattr(stack.lobbies, gone)
    assert not hasattr(stack.repo, "set_pref")


async def test_roles_follow_each_players_profile(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    mains = ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"] * 2
    for i, main in enumerate(mains, start=1):
        await declare(stack, i, {role: (5 if role == main else 2) for role in mains[:5]})
    update = await stack.ready_everyone(lobby_id, list(range(1, 11)))
    proposal = update.snapshot.proposal
    assert proposal is not None
    for i, main in enumerate(mains, start=1):
        slot = slot_of(proposal, i)
        assert slot.role == main
        assert slot.fit == slot.best == 5  # everyone is on their best role


async def test_five_junglers_one_per_team_others_on_their_next_best_role(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    seconds = {1: "TOP", 2: "MID", 3: "ADC", 4: "SUPPORT", 5: "TOP"}
    for i in range(1, 6):
        await declare(stack, i, {"JUNGLE": 5, seconds[i]: 4})
    for i, main in zip(range(6, 11), ["TOP", "MID", "ADC", "SUPPORT", "TOP"], strict=True):
        await declare(stack, i, {main: 5})
    update = await stack.ready_everyone(lobby_id, list(range(1, 11)))
    proposal = update.snapshot.proposal
    assert proposal is not None
    in_jungle = [s.player_id for s in (*proposal.blue, *proposal.red) if s.role == "JUNGLE"]
    assert len(in_jungle) == 2
    assert {s.player_id for s in proposal.blue} & set(in_jungle) != set(in_jungle)  # split up
    for i in range(1, 6):
        slot = slot_of(proposal, i)
        if i not in in_jungle:
            assert slot.fit is not None
            assert slot.fit >= 4  # a role they play well, shown as a note on the message
            assert slot.best == 5


async def test_the_message_data_flags_who_is_off_their_best_role(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    for i in range(1, 11):  # everyone mains MID: only two can have it
        await declare(stack, i, {"MID": 5, "TOP": 3, "JUNGLE": 3, "ADC": 3, "SUPPORT": 3})
    update = await stack.ready_everyone(lobby_id, list(range(1, 11)))
    proposal = update.snapshot.proposal
    assert proposal is not None
    off = [s for s in (*proposal.blue, *proposal.red) if s.fit != s.best]
    assert len(off) == 8
    assert all(s.fit == 3 and s.best == 5 for s in off)


async def test_the_balancer_does_not_block_the_event_loop(stack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    await stack.ready_everyone(lobby_id, list(range(1, 11)))
    task.cancel()
    assert ticks >= 3  # the loop kept running while teams were being computed (~0.3 s)


# --- confirm, cancel ----


async def proposed(stack: LobbyStack):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.ready_everyone(lobby_id, list(range(1, 11)))
    return lobby_id


async def test_confirm_starts_the_game_once_and_only_for_creator_or_admin(stack):
    lobby_id = await proposed(stack)
    with pytest.raises(PlayerError, match="criador"):
        await stack.lobbies.confirm(lobby_id, 4)
    update = await stack.lobbies.confirm(lobby_id, 1)
    assert update.snapshot.lobby.status == "IN_PROGRESS"
    assert update.snapshot.proposal is not None  # the confirmed teams stay on record
    with pytest.raises(PlayerError, match="Não há equipas"):
        await stack.lobbies.confirm(lobby_id, 1)


async def test_simultaneous_confirms_start_the_game_once(stack):
    lobby_id = await proposed(stack)
    results = await asyncio.gather(
        stack.lobbies.confirm(lobby_id, 1),
        stack.lobbies.confirm(lobby_id, ADMIN),
        return_exceptions=True,
    )
    assert sum(isinstance(r, PlayerError) for r in results) == 1
    assert await lobby_status(stack, lobby_id) == "IN_PROGRESS"


async def test_a_started_game_blocks_its_players_but_not_a_new_lobby(stack):
    lobby_id = await proposed(stack)
    await stack.lobbies.confirm(lobby_id, 1)
    with pytest.raises(PlayerError, match="lobby ativa"):
        await stack.lobbies.open(GUILD, CHANNEL, 3)  # player 3 is in the running game
    await stack.register(2, start=11)
    fresh = await stack.lobbies.open(GUILD, CHANNEL, 11)  # others can start another one
    assert fresh.snapshot.lobby.id != lobby_id
    with pytest.raises(PlayerError, match="começou"):
        await stack.lobbies.leave(lobby_id, 3)


async def test_cancel_frees_everyone_and_rejects_further_joins(stack):
    lobby_id = await proposed(stack)
    with pytest.raises(PlayerError, match="criador"):
        await stack.lobbies.cancel(lobby_id, 6)
    update = await stack.lobbies.cancel(lobby_id, ADMIN, "teste")
    assert update.snapshot.lobby.status == "CANCELLED"
    assert update.snapshot.lobby.cancel_reason == "teste"
    assert update.snapshot.lobby.closed_at is not None
    with pytest.raises(PlayerError, match="já está fechada"):
        await stack.lobbies.cancel(lobby_id, 1)
    with pytest.raises(PlayerError, match="já não aceita"):
        await stack.lobbies.join(lobby_id, 1)
    again = await stack.lobbies.open(GUILD, CHANNEL, 6)  # players are free again
    assert again.snapshot.lobby.id != lobby_id


# --- time and persistence ----


async def test_idle_lobbies_expire_but_active_ones_do_not(stack):
    await stack.register(3)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    stack.clock.advance(hours=2.9)
    assert await stack.lobbies.expire_idle() == []
    await stack.lobbies.join(lobby_id, 2)  # activity resets the idle timer
    stack.clock.advance(hours=2.9)
    assert await stack.lobbies.expire_idle() == []
    stack.clock.advance(hours=0.2)
    [expired] = await stack.lobbies.expire_idle()
    assert expired.snapshot.lobby.status == "CANCELLED"
    assert expired.snapshot.lobby.cancel_reason == "inatividade"


async def test_a_started_game_without_a_result_eventually_expires(stack):
    lobby_id = await proposed(stack)
    await stack.lobbies.confirm(lobby_id, 1)
    stack.clock.advance(hours=11)
    assert not [u for u in await stack.lobbies.process_matches() if "voided" in u.events]
    stack.clock.advance(hours=1.5)
    [expired] = [u for u in await stack.lobbies.process_matches() if "voided" in u.events]
    assert expired.snapshot.lobby.status == "CANCELLED"
    assert expired.snapshot.lobby.cancel_reason == "sem resultado"


async def test_state_survives_a_restart(stack, db):
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    await stack.ready_everyone(lobby_id, [1, 2, 3, 4, 5, 6, 7, 8, 9])
    restarted = LobbyStack(db, stack.clock, seed=2)  # a new process: only the database remains
    snap = await restarted.lobbies.snapshot(lobby_id)
    assert snap.lobby.status == "READY_CHECK"
    update = await restarted.lobbies.ready(lobby_id, 10)
    assert "teams_proposed" in update.events
    found = await restarted.lobbies.lobby_for_message(123)
    assert found is None
    await restarted.lobbies.attach_message(lobby_id, 123, CHANNEL)
    found = await restarted.lobbies.lobby_for_message(123)
    assert found is not None
    assert found.id == lobby_id


async def test_custom_ready_check_length_is_used(db):
    stack = LobbyStack(db)
    stack.lobbies = stack.make_service(params=LobbyParams(ready_check_seconds=30))
    update = await stack.full_lobby(10)
    assert update.snapshot.lobby.ready_deadline == stack.clock.now + timedelta(seconds=30)


async def test_migration_5_upgrades_a_populated_version_4_database(tmp_path):
    path = tmp_path / "kdbot.sqlite3"
    async with aiosqlite.connect(path) as raw:
        for i, script in enumerate(MIGRATIONS[:4], start=1):
            await raw.executescript(f"BEGIN;{script};PRAGMA user_version = {i};COMMIT;")
        await raw.execute(
            "INSERT INTO players (id, discord_id, nickname, created_at) VALUES (1, 1, 'A', 'x')"
        )
        await raw.commit()
    database = Database(path)
    await database.connect()
    try:
        async with database.conn.execute("SELECT nickname, is_mock FROM players") as cur:
            assert list(await cur.fetchall()) == [("A", 0)]  # existing players are real
        async with database.conn.execute("PRAGMA foreign_key_check") as cur:
            assert list(await cur.fetchall()) == []
    finally:
        await database.close()


async def test_migration_6_drops_the_unused_preference_table_and_keeps_lobbies(tmp_path):
    path = tmp_path / "kdbot.sqlite3"
    async with aiosqlite.connect(path) as raw:
        for i, script in enumerate(MIGRATIONS[:5], start=1):
            await raw.executescript(f"BEGIN;{script};PRAGMA user_version = {i};COMMIT;")
        await raw.execute(
            "INSERT INTO players (id, discord_id, nickname, created_at) VALUES (1, 1, 'A', 'x')"
        )
        await raw.execute(
            "INSERT INTO lobbies (id, guild_id, channel_id, kind, status, created_by, created_at,"
            " updated_at) VALUES (1, 1, 1, 'spontaneous', 'OPEN', 1, 'x', 'x')"
        )
        await raw.execute("INSERT INTO lobby_entries VALUES (1, 1, 'signed_up', 'x')")
        await raw.execute("INSERT INTO lobby_entry_roles VALUES (1, 1, 'MID', 'prefer')")
        await raw.commit()
    database = Database(path)
    await database.connect()
    try:
        async with database.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'lobby_entry_roles'"
        ) as cur:
            assert list(await cur.fetchall()) == []
        async with database.conn.execute("SELECT status FROM lobbies") as cur:
            assert list(await cur.fetchall()) == [("OPEN",)]
        async with database.conn.execute("SELECT COUNT(*) FROM lobby_entries") as cur:
            assert list(await cur.fetchall()) == [(1,)]
    finally:
        await database.close()


async def test_a_ready_check_asks_the_riot_module_to_refresh_its_players(stack):
    seen: list[list[int]] = []
    stack.lobbies.rank_refresher = lambda ids: seen.append(sorted(ids))
    update = await stack.full_lobby(10)
    lobby_id = update.snapshot.lobby.id
    assert seen == [list(range(1, 11))]  # once, as the ready-check starts
    await stack.lobbies.leave(lobby_id, 4)  # back to open: nothing to refresh
    assert len(seen) == 1
    await stack.register(1, start=11)
    await stack.lobbies.join(lobby_id, 11)  # a new ready-check with a different roster
    assert len(seen) == 2 and 11 in seen[1] and 4 not in seen[1]
