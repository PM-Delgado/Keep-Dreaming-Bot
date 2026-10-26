"""Minimal Riot API client: injectable transport, rate limiting, explicit error types.

Every failure is an exception, never a value, so a caller can't mistake "the request failed" for
"the player is unranked". The API key travels only in a request header and never appears in
exceptions or log messages.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from bot.features.ratings.rank import APEX_POINTS, rank_points
from bot.features.riot.platforms import account_region, match_region, normalise_platform
from bot.features.riot.roles import RANKED_SOLO_QUEUE_ID

SOLO_QUEUE = "RANKED_SOLO_5x5"


class RiotError(Exception):
    """Base class; subclasses say what went wrong."""


class RiotNotFound(RiotError):
    pass


class RiotAuthError(RiotError):
    """401/403: the key is missing, expired or not allowed."""


class RiotRateLimited(RiotError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after:g}s")
        self.retry_after = retry_after


class RiotUnavailable(RiotError):
    """Network trouble, 5xx, or a payload we don't understand."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # lower-case keys
    body: Any


Transport = Callable[[str, Mapping[str, str]], Awaitable[HttpResponse]]


class AiohttpTransport:
    def __init__(self, timeout_s: float = 15.0) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None

    async def __call__(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        try:
            async with self._session.get(url, headers=headers) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                return HttpResponse(
                    resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
                )
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise RiotUnavailable("network error talking to Riot") from exc

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


class RateLimiter:
    """Sliding-window limiter kept under the personal-key limits (20/s, 100 per 2 minutes).

    `reserve` requests of every window are held back for interactive commands: background work
    (ranked-history syncs) may only use `per_window - reserve`, so a long sync can never leave a
    `/riot link` waiting for the whole window. The lock is never held while sleeping, so a
    waiting background request does not block an interactive one either.
    """

    def __init__(
        self,
        per_second: int = 18,
        per_window: int = 90,
        window_s: float = 120.0,
        reserve: int = 30,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not 0 <= reserve < per_window:
            raise ValueError("reserve must be between 0 and per_window - 1")
        self.per_second, self.per_window, self.window_s = per_second, per_window, window_s
        self.reserve = reserve
        self._clock, self._sleep = clock, sleep
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, background: bool = False) -> None:
        cap = self.per_window - (self.reserve if background else 0)
        while True:
            async with self._lock:
                now = self._clock()
                while self._stamps and self._stamps[0] <= now - self.window_s:
                    self._stamps.popleft()
                wait = 0.0
                if len(self._stamps) >= cap:
                    # Wait until enough of the oldest requests have left the window.
                    wait = self._stamps[len(self._stamps) - cap] + self.window_s - now
                last_second = [t for t in self._stamps if t > now - 1.0]
                if len(last_second) >= self.per_second:
                    wait = max(wait, last_second[0] + 1.0 - now)
                if wait <= 0:
                    self._stamps.append(now)
                    return
            await self._sleep(wait)


@dataclass(frozen=True)
class RiotAccount:
    puuid: str
    game_name: str
    tag_line: str


@dataclass(frozen=True)
class SoloRank:
    tier: str
    division: str | None  # None for Master+
    lp: int


@dataclass(frozen=True)
class RankedGame:
    match_id: str
    played_at: datetime
    team_position: str | None
    individual_position: str | None
    win: bool
    kills: int
    deaths: int
    assists: int
    duration_s: int
    early_surrender: bool
    champion: str | None


class RiotClient:
    def __init__(
        self,
        api_key: str,
        transport: Transport | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 2,
        max_retry_wait_s: float = 30.0,
    ) -> None:
        self._headers = {"X-Riot-Token": api_key}
        self._transport = transport or AiohttpTransport()
        self._limiter = limiter or RateLimiter()
        self._sleep = sleep
        self._max_retries = max_retries
        self._max_retry_wait_s = max_retry_wait_s

    async def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if close is not None:
            await close()

    async def _get(
        self,
        host: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        background: bool = False,
    ) -> Any:
        url = f"https://{host}.api.riotgames.com{path}"
        if params:
            url += "?" + urlencode(params)
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire(background=background)
            resp = await self._transport(url, self._headers)
            last_attempt = attempt >= self._max_retries
            if resp.status == 200:
                return resp.body
            if resp.status == 404:
                raise RiotNotFound(path)
            if resp.status in (401, 403):
                raise RiotAuthError("Riot rejected the API key (expired or not allowed)")
            if resp.status == 429:
                retry_after = _retry_after(resp.headers)
                if last_attempt or retry_after > self._max_retry_wait_s:
                    raise RiotRateLimited(retry_after)
                await self._sleep(retry_after)
                continue
            if resp.status >= 500 and not last_attempt:
                await self._sleep(1.0 * (attempt + 1))
                continue
            raise RiotUnavailable(f"unexpected status {resp.status}")
        raise RiotUnavailable("retries exhausted")  # pragma: no cover

    # --- account-v1 ---------------------------------------------------------------------

    async def account_by_riot_id(self, game_name: str, tag_line: str, platform: str) -> RiotAccount:
        path = (
            f"/riot/account/v1/accounts/by-riot-id/{quote(game_name, safe='')}/"
            f"{quote(tag_line, safe='')}"
        )
        return _parse_account(await self._get(account_region(platform), path))

    async def account_by_puuid(self, puuid: str, platform: str) -> RiotAccount:
        path = f"/riot/account/v1/accounts/by-puuid/{quote(puuid, safe='')}"
        return _parse_account(await self._get(account_region(platform), path))

    # --- summoner-v4 --------------------------------------------------------------------

    async def has_summoner(self, puuid: str, platform: str) -> bool:
        """Whether the account has a League profile on this platform (guards against a wrong
        platform, where league entries would come back empty and look like "unranked")."""
        platform = normalise_platform(platform)
        path = f"/lol/summoner/v4/summoners/by-puuid/{quote(puuid, safe='')}"
        try:
            await self._get(platform, path)
        except RiotNotFound:
            return False
        return True

    # --- league-v4 ----------------------------------------------------------------------

    async def solo_rank(self, puuid: str, platform: str) -> SoloRank | None:
        """The Solo/Duo rank, or None when Riot *confirms* there is none (unranked)."""
        platform = normalise_platform(platform)
        path = f"/lol/league/v4/entries/by-puuid/{quote(puuid, safe='')}"
        return _parse_solo_rank(await self._get(platform, path))

    # --- match-v5 -----------------------------------------------------------------------

    async def ranked_match_ids(
        self, puuid: str, platform: str, start_time: int | None = None, count: int = 100
    ) -> list[str]:
        params: dict[str, Any] = {"queue": RANKED_SOLO_QUEUE_ID, "count": count}
        if start_time is not None:
            params["startTime"] = start_time
        path = f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids"
        # History syncs are background work: they leave part of the request budget free for
        # interactive commands (see RateLimiter).
        data = await self._get(match_region(platform), path, params, background=True)
        if not isinstance(data, list) or not all(isinstance(i, str) for i in data):
            raise RiotUnavailable("unexpected match id list")
        return data

    async def ranked_game(self, match_id: str, puuid: str, platform: str) -> RankedGame | None:
        """Player's row of a ranked Solo/Duo match; None if it is not one or they are not in it."""
        path = f"/lol/match/v5/matches/{quote(match_id, safe='')}"
        data = await self._get(match_region(platform), path, background=True)
        return parse_ranked_game(data, puuid)


def _retry_after(headers: Mapping[str, str]) -> float:
    try:
        return max(float(headers.get("retry-after", "1")), 0.0)
    except ValueError:
        return 1.0


def _parse_account(data: Any) -> RiotAccount:
    try:
        return RiotAccount(str(data["puuid"]), str(data["gameName"]), str(data["tagLine"]))
    except (KeyError, TypeError) as exc:
        raise RiotUnavailable("unexpected account payload") from exc


def _parse_solo_rank(data: Any) -> SoloRank | None:
    if not isinstance(data, list):
        raise RiotUnavailable("unexpected league entries payload")
    for entry in data:
        if not isinstance(entry, dict) or entry.get("queueType") != SOLO_QUEUE:
            continue
        try:
            tier = str(entry["tier"]).upper()
            division = None if tier in APEX_POINTS else str(entry["rank"]).upper()
            rank_points(tier, division)  # rejects tiers/divisions we can't map
            return SoloRank(tier, division, int(entry.get("leaguePoints", 0)))
        except (KeyError, ValueError, TypeError) as exc:
            raise RiotUnavailable("unexpected rank in league entry") from exc
    return None  # a successful answer with no Solo/Duo entry: confirmed unranked


def parse_ranked_game(data: Any, puuid: str) -> RankedGame | None:
    try:
        info = data["info"]
        if info.get("queueId") != RANKED_SOLO_QUEUE_ID:
            return None
        me = next((p for p in info["participants"] if p.get("puuid") == puuid), None)
        if me is None:
            return None
        stamp_ms = info.get("gameEndTimestamp") or info["gameCreation"]
        duration = int(me.get("timePlayed", info.get("gameDuration", 0)))
        return RankedGame(
            match_id=str(data["metadata"]["matchId"]),
            played_at=datetime.fromtimestamp(int(stamp_ms) / 1000, tz=UTC),
            team_position=me.get("teamPosition") or None,
            individual_position=me.get("individualPosition") or None,
            win=bool(me["win"]),
            kills=int(me.get("kills", 0)),
            deaths=int(me.get("deaths", 0)),
            assists=int(me.get("assists", 0)),
            duration_s=duration,
            early_surrender=bool(me.get("gameEndedInEarlySurrender")),
            champion=me.get("championName"),
        )
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise RiotUnavailable("unexpected match payload") from exc
