"""Shared fixtures for lobby tests: a lobby service over an in-memory DB with controllable time."""

import random
from datetime import UTC, datetime, timedelta

from bot.db import Database
from bot.features.lobby.repository import LobbyRepository
from bot.features.lobby.service import LobbyParams, LobbyService, LobbyUpdate
from bot.features.players.repository import PlayerRepository
from bot.features.players.service import PlayerService
from bot.features.ratings.repository import RatingRepository
from bot.features.ratings.service import RatingService

START = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
ADMIN = 99
GUILD = 5000
CHANNEL = 6000


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class LobbyStack:
    """Players 1..n are registered with default ratings; ids 1.. are Discord user ids too."""

    def __init__(self, db: Database, clock: Clock | None = None, seed: int = 1) -> None:
        self.db = db
        self.clock = clock or Clock()
        self.ratings = RatingService(RatingRepository(db), clock=self.clock)
        self.players = PlayerService(PlayerRepository(db), self.ratings, clock=self.clock)
        self.repo = LobbyRepository(db)
        self.lobbies = self.make_service(seed)

    def make_service(self, seed: int = 1, params: LobbyParams | None = None) -> LobbyService:
        return LobbyService(
            self.repo,
            self.players,
            self.ratings,
            is_admin=lambda user_id: user_id == ADMIN,
            params=params,
            clock=self.clock,
            rng=random.Random(seed),
        )

    async def register(self, count: int, start: int = 1) -> list[int]:
        ids = list(range(start, start + count))
        for i in ids:
            if await self.players.repo.get_by_discord(i) is None:  # idempotent
                await self.players.register(i, f"Player {i:02d}")
        return ids

    async def full_lobby(self, size: int = 10, creator: int = 1) -> LobbyUpdate:
        """Open a lobby as `creator` and have players 2..size join (registers them if needed)."""
        await self.register(size)
        update = await self.lobbies.open(GUILD, CHANNEL, creator)
        lobby_id = update.snapshot.lobby.id
        for i in range(creator + 1, size + 1):
            update = await self.lobbies.join(lobby_id, i)
        return update

    async def ready_everyone(self, lobby_id: int, ids: list[int]) -> LobbyUpdate:
        update = None
        for i in ids:
            update = await self.lobbies.ready(lobby_id, i)
        assert update is not None
        return update
