"""Automatic Riot refresh: profile views, ready-checks, the timer, and the rejected-key alert."""

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio
from riot_fakes import NOW, FakeRiot, Sleeper, Stack, days_ago

from bot.features.riot.client import RateLimiter, RiotAuthError, RiotClient
from bot.features.riot.cog import KEY_ALERT, KeyAlert

ADMIN = 99


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def world() -> FakeRiot:
    w = FakeRiot()
    w.add_account("Alpha", "EUW", "puuid-alpha")
    w.add_account("Beta", "EUW", "puuid-beta")
    return w


@pytest_asyncio.fixture
async def stack(db, world) -> Stack:
    s = Stack(db, world)
    await s.register(1, "Ana")
    await s.register(2, "Rui")
    return s


def tick(stack: Stack) -> Clock:
    """A clock the test can move, shared by the Riot and the rating services."""
    clock = Clock()
    stack.riot.clock = clock
    stack.ratings.clock = clock
    return clock


async def player_id(stack: Stack, discord_id: int) -> int:
    return (await stack.players.require(discord_id)).id


async def riot_signals(stack: Stack, pid: int):
    return await stack.rows(
        "SELECT tier, division FROM rank_signals WHERE player_id = ? AND source = 'riot'"
        " ORDER BY id",
        pid,
    )


async def link(stack: Stack, discord_id: int, riot_id: str) -> None:
    await stack.riot.link(discord_id, riot_id, None, ADMIN)
    await stack.riot.wait_for_background()


# --- an unchanged rank is not stored again --------------------------------------------------


async def test_an_unchanged_rank_is_stored_at_most_once_a_day(stack, world, caplog):
    clock = tick(stack)
    world.set_rank("puuid-alpha", "GOLD", "II")
    await link(stack, 1, "Alpha#EUW")
    pid = await player_id(stack, 1)
    for _ in range(3):
        await stack.riot.refresh_rank(pid, None)
    assert len(await riot_signals(stack, pid)) == 1  # the link stored it; the rest were copies

    caplog.set_level(logging.INFO, logger="bot.features.ratings.service")
    world.set_rank("puuid-alpha", "GOLD", "I")  # a change is always stored, and logged
    await stack.riot.refresh_rank(pid, None)
    assert len(await riot_signals(stack, pid)) == 2
    assert "Rank changed for player" in caplog.text and "GOLD II -> GOLD I" in caplog.text

    clock.advance(hours=25)  # the same rank again, but the last copy is more than a day old
    await stack.riot.refresh_rank(pid, None)
    assert len(await riot_signals(stack, pid)) == 3


# --- /profile view ----------------------------------------------------------------------------


async def test_a_profile_view_refreshes_a_stale_player_and_then_leaves_it_alone(stack, world):
    clock = tick(stack)
    world.set_rank("puuid-alpha", "SILVER", "I")
    await link(stack, 1, "Alpha#EUW")
    world.accounts[("alpha", "euw")]["gameName"] = "AlphaRenamed"
    world.set_rank("puuid-alpha", "GOLD", "IV")

    assert await stack.riot.view_refresh_due(1) is True
    await stack.riot.refresh_for_view(1)
    assert (await stack.riot.accounts(1))[0][0].riot_id == "AlphaRenamed#EUW"
    pid = await player_id(stack, 1)
    rank = (await stack.ratings.resolve(pid)).rank
    assert rank is not None and rank.label() == "Gold IV"

    assert await stack.riot.view_refresh_due(1) is False  # checked just now
    calls = len(world.calls)
    clock.advance(minutes=29)
    assert await stack.riot.view_refresh_due(1) is False
    assert len(world.calls) == calls
    clock.advance(minutes=2)  # 31 minutes since the check
    assert await stack.riot.view_refresh_due(1) is True


async def test_nothing_is_due_for_unknown_players_players_without_accounts_or_without_a_key(
    db, world
):
    keyless = Stack(db, world, enabled=False)
    await keyless.register(1, "Ana")
    assert await keyless.riot.view_refresh_due(1) is False
    keyless.riot.spawn_rank_refresh([1, 2])
    assert not keyless.riot._tasks  # type: ignore[attr-defined]

    with_key = Stack(db, world)
    assert await with_key.riot.view_refresh_due(404) is False  # not registered
    assert await with_key.riot.view_refresh_due(1) is False  # registered, no accounts linked


async def test_history_is_resynced_in_the_background_only_when_it_is_old(stack, world):
    clock = tick(stack)
    world.add_game("puuid-alpha", "G1", "MIDDLE", days_ago(NOW, 1))
    await link(stack, 1, "Alpha#EUW")

    async def games() -> int:
        return (await stack.rows("SELECT COUNT(*) FROM riot_role_games"))[0][0]

    assert await games() == 1
    world.add_game("puuid-alpha", "G2", "MIDDLE", days_ago(NOW, 0.5))
    clock.advance(minutes=31)  # stale for the rank, but the history is only 31 minutes old
    await stack.riot.refresh_for_view(1)
    await stack.riot.wait_for_background()
    assert await games() == 1
    clock.advance(hours=7)
    await stack.riot.refresh_for_view(1)
    await stack.riot.wait_for_background()
    assert await games() == 2


async def test_a_riot_failure_never_breaks_a_view_and_is_retried_next_time(stack, world, caplog):
    await link(stack, 1, "Alpha#EUW")
    caplog.set_level(logging.INFO, logger="bot.features.riot.service")
    world.forced["/lol/league"] = 500
    await stack.riot.refresh_for_view(1)  # no exception
    assert "failed" in caplog.text and "RiotUnavailable" in caplog.text
    assert await stack.riot.view_refresh_due(1) is True  # nothing was recorded as checked
    del world.forced["/lol/league"]
    await stack.riot.refresh_for_view(1)
    assert await stack.riot.view_refresh_due(1) is False


async def test_a_slow_lookup_is_not_waited_for_beyond_the_limit(stack, world, caplog):
    await link(stack, 1, "Alpha#EUW")
    caplog.set_level(logging.INFO, logger="bot.features.riot.service")
    stack.riot.view_wait_s = 0
    await stack.riot.refresh_for_view(1)  # gives up waiting at once...
    assert "still running" in caplog.text
    await stack.riot.wait_for_background()  # ...but the lookup finishes by itself
    assert await stack.riot.view_refresh_due(1) is False


# --- ready-check ------------------------------------------------------------------------------


async def test_a_ready_check_refreshes_only_stale_ranks_and_not_the_names(stack, world):
    clock = tick(stack)
    await link(stack, 1, "Alpha#EUW")
    await link(stack, 2, "Beta#EUW")
    first, second = await player_id(stack, 1), await player_id(stack, 2)
    stack.riot._last_checked[first] = clock.now  # type: ignore[attr-defined]  # checked just now
    ranks, names = world.count("/lol/league"), world.count("/riot/account")

    stack.riot.spawn_rank_refresh([first, second])
    await stack.riot.wait_for_background()
    assert world.count("/lol/league") == ranks + 1  # only the stale player
    assert world.count("/riot/account") == names  # names are not looked up here


# --- the timer --------------------------------------------------------------------------------


class SpyLimiter(RateLimiter):
    def __init__(self, flags: list[bool]) -> None:
        super().__init__(per_second=10_000, per_window=1_000_000, reserve=0)
        self.flags = flags

    async def acquire(self, background: bool = False) -> None:
        self.flags.append(background)
        await super().acquire(background)


def spy_client(stack: Stack, world: FakeRiot) -> list[bool]:
    flags: list[bool] = []
    stack.riot.client = RiotClient("k", world, SpyLimiter(flags), Sleeper())
    return flags


async def test_the_timer_checks_everyone_as_background_work(stack, world, caplog):
    await link(stack, 1, "Alpha#EUW")
    await link(stack, 2, "Beta#EUW")
    flags = spy_client(stack, world)
    caplog.set_level(logging.INFO, logger="bot.features.riot.service")
    await stack.riot.refresh_all()
    await stack.riot.wait_for_background()
    assert flags and all(flags)  # never competes with a command for the request budget
    assert "Riot refresh cycle: 2 of 2 players checked, 0 failed" in caplog.text
    for discord_id in (1, 2):
        assert await stack.riot.view_refresh_due(discord_id) is False


async def test_a_command_refresh_uses_the_interactive_budget(stack, world):
    await link(stack, 1, "Alpha#EUW")
    flags = spy_client(stack, world)
    await stack.riot.refresh_for_view(1)
    await stack.riot.wait_for_background()
    assert False in flags  # the names and the rank are not background work


@pytest.mark.parametrize(
    ("status", "reason"), [(403, "the API key was rejected"), (429, "rate limiting")]
)
async def test_the_timer_stops_at_the_first_sign_that_going_on_would_only_fail(
    stack, world, caplog, status, reason
):
    await link(stack, 1, "Alpha#EUW")
    await link(stack, 2, "Beta#EUW")
    world.forced["/lol/league"] = status
    ranks = world.count("/lol/league")
    caplog.set_level(logging.INFO, logger="bot.features.riot.service")
    await stack.riot.refresh_all()
    assert world.count("/lol/league") - ranks <= 3  # one player's attempts (with retries), not two
    warning = next(r for r in caplog.records if "refresh cycle" in r.message)
    assert warning.levelno == logging.WARNING
    assert "stopped early" in warning.message and reason in warning.message


async def test_the_timer_counts_a_failed_player_and_goes_on(stack, world, caplog):
    await link(stack, 1, "Alpha#EUW")
    await link(stack, 2, "Beta#EUW")
    del world.accounts[("alpha", "euw")]  # Riot no longer knows the first account
    caplog.set_level(logging.INFO, logger="bot.features.riot.service")
    await stack.riot.refresh_all()
    assert "1 of 2 players checked, 1 failed" in caplog.text


# --- the champion pool ------------------------------------------------------------------------


async def test_the_champion_pool_adds_up_the_games_of_every_linked_account(stack, world):
    for i, champion in enumerate(["Ahri", "Ahri", "Ahri", "Zed"]):
        world.add_game("puuid-alpha", f"A{i}", "MIDDLE", days_ago(NOW, i + 1), champion=champion)
    for i, champion in enumerate(["Ahri", "Ahri", "Lux", "Lux", "Lux", "Lux"]):
        world.add_game("puuid-beta", f"B{i}", "MIDDLE", days_ago(NOW, i + 1), champion=champion)
    await link(stack, 1, "Alpha#EUW")  # the main account
    await link(stack, 1, "Beta#EUW")
    pid = await player_id(stack, 1)
    # The second account's games count too: Lux (4) is not in the main account's top 3 at all.
    assert await stack.riot.champion_pool(pid, 3) == [("Ahri", 5), ("Lux", 4), ("Zed", 1)]


# --- the rejected-key alert -------------------------------------------------------------------


def alert_bot(admin_ids=frozenset({7}), fail: Exception | None = None):
    sent: list[str] = []

    async def send(text: str) -> None:
        if fail:
            raise fail
        sent.append(text)

    user = SimpleNamespace(send=send)
    bot = SimpleNamespace(
        settings=SimpleNamespace(admin_ids=admin_ids), fetch_user=AsyncMock(return_value=user)
    )
    return bot, sent


async def settle(alert: KeyAlert) -> None:
    await asyncio.gather(*list(alert._tasks))  # type: ignore[attr-defined]


async def test_a_rejected_key_messages_the_admins_once_a_day_and_logs_hourly(caplog):
    bot, sent = alert_bot()
    clock = Clock()
    alert = KeyAlert(bot, clock)  # type: ignore[arg-type]
    caplog.set_level(logging.INFO, logger="bot.features.riot.cog")
    for _ in range(5):
        alert.rejected()
    await settle(alert)
    assert sent == [KEY_ALERT]
    assert caplog.text.count("lookups fail until it is fixed") == 1

    clock.advance(hours=2)  # logged again, but nobody is messaged a second time
    alert.rejected()
    assert caplog.text.count("lookups fail until it is fixed") == 2
    assert not alert._tasks  # type: ignore[attr-defined]
    clock.advance(hours=23)  # a day since the first message
    alert.rejected()
    await settle(alert)
    assert sent == [KEY_ALERT, KEY_ALERT]


async def test_an_admin_with_closed_messages_does_not_break_anything(caplog):
    response: Any = SimpleNamespace(status=403, reason="Forbidden")
    bot, sent = alert_bot(fail=discord.HTTPException(response, "closed"))
    alert = KeyAlert(bot, Clock())  # type: ignore[arg-type]
    caplog.set_level(logging.INFO, logger="bot.features.riot.cog")
    alert.rejected()
    await settle(alert)
    assert sent == [] and "Could not message admin 7" in caplog.text


async def test_the_client_reports_every_rejected_request(world):
    told: list[int] = []
    world.forced["/lol/league"] = 403
    client = RiotClient(
        "k",
        world,
        RateLimiter(per_second=10_000, per_window=1_000_000),
        Sleeper(),
        on_auth_error=lambda: told.append(1),
    )
    with pytest.raises(RiotAuthError):
        await client.solo_rank("puuid-alpha", "euw1")
    assert told == [1]
