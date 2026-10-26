import logging

import pytest

from bot.features.riot.client import (
    HttpResponse,
    RateLimiter,
    RiotAuthError,
    RiotClient,
    RiotNotFound,
    RiotRateLimited,
    RiotUnavailable,
    parse_ranked_game,
)
from bot.features.riot.platforms import (
    PLATFORM_REGION,
    PLATFORMS,
    account_region,
    match_region,
    normalise_platform,
)

KEY = "RGAPI-super-secret-key"
PUUID = "puuid-123"


class FakeTransport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        return self.responses.pop(0)


def ok(body):
    return HttpResponse(200, {}, body)


class Sleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def make_client(*responses, **kwargs):
    transport = FakeTransport(*responses)
    sleeper = Sleeper()
    client = RiotClient(
        KEY, transport, RateLimiter(per_second=1000, per_window=10000), sleeper, **kwargs
    )
    return client, transport, sleeper


# --- routing ---------------------------------------------------------------------------------


def test_platform_routing():
    assert match_region("EUW1") == "europe"
    assert match_region("na1") == "americas"
    assert match_region("kr") == "asia"
    assert match_region("oc1") == "sea"
    assert account_region("oc1") == "asia"  # account-v1 has no sea host
    assert account_region("tr1") == "europe"
    assert set(PLATFORMS) == set(PLATFORM_REGION)
    assert len(PLATFORMS) == 16
    with pytest.raises(ValueError):
        normalise_platform("xx9")


# --- requests --------------------------------------------------------------------------------


async def test_account_lookup_url_header_and_payload():
    client, transport, _ = make_client(
        ok({"puuid": PUUID, "gameName": "Some Name", "tagLine": "EUW"})
    )
    account = await client.account_by_riot_id("Some Name", "EUW", "euw1")
    assert (account.puuid, account.game_name, account.tag_line) == (PUUID, "Some Name", "EUW")
    url, headers = transport.calls[0]
    assert (
        url
        == "https://europe.api.riotgames.com/riot/account/v1/accounts/by-riot-id/Some%20Name/EUW"
    )
    assert headers == {"X-Riot-Token": KEY}  # key only in the header, never the URL
    assert KEY not in url


async def test_solo_rank_ranked_unranked_and_apex():
    ranked = ok(
        [
            {"queueType": "RANKED_FLEX_SR", "tier": "SILVER", "rank": "I", "leaguePoints": 5},
            {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 47},
        ]
    )
    client, transport, _ = make_client(
        ranked,
        ok([]),
        ok([{"queueType": "RANKED_SOLO_5x5", "tier": "MASTER", "rank": "I", "leaguePoints": 120}]),
        ok([{"queueType": "RANKED_FLEX_SR", "tier": "GOLD", "rank": "I"}]),
    )
    rank = await client.solo_rank(PUUID, "euw1")
    assert rank is not None
    assert (rank.tier, rank.division, rank.lp) == ("GOLD", "II", 47)
    assert (
        transport.calls[0][0]
        == f"https://euw1.api.riotgames.com/lol/league/v4/entries/by-puuid/{PUUID}"
    )
    assert await client.solo_rank(PUUID, "euw1") is None  # empty answer = confirmed unranked
    apex = await client.solo_rank(PUUID, "euw1")
    assert apex is not None
    assert (apex.tier, apex.division) == ("MASTER", None)
    assert await client.solo_rank(PUUID, "euw1") is None  # only a flex entry: no Solo/Duo rank


async def test_unmappable_rank_is_an_error_not_unranked():
    client, _, _ = make_client(ok([{"queueType": "RANKED_SOLO_5x5", "tier": "WOOD", "rank": "I"}]))
    with pytest.raises(RiotUnavailable):
        await client.solo_rank(PUUID, "euw1")
    client, _, _ = make_client(ok({"unexpected": True}))
    with pytest.raises(RiotUnavailable):
        await client.solo_rank(PUUID, "euw1")


async def test_match_ids_request_filters_ranked_solo():
    client, transport, _ = make_client(ok(["EUW1_1", "EUW1_2"]))
    ids = await client.ranked_match_ids(PUUID, "euw1", start_time=1700000000, count=50)
    assert ids == ["EUW1_1", "EUW1_2"]
    url = transport.calls[0][0]
    assert url.startswith(
        f"https://europe.api.riotgames.com/lol/match/v5/matches/by-puuid/{PUUID}/ids?"
    )
    assert "queue=420" in url
    assert "count=50" in url
    assert "startTime=1700000000" in url


# --- errors: never confused with "unranked" ----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, RiotNotFound), (401, RiotAuthError), (403, RiotAuthError), (400, RiotUnavailable)],
)
async def test_error_statuses_raise(status, error):
    client, _, _ = make_client(HttpResponse(status, {}, None))
    with pytest.raises(error):
        await client.solo_rank(PUUID, "euw1")


async def test_rate_limit_retries_after_the_header_then_succeeds():
    client, transport, sleeper = make_client(HttpResponse(429, {"retry-after": "3"}, None), ok([]))
    assert await client.solo_rank(PUUID, "euw1") is None
    assert sleeper.slept == [3.0]
    assert len(transport.calls) == 2


async def test_rate_limit_gives_up_when_it_persists_or_is_too_long():
    client, _, _ = make_client(*[HttpResponse(429, {"retry-after": "1"}, None)] * 3)
    with pytest.raises(RiotRateLimited) as info:
        await client.solo_rank(PUUID, "euw1")
    assert info.value.retry_after == 1.0
    client, transport, sleeper = make_client(HttpResponse(429, {"retry-after": "120"}, None))
    with pytest.raises(RiotRateLimited):
        await client.solo_rank(PUUID, "euw1")
    assert sleeper.slept == []  # a two-minute wait is surfaced, not slept through
    assert len(transport.calls) == 1


async def test_server_errors_are_retried_then_reported():
    client, _, sleeper = make_client(HttpResponse(503, {}, None), ok([]))
    assert await client.solo_rank(PUUID, "euw1") is None
    assert sleeper.slept == [1.0]
    client, _, _ = make_client(*[HttpResponse(500, {}, None)] * 3)
    with pytest.raises(RiotUnavailable):
        await client.solo_rank(PUUID, "euw1")


async def test_the_api_key_never_reaches_logs_or_error_messages(caplog):
    caplog.set_level(logging.DEBUG)
    client, _, _ = make_client(HttpResponse(403, {}, None))
    with pytest.raises(RiotAuthError) as info:
        await client.solo_rank(PUUID, "euw1")
    assert KEY not in str(info.value)
    assert KEY not in caplog.text


# --- rate limiter ---------------------------------------------------------------------------


async def test_limiter_spaces_requests_per_second_and_per_window():
    now = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(
        per_second=2, per_window=3, window_s=10.0, reserve=0, clock=lambda: now[0], sleep=sleep
    )
    await limiter.acquire()
    await limiter.acquire()
    assert sleeps == []
    await limiter.acquire()  # third in the same second: wait out the 1s window
    assert sleeps == [pytest.approx(1.0)]
    await limiter.acquire()  # fourth: the 3-per-10s window is full, wait for the oldest to expire
    assert now[0] == pytest.approx(10.0)


# --- match parsing ---------------------------------------------------------------------------


def match_payload(queue=420, puuid=PUUID, **overrides):
    participant = {
        "puuid": puuid,
        "teamPosition": "BOTTOM",
        "individualPosition": "BOTTOM",
        "win": True,
        "kills": 7,
        "deaths": 2,
        "assists": 9,
        "timePlayed": 1850,
        "championName": "Jinx",
        "gameEndedInEarlySurrender": False,
        **overrides,
    }
    return {
        "metadata": {"matchId": "EUW1_99"},
        "info": {
            "queueId": queue,
            "gameCreation": 1_700_000_000_000,
            "gameEndTimestamp": 1_700_001_850_000,
            "participants": [{"puuid": "other"}, participant],
        },
    }


def test_parse_ranked_game():
    game = parse_ranked_game(match_payload(), PUUID)
    assert game is not None
    assert (game.match_id, game.team_position, game.win) == ("EUW1_99", "BOTTOM", True)
    assert (game.kills, game.deaths, game.assists, game.duration_s) == (7, 2, 9, 1850)
    assert game.played_at.year == 2023
    assert game.champion == "Jinx"


def test_parse_ignores_other_queues_and_missing_participants():
    assert parse_ranked_game(match_payload(queue=450), PUUID) is None
    assert parse_ranked_game(match_payload(), "someone-else") is None


def test_parse_keeps_unknown_positions_as_they_are():
    game = parse_ranked_game(match_payload(teamPosition="", individualPosition="Invalid"), PUUID)
    assert game is not None
    assert game.team_position is None
    assert game.individual_position == "Invalid"


def test_parse_rejects_malformed_payloads():
    with pytest.raises(RiotUnavailable):
        parse_ranked_game({"info": {"queueId": 420}}, PUUID)
