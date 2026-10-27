"""`Database.transaction()`: atomic multi-statement writes on the one shared connection."""

import asyncio

import pytest
import pytest_asyncio

from bot.db import Database


@pytest_asyncio.fixture
async def tdb(db: Database) -> Database:
    await db.conn.execute("CREATE TABLE t (x TEXT PRIMARY KEY)")
    await db.conn.commit()
    return db


async def rows(db: Database) -> list[str]:
    async with db.conn.execute("SELECT x FROM t ORDER BY x") as cur:
        return [r[0] for r in await cur.fetchall()]


async def put(db: Database, value: str) -> None:
    """The way the rest of the bot writes: one statement, then commit."""
    await db.conn.execute("INSERT INTO t (x) VALUES (?)", (value,))
    await db.conn.commit()


async def test_everything_in_a_transaction_is_committed_together(tdb):
    async with tdb.transaction():
        await put(tdb, "a")  # the repositories' own commit() calls must not end the transaction
        await put(tdb, "b")
    assert await rows(tdb) == ["a", "b"]


async def test_an_error_rolls_back_every_write_even_after_inner_commits(tdb):
    await put(tdb, "before")
    with pytest.raises(RuntimeError):
        async with tdb.transaction():
            await put(tdb, "a")
            await put(tdb, "b")
            raise RuntimeError("boom")
    assert await rows(tdb) == ["before"]


async def test_a_failed_statement_inside_a_transaction_aborts_all_of_it(tdb):
    await put(tdb, "dup")
    with pytest.raises(Exception, match="UNIQUE"):
        async with tdb.transaction():
            await put(tdb, "new")
            await put(tdb, "dup")
    assert await rows(tdb) == ["dup"]


async def test_other_writers_wait_and_are_not_swept_into_a_rollback(tdb):
    started = asyncio.Event()

    async def failing() -> None:
        async with tdb.transaction():
            await put(tdb, "in-tx")
            started.set()
            await asyncio.sleep(0.05)
            raise RuntimeError("boom")

    async def bystander() -> None:
        await started.wait()
        await put(tdb, "bystander")  # waits for the transaction, then writes normally

    results = await asyncio.gather(failing(), bystander(), return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert results[1] is None
    assert await rows(tdb) == ["bystander"]


async def test_other_tasks_cannot_see_uncommitted_data(tdb):
    started = asyncio.Event()
    seen: list[list[str]] = []

    async def writer() -> None:
        async with tdb.transaction():
            await put(tdb, "a")
            started.set()
            await asyncio.sleep(0.05)
            await put(tdb, "b")

    async def reader() -> None:
        await started.wait()
        seen.append(await rows(tdb))

    await asyncio.gather(writer(), reader())
    assert seen == [["a", "b"]]  # the read waited for the commit: never a half-written state


async def test_a_write_that_is_mid_commit_finishes_before_the_transaction_starts(tdb):
    inserted = asyncio.Event()

    async def slow_writer() -> None:
        await tdb.conn.execute("INSERT INTO t (x) VALUES ('slow')")
        inserted.set()
        await asyncio.sleep(0.02)  # holds an implicit transaction open for a moment
        await tdb.conn.commit()

    async def transactional() -> None:
        await inserted.wait()
        with pytest.raises(RuntimeError):
            async with tdb.transaction():
                await put(tdb, "tx")
                raise RuntimeError("boom")

    await asyncio.gather(slow_writer(), transactional())
    assert await rows(tdb) == ["slow"]  # committed by its owner, not rolled back with the tx


async def test_transactions_are_serialised(tdb):
    order: list[str] = []

    async def tx(name: str) -> None:
        async with tdb.transaction():
            order.append(f"{name}:start")
            await put(tdb, name)
            await asyncio.sleep(0.01)
            order.append(f"{name}:end")

    await asyncio.gather(tx("a"), tx("b"), tx("c"))
    assert await rows(tdb) == ["a", "b", "c"]
    for i in range(0, 6, 2):  # never interleaved
        assert order[i].split(":")[0] == order[i + 1].split(":")[0]


async def test_a_nested_transaction_joins_the_outer_one(tdb):
    with pytest.raises(RuntimeError):
        async with tdb.transaction():
            await put(tdb, "outer")
            async with tdb.transaction():
                await put(tdb, "inner")
            raise RuntimeError("boom")
    assert await rows(tdb) == []


async def test_a_task_left_over_from_a_transaction_is_not_treated_as_inside_it(tdb):
    escaped: list[asyncio.Task] = []

    async def late() -> None:
        await asyncio.sleep(0.05)
        await put(tdb, "late")

    async with tdb.transaction():
        escaped.append(asyncio.create_task(late()))  # inherits the owner context
    async with tdb.transaction():
        await asyncio.sleep(0.1)  # the late write happens while this one is open: it must wait
        await put(tdb, "second")
        assert await rows(tdb) == ["second"]
    await escaped[0]
    assert await rows(tdb) == ["late", "second"]


async def test_many_concurrent_writers_and_transactions_stay_consistent(tdb):
    async def single(i: int) -> None:
        await put(tdb, f"s{i:02d}")

    async def pair(i: int) -> None:
        async with tdb.transaction():
            await put(tdb, f"p{i:02d}a")
            await asyncio.sleep(0)
            await put(tdb, f"p{i:02d}b")

    async def failing(i: int) -> None:
        with pytest.raises(RuntimeError):
            async with tdb.transaction():
                await put(tdb, f"f{i:02d}")
                raise RuntimeError("boom")

    await asyncio.gather(
        *(single(i) for i in range(20)),
        *(pair(i) for i in range(10)),
        *(failing(i) for i in range(10)),
    )
    found = await rows(tdb)
    assert len(found) == 20 + 20
    assert not any(x.startswith("f") for x in found)
    for i in range(10):
        assert (f"p{i:02d}a" in found) == (f"p{i:02d}b" in found) is True
