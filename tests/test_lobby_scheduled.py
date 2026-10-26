"""Scheduled lobbies: sign up in advance, reminders, start at the time, busy players, expiry."""

import asyncio
from datetime import timedelta

import aiosqlite
import pytest
import pytest_asyncio
from lobby_fakes import ADMIN, CHANNEL, GUILD, START, LobbyStack

from bot.db import MIGRATIONS, Database
from bot.features.lobby.service import LobbyParams
from bot.features.players.service import PlayerError

# lobby_fakes.START is 2026-07-01 20:00 UTC = 21:00 in Lisbon, so "22:00" is one hour ahead.
WHEN = "22:00"


@pytest_asyncio.fixture
async def stack(db: Database) -> LobbyStack:
    return LobbyStack(db)


async def schedule_full(stack: LobbyStack, size: int = 10, when: str = WHEN, creator: int = 1):
    """A scheduled lobby with `size` players signed up (registering them if needed)."""
    await stack.register(size)
    update = await stack.lobbies.schedule(GUILD, CHANNEL, creator, when)
    lobby_id = update.snapshot.lobby.id
    for i in range(creator + 1, size + 1):
        update = await stack.lobbies.join(lobby_id, i)
    return lobby_id, update


def at_start(stack: LobbyStack, minutes: float = 0) -> None:
    """Move the clock to the scheduled time (one hour after START) plus `minutes`."""
    stack.clock.now = START + timedelta(hours=1, minutes=minutes)


# --- creating ----


async def test_schedule_creates_a_scheduled_lobby_and_signs_the_creator_up(stack):
    await stack.register(3)
    update = await stack.lobbies.schedule(GUILD, CHANNEL, 1, WHEN, "  noite   de   flex  ")
    lobby = update.snapshot.lobby
    assert lobby.kind == "scheduled"
    assert lobby.status == "OPEN"
    assert lobby.scheduled_for == stack.clock.now + timedelta(hours=1)
    assert lobby.note == "noite de flex"
    assert lobby.reminder_stage == 0
    assert [v.discord_id for v in update.snapshot.roster] == [1]


async def test_the_time_is_validated_with_helpful_messages(stack):
    await stack.register(2)
    with pytest.raises(PlayerError, match="Falta a hora"):
        await stack.lobbies.schedule(GUILD, CHANNEL, 1, "amanhã")
    with pytest.raises(PlayerError, match="já passou"):
        await stack.lobbies.schedule(GUILD, CHANNEL, 1, "hoje 10:00")
    with pytest.raises(PlayerError, match="até 14 dias"):
        await stack.lobbies.schedule(GUILD, CHANNEL, 1, "2026-08-30 21:00")
    strict = stack.make_service(params=LobbyParams(min_lead_minutes=120))
    with pytest.raises(PlayerError, match="pelo menos"):
        await strict.schedule(GUILD, CHANNEL, 1, WHEN)
    with pytest.raises(PlayerError, match="não está registado"):
        await stack.lobbies.schedule(GUILD, CHANNEL, 404, WHEN)
    assert await stack.repo.list_scheduled(GUILD) == []  # nothing half-created


async def test_a_note_is_trimmed_and_optional(stack):
    await stack.register(1)
    long = await stack.lobbies.schedule(GUILD, CHANNEL, 1, WHEN, "x" * 300)
    assert long.snapshot.lobby.note == "x" * 100
    await stack.lobbies.cancel(long.snapshot.lobby.id, 1)
    blank = await stack.lobbies.schedule(GUILD, CHANNEL, 1, WHEN, "   ")
    assert blank.snapshot.lobby.note is None


async def test_several_scheduled_lobbies_and_an_open_one_coexist(stack):
    await stack.register(3)
    later = await stack.lobbies.schedule(GUILD, CHANNEL, 1, "amanhã 21:00")
    sooner = await stack.lobbies.schedule(GUILD, CHANNEL, 2, "23:00")
    spontaneous = await stack.lobbies.open(GUILD, CHANNEL, 3)
    listed = await stack.lobbies.list_scheduled(GUILD)
    assert [s.lobby.id for s in listed] == [sooner.snapshot.lobby.id, later.snapshot.lobby.id]
    assert spontaneous.snapshot.lobby.kind == "spontaneous"
    assert (await stack.lobbies.open_lobby(GUILD)).id == spontaneous.snapshot.lobby.id  # type: ignore[union-attr]
    assert await stack.lobbies.list_scheduled(GUILD + 1) == []


# --- signing up ahead of time ----


async def test_a_player_can_sign_up_for_several_scheduled_lobbies(stack):
    await stack.register(3)
    a = await stack.lobbies.schedule(GUILD, CHANNEL, 1, WHEN)
    b = await stack.lobbies.schedule(GUILD, CHANNEL, 2, "amanhã 21:00")
    await stack.lobbies.join(a.snapshot.lobby.id, 3)
    await stack.lobbies.join(b.snapshot.lobby.id, 3)  # the same player, a different night
    await stack.lobbies.join(b.snapshot.lobby.id, 1)  # creator of another one: fine too
    assert len((await stack.lobbies.snapshot(b.snapshot.lobby.id)).roster) == 3


async def test_a_future_sign_up_does_not_block_opening_a_spontaneous_lobby(stack):
    await stack.register(2)
    await stack.lobbies.schedule(GUILD, CHANNEL, 1, WHEN)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)  # player 1 is signed up for tonight's
    assert update.snapshot.lobby.kind == "spontaneous"


async def test_a_spontaneous_lobby_still_blocks_other_lobbies_for_its_players(stack):
    await stack.register(2)
    await stack.lobbies.open(GUILD, CHANNEL, 1)
    with pytest.raises(PlayerError, match="lobby ativa"):
        await stack.lobbies.open(GUILD + 1, CHANNEL, 1)


async def test_ten_sign_ups_do_not_start_a_ready_check_before_the_time(stack):
    lobby_id, update = await schedule_full(stack, 10)
    assert update.snapshot.lobby.status == "OPEN"
    assert "ready_check_started" not in update.events
    assert len(update.snapshot.roster) == 10
    assert await stack.lobbies.process_schedules() == []  # still too early (> 30 min away)


async def test_extra_sign_ups_are_reserves_in_signup_order(stack):
    lobby_id, update = await schedule_full(stack, 12)
    assert len(update.snapshot.roster) == 10
    assert [v.discord_id for v in update.snapshot.reserves] == [11, 12]


# --- reminders and the start ----


async def test_the_reminder_goes_out_once_thirty_minutes_before(stack):
    lobby_id, _ = await schedule_full(stack, 6)
    stack.clock.advance(minutes=29)  # 31 minutes to go
    assert await stack.lobbies.process_schedules() == []
    stack.clock.advance(minutes=1)  # 30 minutes to go
    [update] = await stack.lobbies.process_schedules()
    assert update.events == ["reminder"]
    assert len(update.snapshot.roster) == 6
    assert update.snapshot.lobby.reminder_stage == 1
    stack.clock.advance(minutes=5)
    assert await stack.lobbies.process_schedules() == []  # not again


async def test_a_reminder_is_not_repeated_after_a_restart(stack, db):
    await schedule_full(stack, 4)
    stack.clock.advance(minutes=31)
    assert len(await stack.lobbies.process_schedules()) == 1
    restarted = LobbyStack(db, stack.clock, seed=9)
    assert await restarted.lobbies.process_schedules() == []  # the stage is stored


async def test_at_the_time_ten_sign_ups_start_the_ready_check(stack):
    lobby_id, _ = await schedule_full(stack, 10)
    stack.clock.advance(minutes=31)
    await stack.lobbies.process_schedules()  # the reminder
    at_start(stack)
    [update] = await stack.lobbies.process_schedules()
    assert "ready_check_started" in update.events
    assert "short_of_players" not in update.events
    assert update.snapshot.lobby.status == "READY_CHECK"
    assert update.snapshot.lobby.ready_deadline == stack.clock.now + timedelta(seconds=120)
    assert await stack.lobbies.process_schedules() == []  # nothing further to do


async def test_a_scheduled_game_plays_out_like_any_other(stack):
    lobby_id, _ = await schedule_full(stack, 10)
    at_start(stack)
    await stack.lobbies.process_schedules()
    update = await stack.ready_everyone(lobby_id, list(range(1, 11)))
    assert "teams_proposed" in update.events
    assert update.snapshot.proposal is not None
    update = await stack.lobbies.confirm(lobby_id, 1)
    assert update.snapshot.lobby.status == "IN_PROGRESS"


async def test_short_of_players_is_announced_once_at_the_time(stack):
    lobby_id, _ = await schedule_full(stack, 7)
    at_start(stack)
    [update] = await stack.lobbies.process_schedules()
    assert update.events == ["short_of_players"]
    assert update.snapshot.lobby.status == "OPEN"  # it keeps waiting
    assert await stack.lobbies.process_schedules() == []
    stack.clock.advance(minutes=10)
    assert await stack.lobbies.process_schedules() == []  # not nagging every few seconds


async def test_the_tenth_sign_up_after_the_time_starts_the_ready_check_at_once(stack):
    lobby_id, _ = await schedule_full(stack, 9)
    at_start(stack)
    await stack.lobbies.process_schedules()
    await stack.register(1, start=10)
    update = await stack.lobbies.join(lobby_id, 10)
    assert "ready_check_started" in update.events
    assert update.snapshot.lobby.status == "READY_CHECK"


async def test_processing_the_same_moment_twice_at_once_acts_once(stack):
    lobby_id, _ = await schedule_full(stack, 10)
    at_start(stack)
    first, second = await asyncio.gather(
        stack.lobbies.process_schedules(), stack.lobbies.process_schedules()
    )
    assert len(first) + len(second) == 1
    snap = await stack.lobbies.snapshot(lobby_id)
    assert snap.lobby.status == "READY_CHECK"


async def test_a_ready_check_on_a_scheduled_lobby_can_time_out_back_to_open(stack):
    lobby_id, _ = await schedule_full(stack, 10)
    at_start(stack)
    await stack.lobbies.process_schedules()
    stack.clock.advance(seconds=121)
    [update] = await stack.lobbies.process_deadlines()
    assert update.snapshot.lobby.status == "OPEN"  # nobody pressed Pronto
    assert update.snapshot.roster == []
    # ...and since its time has passed, ten new sign-ups start a new ready-check straight away.
    update = None
    for i in range(1, 11):
        update = await stack.lobbies.join(lobby_id, i)
    assert update is not None
    assert update.snapshot.lobby.status == "READY_CHECK"


# --- people who are busy ----


async def start_a_game(stack: LobbyStack, players: list[int]) -> int:
    """Run a spontaneous lobby (created by players[0]) up to IN_PROGRESS."""
    update = await stack.lobbies.open(GUILD, CHANNEL, players[0])
    lobby_id = update.snapshot.lobby.id
    for i in players[1:]:
        await stack.lobbies.join(lobby_id, i)
    await stack.ready_everyone(lobby_id, players)
    await stack.lobbies.confirm(lobby_id, players[0])
    return lobby_id


async def test_someone_who_is_in_a_running_game_is_dropped_when_the_scheduled_lobby_starts(stack):
    await stack.register(19)
    scheduled = await stack.lobbies.schedule(GUILD, CHANNEL, 1, WHEN)
    scheduled_id = scheduled.snapshot.lobby.id
    for i in range(2, 11):
        await stack.lobbies.join(scheduled_id, i)  # all ten are signed up for 22:00
    await start_a_game(stack, [3, *range(11, 20)])  # player 3 is now busy in another game
    at_start(stack)
    [update] = await stack.lobbies.process_schedules()
    assert "dropped_busy" in update.events
    assert update.snapshot.lobby.status == "OPEN"  # nine left
    assert 3 not in [v.discord_id for v in update.snapshot.roster]
    assert "short_of_players" in update.events


async def test_a_busy_player_can_still_sign_up_for_a_future_lobby_but_not_an_imminent_one(stack):
    await stack.register(11)
    await start_a_game(stack, list(range(1, 11)))
    future = await stack.lobbies.schedule(GUILD, CHANNEL, 11, "amanhã 21:00")
    update = await stack.lobbies.join(future.snapshot.lobby.id, 1)  # busy now, game tomorrow: fine
    assert 1 in [v.discord_id for v in update.snapshot.roster]
    tonight = await stack.lobbies.schedule(GUILD, CHANNEL, 11, WHEN)
    at_start(stack, minutes=1)
    with pytest.raises(PlayerError, match="outra lobby"):
        await stack.lobbies.join(tonight.snapshot.lobby.id, 2)  # the start time has passed


# --- cancelling and expiring ----


async def test_creator_or_admin_can_cancel_a_scheduled_lobby(stack):
    lobby_id, _ = await schedule_full(stack, 3)
    with pytest.raises(PlayerError, match="criador"):
        await stack.lobbies.cancel(lobby_id, 2)
    update = await stack.lobbies.cancel(lobby_id, ADMIN, "mudança de planos")
    assert update.snapshot.lobby.status == "CANCELLED"
    assert await stack.lobbies.list_scheduled(GUILD) == []
    stack.clock.advance(hours=2)
    assert await stack.lobbies.process_schedules() == []  # a cancelled lobby sends nothing


async def test_a_lobby_for_next_week_is_not_idle_but_a_missed_one_expires(stack):
    await stack.register(2)
    update = await stack.lobbies.schedule(GUILD, CHANNEL, 1, "2026-07-10 21:00")
    stack.clock.advance(hours=48)
    assert await stack.lobbies.expire_idle() == []  # far from its time: not idle
    lobby_id = update.snapshot.lobby.id
    # Jump to well after the start time (21:00 Lisbon on the 10th is 20:00 UTC).
    stack.clock.now = stack.clock.now.replace(day=10, hour=20, minute=0) + timedelta(hours=3.1)
    [expired] = await stack.lobbies.expire_idle()
    assert expired.snapshot.lobby.id == lobby_id
    assert expired.snapshot.lobby.cancel_reason == "inatividade"


async def test_a_lobby_missed_by_hours_is_not_started_late(stack):
    await schedule_full(stack, 10)
    at_start(stack, minutes=4 * 60)  # the bot was down for four hours
    assert await stack.lobbies.process_schedules() == []  # too stale to start; expiry closes it
    [expired] = await stack.lobbies.expire_idle()
    assert expired.snapshot.lobby.status == "CANCELLED"


# --- migration ----


async def test_migration_7_upgrades_a_populated_version_6_database(tmp_path):
    path = tmp_path / "kdbot.sqlite3"
    async with aiosqlite.connect(path) as raw:
        for i, script in enumerate(MIGRATIONS[:6], start=1):
            await raw.executescript(f"BEGIN;{script};PRAGMA user_version = {i};COMMIT;")
        await raw.execute(
            "INSERT INTO lobbies (id, guild_id, channel_id, kind, status, created_by, created_at,"
            " updated_at) VALUES (1, 1, 1, 'spontaneous', 'OPEN', 1, 'x', 'x')"
        )
        await raw.commit()
    database = Database(path)
    await database.connect()
    try:
        async with database.conn.execute("SELECT status, reminder_stage, note FROM lobbies") as cur:
            assert list(await cur.fetchall()) == [("OPEN", 0, None)]
    finally:
        await database.close()
