"""The real services wired on one database, the way the bot wires them (no Discord)."""

import random
from collections.abc import Callable
from datetime import UTC, datetime

from bot.db import Database
from bot.features.champions.repository import ChampionRepository
from bot.features.champions.service import ChampionService
from bot.features.lobby.repository import LobbyRepository
from bot.features.lobby.service import LobbyService
from bot.features.matches.repository import MatchRepository
from bot.features.matches.service import MatchService
from bot.features.players.repository import PlayerRepository
from bot.features.players.service import PlayerService
from bot.features.ratings.repository import RatingRepository
from bot.features.ratings.service import RatingService
from bot.features.riot.client import RiotClient
from bot.features.riot.repository import AccountRow, RiotRepository
from bot.features.riot.service import RiotService


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NoHistoryRiotService(RiotService):
    """Links accounts and reads the rank, but leaves the (slow) ranked-history download to the bot,
    which resumes every unfinished sync at startup."""

    def spawn_history_sync(self, account: AccountRow) -> None:
        return None


class Stack:
    def __init__(
        self,
        db: Database,
        admin_ids: frozenset[int],
        *,
        riot_client: RiotClient | None = None,
        default_platform: str = "euw1",
        clock: Callable[[], datetime] = _utcnow,
        seed: int = 7,
        sync_history: bool = True,
    ) -> None:
        self.db = db
        self.admin_ids = admin_ids
        is_admin = admin_ids.__contains__
        self.ratings = RatingService(RatingRepository(db), clock=clock)
        self.players = PlayerService(PlayerRepository(db), self.ratings, clock=clock)
        self.riot_repo = RiotRepository(db)
        riot_class = RiotService if sync_history else NoHistoryRiotService
        self.riot = riot_class(
            self.riot_repo, self.ratings, self.players, riot_client, default_platform, clock=clock
        )
        self.ratings.main_puuid_of = self.riot.main_puuid_of
        self.players.role_estimator = self.riot.role_estimates
        self.players.account_labeler = self.riot.account_labels
        self.players.champion_pooler = self.riot.champion_pool
        self.lobby_repo = LobbyRepository(db)
        self.match_repo = MatchRepository(db)
        self.ratings.matches_source = self.match_repo.confirmed_for_replay
        self.ratings.form_source = self.match_repo.recent_results
        self.champions = ChampionService(ChampionRepository(db))
        self.matches = MatchService(
            self.match_repo, self.lobby_repo, self.players, self.ratings, is_admin, db,
            clock=clock, champions=self.champions,
        )  # fmt: skip
        self.lobbies = LobbyService(
            self.lobby_repo, self.players, self.ratings, self.matches,
            is_admin=is_admin, clock=clock, rng=random.Random(seed),
        )  # fmt: skip
