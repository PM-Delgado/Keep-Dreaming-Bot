"""Interactive Riot commands must not starve behind background history syncs."""

import asyncio

import pytest
from riot_fakes import NOW, FakeRiot, Stack, days_ago

from bot.features.riot.client import HttpResponse, RateLimiter, RiotClient, RiotNotFound


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def limiter(t: FakeTime, per_window=4, reserve=2, window_s=10.0) -> RateLimiter:
    return RateLimiter(
        per_second=1000,
        per_window=per_window,
        window_s=window_s,
        reserve=reserve,
        clock=t.clock,
        sleep=t.sleep,
    )


async def test_background_work_stops_at_the_reserve_but_interactive_continues():
    t = FakeTime()
    lim = limiter(t)  # 4 per window, 2 reserved for interactive
    await lim.acquire(background=True)
    await lim.acquire(background=True)
    assert t.sleeps == []
    await lim.acquire()  # interactive: still inside the reserved share, no wait
    await lim.acquire()
    assert t.sleeps == []
    await lim.acquire(background=True)  # the window is full: wait for the oldest to expire
    assert t.sleeps == [pytest.approx(10.0)]


async def test_background_alone_never_uses_the_reserved_share():
    t = FakeTime()
    lim = limiter(t)
    for _ in range(2):
        await lim.acquire(background=True)
    await lim.acquire(background=True)  # third background call must wait a full window
    assert t.sleeps == [pytest.approx(10.0)]


async def test_a_waiting_background_request_does_not_block_an_interactive_one():
    gate = asyncio.Event()
    now = [0.0]

    async def sleep(_seconds: float) -> None:
        await gate.wait()  # the background waiter sleeps until the test releases it
        now[0] += 10.0

    lim = RateLimiter(
        per_second=1000, per_window=3, window_s=10.0, reserve=1, clock=lambda: now[0], sleep=sleep
    )
    await lim.acquire(background=True)
    await lim.acquire(background=True)
    waiter = asyncio.create_task(lim.acquire(background=True))  # capped at 2: has to wait
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not waiter.done()
    await asyncio.wait_for(lim.acquire(), timeout=1)  # interactive gets through while it waits
    gate.set()
    await asyncio.wait_for(waiter, timeout=1)


def test_reserve_must_leave_room_for_background_work():
    with pytest.raises(ValueError):
        RateLimiter(per_window=10, reserve=10)
    with pytest.raises(ValueError):
        RateLimiter(per_window=10, reserve=-1)


class SpyLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__(per_second=10_000, per_window=1_000_000, reserve=1)
        self.flags: list[bool] = []

    async def acquire(self, background: bool = False) -> None:
        self.flags.append(background)
        await super().acquire(background)


async def test_only_history_calls_are_background():
    spy = SpyLimiter()
    responses = {
        "by-riot-id": {"puuid": "p", "gameName": "A", "tagLine": "EUW"},
        "by-puuid/p/ids": ["EUW1_1"],
    }

    async def transport(url: str, headers):
        if "by-riot-id" in url:
            return HttpResponse(200, {}, responses["by-riot-id"])
        if "/entries/by-puuid" in url or "/summoners/by-puuid" in url:
            return HttpResponse(200, {}, [])
        if url.endswith("/ids") or "/ids?" in url:
            return HttpResponse(200, {}, responses["by-puuid/p/ids"])
        return HttpResponse(404, {}, None)

    client = RiotClient("key", transport, spy)
    await client.account_by_riot_id("A", "EUW", "euw1")
    await client.has_summoner("p", "euw1")
    await client.solo_rank("p", "euw1")
    assert spy.flags == [False, False, False]
    spy.flags.clear()
    await client.ranked_match_ids("p", "euw1")
    with pytest.raises(RiotNotFound):  # the stub has no such match; only the flag matters
        await client.ranked_game("EUW1_1", "p", "euw1")
    assert spy.flags == [True, True]


# --- resuming unfinished syncs --------------------------------------------------------------


async def test_unfinished_syncs_are_resumed_at_startup(db):
    world = FakeRiot()
    for name in ("Alpha", "Beta"):
        world.add_account(name, "EUW", f"puuid-{name.lower()}")
    for i in range(3):
        world.add_game("puuid-alpha", f"A_{i}", "MIDDLE", days_ago(NOW, i + 1))
        world.add_game("puuid-beta", f"B_{i}", "TOP", days_ago(NOW, i + 1))
    stack = Stack(db, world)
    await stack.register(1, "Ana")
    alpha = await stack.riot.link(1, "Alpha#EUW", None, 99)
    beta = await stack.riot.link(1, "Beta#EUW", None, 99)
    await stack.riot.wait_for_background()

    # Simulate a restart that cut Beta's first sync short: nothing stored, never marked done.
    await db.conn.execute(
        "DELETE FROM riot_role_games WHERE riot_account_id = ?", (beta.account.id,)
    )
    await db.conn.execute(
        "UPDATE riot_accounts SET history_synced_at = NULL WHERE id = ?", (beta.account.id,)
    )
    await db.conn.commit()

    assert await stack.riot.resume_unfinished_syncs() == 1  # Alpha is complete, only Beta resumes
    await stack.riot.wait_for_background()
    assert await stack.riot_repo.count_games(beta.account.id) == 3
    assert await stack.riot_repo.count_games(alpha.account.id) == 3
    refreshed = await stack.riot_repo.get(beta.account.id)
    assert refreshed is not None
    assert refreshed.history_synced_at is not None
    assert await stack.riot.resume_unfinished_syncs() == 0  # nothing left to resume


async def test_resume_does_nothing_without_a_key(db):
    world = FakeRiot()
    world.add_account("Alpha", "EUW", "puuid-alpha")
    on = Stack(db, world)
    await on.register(1, "Ana")
    await on.riot.link(1, "Alpha#EUW", None, 99)
    await on.riot.wait_for_background()
    await db.conn.execute("UPDATE riot_accounts SET history_synced_at = NULL")
    await db.conn.commit()
    off = Stack(db, world, enabled=False)
    calls = len(world.calls)
    assert await off.riot.resume_unfinished_syncs() == 0
    assert len(world.calls) == calls
