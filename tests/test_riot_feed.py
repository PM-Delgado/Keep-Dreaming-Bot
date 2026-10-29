"""The post-game feed: the poll cycle, the LP change, what is skipped, and what it stores."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import discord
import pytest
import pytest_asyncio
from riot_fakes import NOW, FakeRiot, Stack
from test_riot_card import make_cog as make_card_cog
from test_riot_cog import FakeInteraction, member
from test_riot_history import stored
from test_riot_leaderboard import Channel
from test_riot_refresh import tick

from bot.features.riot.client import SoloRank
from bot.features.riot.cog import Riot
from bot.features.riot.history import format_line, to_entry
from bot.features.riot.service import FEED_RANK_LAG, FeedItem


class Posts:
    """The `announce` callback: remembers what was posted, can be told to fail."""

    def __init__(self, ok: bool = True) -> None:
        self.items: list[FeedItem] = []
        self.ok = ok

    async def __call__(self, item: FeedItem) -> bool:
        if not self.ok:
            return False
        self.items.append(item)
        return True

    @property
    def ids(self) -> list[str]:
        return [i.source.game.match_id for i in self.items]

    @property
    def deltas(self) -> list[int | None]:
        return [i.lp_delta for i in self.items]


def finish(
    world: FakeRiot,
    clock: Any,
    puuid: str,
    match_id: str,
    ended_minutes_ago: float,
    duration: int = 1800,
    **kwargs: Any,
) -> None:
    """A ranked game that ended `ended_minutes_ago` minutes before the (moving) clock."""
    start = clock.now - timedelta(minutes=ended_minutes_ago, seconds=duration)
    world.add_game(puuid, match_id, "MIDDLE", start, duration=duration, **kwargs)


@pytest.fixture
def world() -> FakeRiot:
    w = FakeRiot()
    w.add_account("Alpha", "EUW", "puuid-alpha")
    w.add_account("Beta", "EUW", "puuid-beta")
    w.set_rank("puuid-alpha", "MASTER", "I", 100)
    w.set_rank("puuid-beta", "GOLD", "II", 40)
    return w


@pytest_asyncio.fixture
async def stack(db, world) -> Stack:
    s = Stack(db, world)
    await s.register(1, "Ana")
    await s.register(2, "Rui")
    return s


@pytest.fixture
def clock(stack):
    return tick(stack)


async def start_watching(stack: Stack, clock: Any, both: bool = False) -> None:
    """Link Ana's account (and Rui's), let the history sync, and run the first cycle: the feed
    now watches them with the rank they have."""
    await stack.riot.link(1, "Alpha#EUW", None, 1)
    if both:
        await stack.riot.link(2, "Beta#EUW", None, 2)
    await stack.riot.wait_for_background()
    summary = await stack.riot.poll_new_games(Posts())
    assert summary.started == (2 if both else 1)


async def alpha(stack: Stack):
    return (await stack.riot_repo.list_accounts((await stack.players.require(1)).id))[0]


async def pending(stack: Stack, account) -> list[str]:
    return [g.match_id for g in await stack.riot_repo.pending_games(account.id, account.feed_since)]


# --- starting to watch --------------------------------------------------------------------------


async def test_the_first_cycle_only_remembers_the_rank_and_never_posts_old_games(
    stack, world, clock
):
    finish(world, clock, "puuid-alpha", "EUW1_OLD", 20)
    await stack.riot.link(1, "Alpha#EUW", None, 1)
    await stack.riot.wait_for_background()
    posts = Posts()
    summary = await stack.riot.poll_new_games(posts)
    assert (summary.watched, summary.started, summary.posted) == (1, 1, 0)
    account = await alpha(stack)
    assert account.feed_since == NOW
    assert account.feed_rank == SoloRank("MASTER", None, 100)
    # A stored game that ended before the feed started is never announced, however long we wait.
    clock.advance(minutes=5)
    assert (await stack.riot.poll_new_games(posts)).posted == 0
    assert posts.items == [] and await pending(stack, account) == []


async def test_a_new_game_is_posted_once_with_its_lp_change(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1, champion="Yone")
    world.set_rank("puuid-alpha", "MASTER", "I", 127)
    posts = Posts()
    summary = await stack.riot.poll_new_games(posts)
    assert summary.posted == 1 and posts.ids == ["EUW1_1"]
    item = posts.items[0]
    assert item.lp_delta == 27
    assert item.discord_id == 1 and item.source.nickname == "Ana"
    assert item.source.riot_id == "Alpha#EUW"
    assert item.source.rank == SoloRank("MASTER", None, 127)
    assert item.source.game.champion == "Yone"
    # What it remembers: the baseline moved, the game is handled, history can show the LP, and
    # the leaderboard's rank of the main account is as fresh as the feed's.
    account = await alpha(stack)
    assert account.feed_rank == SoloRank("MASTER", None, 127)
    rows = await stack.rows(
        "SELECT announced_at, lp_delta, rank_tier, rank_lp FROM riot_role_games"
        " WHERE riot_match_id = 'EUW1_1'"
    )
    assert rows[0][1:] == (27, "MASTER", 127) and rows[0][0] is not None
    saved = await stack.rows(
        "SELECT rank_tier, rank_lp FROM riot_accounts WHERE id = ?", account.id
    )
    assert saved == [("MASTER", 127)]
    history = await stack.riot.recent_history(1)
    assert [g.lp_delta for g in history.games] == [27]
    # Nothing is posted twice.
    clock.advance(minutes=5)
    assert (await stack.riot.poll_new_games(posts)).posted == 0
    assert posts.ids == ["EUW1_1"]


async def test_a_loss_has_a_negative_lp_change_and_promotions_count_across_divisions(
    stack, world, clock
):
    world.set_rank("puuid-beta", "GOLD", "II", 90)
    await start_watching(stack, clock, both=True)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_A", 1, win=False)
    finish(world, clock, "puuid-beta", "EUW1_B", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 81)
    world.set_rank("puuid-beta", "GOLD", "I", 10)  # 90 LP in Gold II -> 10 LP in Gold I
    posts = Posts()
    await stack.riot.poll_new_games(posts)
    assert dict(zip(posts.ids, posts.deltas, strict=True)) == {"EUW1_A": -19, "EUW1_B": 20}


# --- when Riot has not caught up ----------------------------------------------------------------


async def test_a_game_waits_for_the_rank_to_update_then_is_posted_without_a_guess(
    stack, world, clock
):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 2)  # the rank has not moved yet
    posts = Posts()
    summary = await stack.riot.poll_new_games(posts)
    assert summary.waiting == 1 and summary.posted == 0 and posts.items == []
    assert await pending(stack, await alpha(stack)) == ["EUW1_1"]
    # Riot catches up: the next cycle has the real number.
    clock.advance(minutes=3)
    world.set_rank("puuid-alpha", "MASTER", "I", 118)
    await stack.riot.poll_new_games(posts)
    assert posts.deltas == [18]


async def test_after_the_wait_a_game_whose_rank_never_moved_is_posted_with_no_lp_number(
    stack, world, clock
):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 2)
    posts = Posts()
    await stack.riot.poll_new_games(posts)
    assert posts.items == []
    clock.advance(minutes=FEED_RANK_LAG.total_seconds() / 60)  # now older than the wait
    summary = await stack.riot.poll_new_games(posts)
    assert summary.posted == 1 and posts.deltas == [0]  # 0 = unknown: the card shows no number


# --- several games, remakes, old games ----------------------------------------------------------


async def test_several_new_games_at_once_get_no_lp_number_and_only_the_last_shows_a_rank(
    stack, world, clock
):
    await start_watching(stack, clock)
    clock.advance(minutes=10)
    finish(world, clock, "puuid-alpha", "EUW1_3", 8)
    finish(world, clock, "puuid-alpha", "EUW1_4", 3)
    world.set_rank("puuid-alpha", "MASTER", "I", 150)
    posts = Posts()
    summary = await stack.riot.poll_new_games(posts)
    assert summary.posted == 2
    assert posts.ids == ["EUW1_3", "EUW1_4"]  # oldest first
    assert posts.deltas == [None, None]  # a sum of two games would be a wrong number
    assert [i.source.rank for i in posts.items] == [None, SoloRank("MASTER", None, 150)]
    assert (await alpha(stack)).feed_rank == SoloRank("MASTER", None, 150)
    rows = await stack.rows("SELECT riot_match_id, lp_delta, rank_lp FROM riot_role_games")
    assert {r[0]: r[1:] for r in rows} == {"EUW1_3": (None, None), "EUW1_4": (None, 150)}


async def test_remakes_are_handled_without_a_post(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(minutes=10)
    finish(world, clock, "puuid-alpha", "EUW1_R1", 8, duration=200)  # under five minutes
    finish(world, clock, "puuid-alpha", "EUW1_R2", 5, early=True)  # surrendered early
    posts = Posts()
    summary = await stack.riot.poll_new_games(posts)
    assert summary.posted == 0 and summary.skipped == 2 and posts.items == []
    assert await pending(stack, await alpha(stack)) == []  # never looked at again


async def test_a_game_that_ended_hours_ago_is_not_announced(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(hours=4)  # the bot was down, say
    finish(world, clock, "puuid-alpha", "EUW1_OLD", 180)  # ended 3 hours ago
    finish(world, clock, "puuid-alpha", "EUW1_NEW", 3)
    world.set_rank("puuid-alpha", "MASTER", "I", 120)
    posts = Posts()
    summary = await stack.riot.poll_new_games(posts)
    assert posts.ids == ["EUW1_NEW"] and summary.skipped == 1
    assert posts.deltas == [20]  # the old one was set aside, so this is the only live game


async def test_a_game_another_trigger_already_stored_is_still_announced(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 110)
    await stack.riot.sync_history(await alpha(stack))  # e.g. a /riot history view got there first
    posts = Posts()
    await stack.riot.poll_new_games(posts)
    assert posts.ids == ["EUW1_1"] and posts.deltas == [10]


# --- failures -----------------------------------------------------------------------------------


async def test_a_game_that_could_not_be_posted_waits_and_keeps_the_baseline(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 127)
    refusing = Posts(ok=False)
    summary = await stack.riot.poll_new_games(refusing)
    assert summary.failed == 1 and summary.posted == 0
    account = await alpha(stack)
    assert account.feed_rank == SoloRank("MASTER", None, 100)  # unchanged, so the LP stays right
    assert await pending(stack, account) == ["EUW1_1"]
    clock.advance(minutes=5)
    working = Posts()
    await stack.riot.poll_new_games(working)
    assert working.ids == ["EUW1_1"] and working.deltas == [27]


async def test_one_failing_account_does_not_stop_the_others(stack, world, clock):
    await start_watching(stack, clock, both=True)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-beta", "EUW1_B", 1)
    world.set_rank("puuid-beta", "GOLD", "II", 55)
    world.forced["/lol/match/v5/matches/by-puuid/puuid-alpha"] = 500
    posts = Posts()
    summary = await stack.riot.poll_new_games(posts)
    assert summary.failed == 1 and summary.posted == 1 and posts.ids == ["EUW1_B"]


async def test_a_rejected_key_stops_the_whole_cycle(stack, world, clock):
    await start_watching(stack, clock, both=True)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.forced["/lol/league"] = 401
    before = world.count("/lol/league")
    summary = await stack.riot.poll_new_games(Posts())
    assert summary.stopped == "RiotAuthError" and summary.failed == 0
    assert world.count("/lol/league") - before == 1  # refused once, and it did not go on asking


async def test_a_rate_limit_stops_the_whole_cycle(stack, world, clock):
    await start_watching(stack, clock, both=True)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.forced["/lol/league"] = 429
    summary = await stack.riot.poll_new_games(Posts())
    assert summary.stopped == "RiotRateLimited"


async def test_a_crash_in_the_announce_callback_is_survived(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 120)

    async def broken(item: FeedItem) -> bool:
        raise RuntimeError("boom")

    summary = await stack.riot.poll_new_games(broken)
    assert summary.failed == 1
    assert await pending(stack, await alpha(stack)) == ["EUW1_1"]  # tried again next cycle


async def test_a_game_riot_no_longer_has_is_set_aside(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 120)
    await stack.riot.sync_history(await alpha(stack))
    del world.matches["EUW1_1"]  # Riot dropped it before the card was built
    posts = Posts()
    summary = await stack.riot.poll_new_games(posts)
    assert posts.items == [] and summary.skipped == 1
    assert await pending(stack, await alpha(stack)) == []


# --- who is watched -----------------------------------------------------------------------------


async def test_mock_players_players_who_left_and_switched_off_accounts_are_not_watched(
    stack, world, clock
):
    await start_watching(stack, clock, both=True)
    assert (await stack.riot.poll_new_games(Posts())).watched == 2
    await stack.db.conn.execute("UPDATE players SET is_mock = 1 WHERE discord_id = 2")
    await stack.db.conn.commit()
    assert (await stack.riot.poll_new_games(Posts())).watched == 1
    await stack.db.conn.execute(
        "UPDATE players SET is_mock = 0, left_at = 'x' WHERE discord_id = 2"
    )
    await stack.db.conn.commit()
    assert (await stack.riot.poll_new_games(Posts())).watched == 1
    await stack.db.conn.execute("UPDATE players SET left_at = NULL WHERE discord_id = 2")
    await stack.db.conn.execute(
        "UPDATE riot_accounts SET feed_enabled = 0 WHERE game_name = 'Beta'"
    )
    await stack.db.conn.commit()
    assert (await stack.riot.poll_new_games(Posts())).watched == 1


async def test_every_linked_account_is_watched_not_only_the_main(stack, world, clock):
    world.add_account("Alt", "EUW", "puuid-alt")
    world.set_rank("puuid-alt", "SILVER", "I", 20)
    await stack.riot.link(1, "Alpha#EUW", None, 1)
    await stack.riot.link(1, "Alt#EUW", None, 1)  # the second account is not the main
    await stack.riot.wait_for_background()
    assert (await stack.riot.poll_new_games(Posts())).started == 2
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alt", "EUW1_ALT", 1)
    world.set_rank("puuid-alt", "SILVER", "I", 35)
    posts = Posts()
    await stack.riot.poll_new_games(posts)
    assert posts.ids == ["EUW1_ALT"] and posts.deltas == [15]
    assert posts.items[0].source.riot_id == "Alt#EUW"
    # A second account does not touch the leaderboard's rank (it only lists mains).
    rows = await stack.rows("SELECT game_name, rank_lp FROM riot_accounts WHERE game_name = 'Alt'")
    assert rows == [("Alt", None)]


async def test_unlinking_an_account_stops_it_and_deletes_what_the_feed_stored(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 120)
    await stack.riot.poll_new_games(Posts())
    await stack.riot.unlink(1, "Alpha#EUW")
    assert (await stack.riot.poll_new_games(Posts())).watched == 0
    assert await stack.rows("SELECT * FROM riot_role_games") == []


async def test_without_a_key_the_cycle_does_nothing(db):
    s = Stack(db, FakeRiot(), enabled=False)
    summary = await s.riot.poll_new_games(Posts())
    assert summary.watched == 0 and summary.posted == 0


async def test_two_cycles_never_run_at_the_same_time(stack, world, clock):
    await start_watching(stack, clock)
    async with stack.riot._polling:
        summary = await stack.riot.poll_new_games(Posts())
    assert summary.watched == 0  # the running cycle was left alone


# --- LP in /riot history ------------------------------------------------------------------------


def test_the_history_line_shows_the_lp_change_only_when_it_is_known():
    now = NOW + timedelta(days=3)
    won = format_line(to_entry(stored(lp_delta=27)), now)
    first, second = won.split("\n")
    assert "LP" not in first  # the first row never grows with the LP
    assert second.startswith("-# 31 min · +27 LP · há ")
    lost = format_line(to_entry(stored(win=False, lp_delta=-19)), now)
    assert lost.split("\n")[1].startswith("-# 31 min · -19 LP · há ")
    for unknown in (None, 0):
        assert "LP" not in format_line(to_entry(stored(lp_delta=unknown)), now)
    remake = format_line(to_entry(stored(early_surrender=True, lp_delta=12)), now)
    assert "LP" not in remake


async def test_history_shows_the_lp_of_games_the_feed_saw_live(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 127)
    await stack.riot.poll_new_games(Posts())
    from bot.features.riot.cog import build_history_embed

    embed = build_history_embed((await stack.riot.recent_history(1)), clock.now)
    assert "+27 LP" in (embed.description or "")


# --- migration ----------------------------------------------------------------------


async def test_migration_14_keeps_existing_accounts_and_games(tmp_path):
    import aiosqlite

    from bot.db import MIGRATIONS, Database

    path = tmp_path / "kdbot.sqlite3"
    async with aiosqlite.connect(path) as raw:
        for i, script in enumerate(MIGRATIONS[:13], start=1):
            await raw.executescript(f"BEGIN;{script};PRAGMA user_version = {i};COMMIT;")
        await raw.execute("PRAGMA foreign_keys = ON")
        await raw.execute(
            "INSERT INTO players (id, discord_id, nickname, created_at) VALUES (1, 11, 'A', 'x')"
        )
        await raw.execute(
            "INSERT INTO riot_accounts (id, player_id, puuid, game_name, tag_line, platform,"
            " is_main, linked_by, linked_at) VALUES (1, 1, 'p', 'Alpha', 'EUW', 'euw1', 1, 1, 'x')"
        )
        await raw.execute(
            "INSERT INTO riot_role_games (riot_account_id, riot_match_id, played_at, win, kills,"
            " deaths, assists, duration_s) VALUES (1, 'EUW1_1', 'x', 1, 1, 1, 1, 1800)"
        )
        await raw.commit()
    database = Database(path)
    await database.connect()
    try:
        async with database.conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone()) == (len(MIGRATIONS),)
        async with database.conn.execute(
            "SELECT feed_since, feed_enabled, feed_tier, feed_lp FROM riot_accounts"
        ) as cur:
            assert list(await cur.fetchall()) == [(None, 1, None, None)]
        async with database.conn.execute(
            "SELECT riot_match_id, announced_at, lp_delta, rank_tier FROM riot_role_games"
        ) as cur:
            assert list(await cur.fetchall()) == [("EUW1_1", None, None, None)]
    finally:
        await database.close()


# --- the cog ------------------------------------------------------------------------------------


class Guild:
    def __init__(self, members: dict[int, Any]) -> None:
        self.members = members

    def get_member(self, discord_id: int) -> Any:
        return self.members.get(discord_id)

    async def fetch_member(self, discord_id: int) -> Any:
        if discord_id in self.members:
            return self.members[discord_id]
        raise discord.HTTPException(SimpleNamespace(status=404, reason="no"), "Unknown Member")  # type: ignore[arg-type]


def feed_cog(stack: Stack, channel: Channel | None, guild: Guild | None = None) -> Riot:
    cog = make_card_cog(stack, channel)
    cog.bot.get_guild = lambda _id: guild  # type: ignore[attr-defined]
    return cog


async def test_posting_a_card_sends_it_with_the_lp_title_and_the_avatar(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 127)
    posts = Posts()
    await stack.riot.poll_new_games(posts)
    channel = Channel()
    cog = feed_cog(stack, channel, Guild({1: member(1)}))
    assert await cog.post_card(posts.items[0]) is True
    sent = channel.sent[0]
    assert sent["embed"].title == "Ganhou (Solo/Duo)"
    assert "MASTER · 127 LP (📈 **+27**)" in sent["embed"].description  # the LP, next to the rank
    assert sent["embed"].author.name == "Ana (Alpha#EUW)"
    assert sent["embed"].author.icon_url == "https://cdn.invalid/a.png"
    assert sent["allowed_mentions"].users is False  # nobody is pinged


async def test_a_card_is_still_posted_when_the_avatar_cannot_be_found(stack, world, clock):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 120)
    posts = Posts()
    await stack.riot.poll_new_games(posts)
    channel = Channel()
    cog = feed_cog(stack, channel, Guild({}))
    assert await cog.post_card(posts.items[0]) is True
    assert channel.sent[0]["embed"].author.icon_url is None


async def test_without_a_channel_or_when_discord_refuses_the_card_is_not_posted(
    stack, world, clock
):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 120)
    posts = Posts()
    await stack.riot.poll_new_games(posts)

    assert await feed_cog(stack, None, Guild({1: member(1)})).post_card(posts.items[0]) is False

    class Refusing(Channel):
        async def send(self, **kwargs):  # type: ignore[override]
            raise discord.HTTPException(SimpleNamespace(status=403, reason="no"), "Missing Access")  # type: ignore[arg-type]

    assert (
        await feed_cog(stack, Refusing(), Guild({1: member(1)})).post_card(posts.items[0]) is False
    )


async def test_the_loop_runs_a_cycle_and_a_discord_failure_leaves_the_game_for_next_time(
    stack, world, clock
):
    await start_watching(stack, clock)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 127)
    cog = feed_cog(stack, None, Guild({1: member(1)}))  # no channel: nothing can be posted
    await cog.feed_poller.coro(cog)
    assert await pending(stack, await alpha(stack)) == ["EUW1_1"]
    channel = Channel()
    cog = feed_cog(stack, channel, Guild({1: member(1)}))
    await cog.feed_poller.coro(cog)
    assert [m["embed"].title for m in channel.sent] == ["Ganhou (Solo/Duo)"]
    assert "(📈 **+27**)" in channel.sent[0]["embed"].description
    assert await pending(stack, await alpha(stack)) == []


async def test_the_loop_survives_a_crash(stack, monkeypatch):
    cog = feed_cog(stack, Channel(), Guild({}))

    async def boom(announce):
        raise RuntimeError("boom")

    monkeypatch.setattr(stack.riot, "poll_new_games", boom)
    await cog.feed_poller.coro(cog)  # logs, never raises


async def test_the_feed_loop_only_starts_with_a_channel_and_a_key(stack):
    async def ready() -> None:
        await asyncio.Event().wait()

    for channel_id, enabled, running in ((555, True, True), (None, True, False)):
        cog = feed_cog(stack, Channel())
        cog.bot.settings = SimpleNamespace(  # type: ignore[attr-defined]
            riot_feed_channel_id=channel_id, guild_id=1
        )
        cog.bot.add_view = lambda view: None  # type: ignore[attr-defined]
        cog.bot.wait_until_ready = ready  # type: ignore[attr-defined]
        assert enabled and stack.riot.enabled
        await cog.cog_load()
        assert cog.feed_poller.is_running() is running
        cog.refresher.cancel()
        cog.daily_leaderboard.cancel()
        cog.feed_poller.cancel()
    disabled = Stack(stack.db, FakeRiot(), enabled=False)
    cog = feed_cog(disabled, Channel())
    cog.bot.add_view = lambda view: None  # type: ignore[attr-defined]
    cog.bot.wait_until_ready = ready  # type: ignore[attr-defined]
    await cog.cog_load()
    assert cog.feed_poller.is_running() is False  # no key, no feed
    cog.refresher.cancel()


async def test_the_card_command_still_shows_no_lp(stack, world, clock):
    from test_riot_card import linked

    await linked(stack, world)
    channel = Channel()
    cog = feed_cog(stack, channel, Guild({1: member(1)}))
    ix = FakeInteraction(99)
    await cog.card.callback(cog, ix, member(1), None, False)  # type: ignore[arg-type]
    assert ix.sent[0]["embed"].title == "Ganhou (Solo/Duo)"  # a preview never knows the LP
