"""Data Dragon client: the list of champions for the current patch.

Data Dragon is Riot's static-data CDN. It needs no API key. The HTTP call is an injectable
function (like the Riot client's transport), so tests never touch the network. Every failure is a
`DataDragonError`; a payload that is not the shape we expect counts as a failure.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

BASE_URL = "https://ddragon.leagueoflegends.com"
VERSIONS_URL = f"{BASE_URL}/api/versions.json"
LOCALE = "en_US"
TIMEOUT_S = 15.0

GetJson = Callable[[str], Awaitable[Any]]
GetBytes = Callable[[str], Awaitable[bytes]]


def icon_url(patch: str, champion_id: str) -> str:
    """The champion's square icon (120x120 PNG) in that patch."""
    return f"{BASE_URL}/cdn/{patch}/img/champion/{champion_id}.png"


class DataDragonError(Exception):
    """The champion list could not be fetched or made no sense."""


@dataclass(frozen=True)
class Champion:
    id: str  # Data Dragon id, e.g. "MonkeyKing"; the same id the Riot match API reports
    name: str  # display name, e.g. "Wukong"


@dataclass(frozen=True)
class ChampionList:
    patch: str
    champions: tuple[Champion, ...]


async def aiohttp_get_json(url: str) -> Any:
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as resp:
            if resp.status != 200:
                raise DataDragonError(f"Data Dragon answered {resp.status}")
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise DataDragonError("network error talking to Data Dragon") from exc


async def aiohttp_get_bytes(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as resp:
            if resp.status != 200:
                raise DataDragonError(f"Data Dragon answered {resp.status}")
            return await resp.read()
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise DataDragonError("network error talking to Data Dragon") from exc


async def fetch_champions(get_json: GetJson = aiohttp_get_json) -> ChampionList:
    """The champions of the latest patch."""
    versions = await get_json(VERSIONS_URL)
    if not isinstance(versions, list) or not versions or not isinstance(versions[0], str):
        raise DataDragonError("unexpected versions payload")
    patch = versions[0]
    payload = await get_json(f"{BASE_URL}/cdn/{patch}/data/{LOCALE}/champion.json")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise DataDragonError("unexpected champion payload")
    champions = []
    for entry in data.values():
        champion_id = entry.get("id") if isinstance(entry, dict) else None
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(champion_id, str) or not isinstance(name, str):
            raise DataDragonError("unexpected champion entry")
        champions.append(Champion(champion_id, name))
    if not champions:
        raise DataDragonError("empty champion list")
    return ChampionList(patch, tuple(sorted(champions, key=lambda c: c.name.lower())))
