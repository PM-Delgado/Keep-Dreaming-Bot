from collections.abc import AsyncIterator

import pytest_asyncio

from bot.db import Database
from bot.features.nameday.repository import NameDayRepository


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def repo(db: Database) -> NameDayRepository:
    return NameDayRepository(db)
