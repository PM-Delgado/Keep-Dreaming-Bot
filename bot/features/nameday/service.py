"""Name-of-the-day rules. No Discord imports; clock and RNG are injectable for tests."""

import random
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.features.nameday.repository import NameDayRepository, Pick

LISBON = ZoneInfo("Europe/Lisbon")


class NameDayService:
    def __init__(
        self,
        repo: NameDayRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(LISBON),
        rng: random.Random | None = None,
    ) -> None:
        self.repo = repo
        self.clock = clock
        self.rng = rng or random.Random()

    async def get_or_pick_today(self) -> Pick | None:
        """Today's pick (Lisbon date), choosing one first if the day has none yet.

        Returns None only when there are no names or phrases to choose from.
        """
        now = self.clock().astimezone(LISBON)
        today = now.date()
        existing = await self.repo.get_pick(today)
        if existing:
            return existing

        names = await self.repo.list_names()
        phrases = await self.repo.list_phrases()
        if not names or not phrases:
            return None

        name_id, name = self.rng.choice(names)
        phrase = self.rng.choice(phrases).replace("{name}", name)
        await self.repo.add_pick(today, name_id, phrase, now)
        # Re-read so the stored pick is returned even if another writer got there first.
        return await self.repo.get_pick(today)

    async def leaderboard(self, limit: int = 3) -> list[tuple[str, int]]:
        return await self.repo.leaderboard(limit)
