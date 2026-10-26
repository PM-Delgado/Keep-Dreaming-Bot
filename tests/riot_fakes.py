"""A fake Riot HTTP transport for tests: routes by URL path, no network, no key."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlparse

from bot.db import Database
from bot.features.players.repository import PlayerRepository
from bot.features.players.service import PlayerService
from bot.features.ratings.repository import RatingRepository
from bot.features.ratings.service import RatingService
from bot.features.riot.client import HttpResponse, RateLimiter, RiotClient
from bot.features.riot.repository import RiotRepository
from bot.features.riot.service import RiotService

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class Sleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


@dataclass
class FakeRiot:
    """Mutable world state; `calls` records every request path for assertions."""

    accounts: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    summoners: set[tuple[str, str]] = field(default_factory=set)  # (puuid, platform)
    ranks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    match_ids: dict[str, list[str]] = field(default_factory=dict)
    matches: dict[str, dict[str, Any]] = field(default_factory=dict)
    # path prefix -> forced status (e.g. "/lol/league" -> 500)
    forced: dict[str, int] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def add_account(self, name: str, tag: str, puuid: str, platform: str = "euw1") -> str:
        self.accounts[(name.lower(), tag.lower())] = {
            "puuid": puuid,
            "gameName": name,
            "tagLine": tag,
        }
        self.summoners.add((puuid, platform))
        return puuid

    def set_rank(self, puuid: str, tier: str | None, rank: str = "I", lp: int = 0) -> None:
        self.ranks[puuid] = (
            []
            if tier is None
            else [{"queueType": "RANKED_SOLO_5x5", "tier": tier, "rank": rank, "leaguePoints": lp}]
        )

    def add_game(
        self,
        puuid: str,
        match_id: str,
        position: str | None,
        when: datetime,
        queue: int = 420,
        duration: int = 1800,
        early: bool = False,
        individual: str | None = None,
    ) -> None:
        stamp = int(when.timestamp() * 1000)
        self.match_ids.setdefault(puuid, []).insert(0, match_id)
        self.matches[match_id] = {
            "metadata": {"matchId": match_id},
            "info": {
                "queueId": queue,
                "gameCreation": stamp,
                "gameEndTimestamp": stamp + duration * 1000,
                "participants": [
                    {
                        "puuid": puuid,
                        "teamPosition": position or "",
                        "individualPosition": individual if individual is not None else position,
                        "win": True,
                        "kills": 5,
                        "deaths": 3,
                        "assists": 7,
                        "timePlayed": duration,
                        "championName": "Ahri",
                        "gameEndedInEarlySurrender": early,
                    }
                ],
            },
        }

    async def __call__(self, url: str, headers: Any) -> HttpResponse:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        self.calls.append(path)
        for prefix, status in self.forced.items():
            if path.startswith(prefix):
                return HttpResponse(status, {}, None)
        host = parsed.hostname or ""
        platform = host.split(".")[0]
        parts = path.strip("/").split("/")
        if path.startswith("/riot/account/v1/accounts/by-riot-id/"):
            found = self.accounts.get((parts[-2].lower(), parts[-1].lower()))
            return HttpResponse(200, {}, found) if found else HttpResponse(404, {}, None)
        if path.startswith("/riot/account/v1/accounts/by-puuid/"):
            found = next((a for a in self.accounts.values() if a["puuid"] == parts[-1]), None)
            return HttpResponse(200, {}, found) if found else HttpResponse(404, {}, None)
        if path.startswith("/lol/summoner/v4/summoners/by-puuid/"):
            ok = (parts[-1], platform) in self.summoners
            return HttpResponse(200 if ok else 404, {}, {"puuid": parts[-1]} if ok else None)
        if path.startswith("/lol/league/v4/entries/by-puuid/"):
            return HttpResponse(200, {}, self.ranks.get(parts[-1], []))
        if path.startswith("/lol/match/v5/matches/by-puuid/") and path.endswith("/ids"):
            return HttpResponse(200, {}, list(self.match_ids.get(parts[-2], [])))
        if path.startswith("/lol/match/v5/matches/"):
            found = self.matches.get(parts[-1])
            return HttpResponse(200, {}, found) if found else HttpResponse(404, {}, None)
        return HttpResponse(404, {}, None)

    def count(self, prefix: str) -> int:
        return sum(1 for c in self.calls if c.startswith(prefix))


def make_client(world: FakeRiot) -> RiotClient:
    return RiotClient(
        "RGAPI-test-key",
        world,
        RateLimiter(per_second=10_000, per_window=1_000_000),
        Sleeper(),
    )


def days_ago(now: datetime, days: float) -> datetime:
    return now - timedelta(days=days)


class Stack:
    def __init__(self, db: Database, world: FakeRiot, enabled: bool = True) -> None:
        self.db = db
        self.world = world
        self.riot_repo = RiotRepository(db)
        self.ratings = RatingService(
            RatingRepository(db), clock=lambda: NOW, main_puuid_of=self.riot_repo.main_puuid
        )
        self.players = PlayerService(PlayerRepository(db), self.ratings, clock=lambda: NOW)
        self.riot = RiotService(
            self.riot_repo,
            self.ratings,
            self.players,
            make_client(world) if enabled else None,
            clock=lambda: NOW,
        )
        self.players.role_estimator = self.riot.role_estimates
        self.players.account_labeler = self.riot.account_labels

    async def register(self, discord_id: int, nickname: str) -> int:
        return (await self.players.register(discord_id, nickname)).id

    async def rows(self, sql: str, *args):
        async with self.db.conn.execute(sql, args) as cur:
            return list(await cur.fetchall())
