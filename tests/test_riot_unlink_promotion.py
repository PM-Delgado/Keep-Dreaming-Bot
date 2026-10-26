"""Unlinking the main account promotes the earliest-linked remaining account."""

import pytest
import pytest_asyncio
from riot_fakes import FakeRiot, Stack

ADMIN = 99


@pytest.fixture
def world() -> FakeRiot:
    w = FakeRiot()
    for name, rank, division in (
        ("Alpha", "MASTER", "I"),
        ("Beta", "SILVER", "II"),
        ("Delta", "PLATINUM", "IV"),
    ):
        puuid = w.add_account(name, "EUW", f"puuid-{name.lower()}")
        w.set_rank(puuid, rank, division)
    return w


@pytest_asyncio.fixture
async def stack(db, world) -> Stack:
    s = Stack(db, world)
    await s.register(1, "Ana")
    for name in ("Alpha", "Beta", "Delta"):  # linked in this order; Alpha is the main
        await s.riot.link(1, f"{name}#EUW", None, ADMIN)
    await s.riot.wait_for_background()
    return s


async def test_unlinking_the_main_promotes_the_earliest_linked_account(stack):
    player = await stack.players.require(1)
    result = await stack.riot.unlink(1, "Alpha#EUW", ADMIN)
    assert result.new_main is not None
    assert result.new_main.riot_id == "Beta#EUW"  # same order /riot list shows
    assert result.new_main.is_main
    assert result.rank is not None
    assert result.rank.label == "Silver II"
    assert await stack.riot_repo.main_puuid(player.id) == "puuid-beta"
    rank = (await stack.ratings.resolve(player.id)).rank
    assert rank is not None
    assert rank.label() == "Silver II"  # Master left with the removed account
    mains = [a for a, _ in await stack.riot.accounts(1) if a.is_main]
    assert [a.riot_id for a in mains] == ["Beta#EUW"]  # exactly one main


async def test_unlinking_a_secondary_account_keeps_the_main(stack):
    player = await stack.players.require(1)
    result = await stack.riot.unlink(1, "Beta#EUW", ADMIN)
    assert result.new_main is None
    assert result.rank is None
    assert await stack.riot_repo.main_puuid(player.id) == "puuid-alpha"
    rank = (await stack.ratings.resolve(player.id)).rank
    assert rank is not None
    assert rank.tier == "MASTER"


async def test_promotion_survives_a_failed_rank_lookup(stack, world):
    player = await stack.players.require(1)
    world.forced["/lol/league"] = 500
    result = await stack.riot.unlink(1, "Alpha#EUW", ADMIN)
    assert result.new_main is not None
    assert result.new_main.riot_id == "Beta#EUW"
    assert result.rank is not None
    assert result.rank.status == "pending"
    resolution = await stack.ratings.resolve(player.id)
    assert resolution.rank is None  # not Master: pending until the next successful refresh
    assert resolution.riot_pending
    del world.forced["/lol/league"]
    assert (await stack.riot.refresh_rank(player.id, ADMIN)).label == "Silver II"


async def test_promotion_chain_ends_with_no_main_when_nothing_is_left(stack):
    player = await stack.players.require(1)
    await stack.riot.unlink(1, "Alpha#EUW", ADMIN)  # Beta becomes main
    await stack.riot.unlink(1, "Beta#EUW", ADMIN)  # Delta becomes main
    assert await stack.riot_repo.main_puuid(player.id) == "puuid-delta"
    last = await stack.riot.unlink(1, "Delta#EUW", ADMIN)
    assert last.new_main is None
    assert await stack.riot_repo.main_puuid(player.id) is None
    assert (await stack.ratings.resolve(player.id)).rank is None


async def test_promotion_works_with_the_integration_off(db, world):
    on = Stack(db, world)
    await on.register(1, "Ana")
    await on.riot.link(1, "Alpha#EUW", None, ADMIN)
    await on.riot.link(1, "Beta#EUW", None, ADMIN)
    await on.riot.wait_for_background()
    off = Stack(db, world, enabled=False)
    result = await off.riot.unlink(1, "Alpha#EUW", ADMIN)
    assert result.new_main is not None
    assert result.new_main.riot_id == "Beta#EUW"
    assert result.rank is not None
    assert result.rank.status == "pending"  # no key, so no lookup, but the main is set
