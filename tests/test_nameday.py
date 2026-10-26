import json
import random
from datetime import datetime, timedelta

from bot.features.nameday.repository import SEED_DIR, NameDayRepository
from bot.features.nameday.service import LISBON, NameDayService


async def seeded(repo: NameDayRepository, names=("Ana", "Bia"), phrases=("Hoje: {name}",)):
    for n in names:
        await repo.db.conn.execute("INSERT INTO names (name) VALUES (?)", (n,))
    for p in phrases:
        await repo.db.conn.execute("INSERT INTO phrases (text) VALUES (?)", (p,))
    await repo.db.conn.commit()


def make_service(repo, now):
    return NameDayService(repo, clock=lambda: now[0], rng=random.Random(1))


async def test_pick_is_stable_within_a_day_and_formats_phrase(repo):
    await seeded(repo)
    now = [datetime(2026, 1, 10, 9, 0, tzinfo=LISBON)]
    service = make_service(repo, now)
    first = await service.get_or_pick_today()
    assert first is not None
    assert first.phrase == f"Hoje: {first.name}"
    now[0] += timedelta(hours=10)
    assert await service.get_or_pick_today() == first
    assert await service.leaderboard() == [(first.name, 1)]


async def test_new_lisbon_day_creates_new_pick(repo):
    await seeded(repo)
    now = [datetime(2026, 1, 10, 23, 59, tzinfo=LISBON)]
    service = make_service(repo, now)
    day1 = await service.get_or_pick_today()
    now[0] += timedelta(minutes=2)  # crosses Lisbon midnight
    day2 = await service.get_or_pick_today()
    assert day1 and day2 and day1.day != day2.day
    assert sum(c for _, c in await service.leaderboard(10)) == 2


async def test_no_names_returns_none(repo):
    service = make_service(repo, [datetime(2026, 1, 10, tzinfo=LISBON)])
    assert await service.get_or_pick_today() is None
    assert await service.leaderboard() == []


async def test_leaderboard_orders_by_count_then_name(repo):
    await seeded(repo, names=("Ana", "Bia", "Cai", "Dee"))
    ids = {name: id_ for id_, name in await repo.list_names()}
    for day, name in enumerate(["Bia", "Ana", "Bia", "Cai", "Dee", "Cai", "Bia"], start=1):
        await repo.add_pick(
            datetime(2026, 1, day, tzinfo=LISBON).date(),
            ids[name],
            "p",
            datetime(2026, 1, day, tzinfo=LISBON),
        )
    assert await repo.leaderboard(3) == [("Bia", 3), ("Cai", 2), ("Ana", 1)]


async def test_second_pick_for_same_day_is_ignored(repo):
    await seeded(repo)
    ids = {name: id_ for id_, name in await repo.list_names()}
    day = datetime(2026, 1, 10, tzinfo=LISBON)
    await repo.add_pick(day.date(), ids["Ana"], "one", day)
    await repo.add_pick(day.date(), ids["Bia"], "two", day)
    pick = await repo.get_pick(day.date())
    assert pick is not None and (pick.name, pick.phrase) == ("Ana", "one")


async def test_seed_loads_repo_data_once(repo):
    await repo.seed_if_empty()
    await repo.seed_if_empty()
    expected = json.loads((SEED_DIR / "names.json").read_text(encoding="utf-8"))
    assert [n for _, n in await repo.list_names()] == expected
    assert len(await repo.list_phrases()) == len(
        json.loads((SEED_DIR / "phrases.json").read_text(encoding="utf-8"))
    )
