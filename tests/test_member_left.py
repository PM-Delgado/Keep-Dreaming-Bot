"""Players who leave the Discord server: found by the member check, marked as left, their Riot
accounts unlinked, their lobby places released, and a clean way back with /register."""

from types import SimpleNamespace
from typing import Any

import discord
import pytest
import pytest_asyncio
from lobby_fakes import CHANNEL, GUILD, LobbyStack
from riot_fakes import FakeRiot, Stack
from test_riot_feed import Posts, finish, start_watching
from test_riot_refresh import tick

from bot.features.players.cog import UNKNOWN_MEMBER, Players
from bot.features.players.service import PlayerError


class Server:
    """Who is in the server. A value of None answers "Discord cannot say"."""

    def __init__(self, *ids: int) -> None:
        self.members: dict[int, bool | None] = {i: True for i in ids}
        self.asked: list[int] = []

    def leave(self, discord_id: int) -> None:
        self.members[discord_id] = False

    async def __call__(self, discord_id: int) -> bool | None:
        self.asked.append(discord_id)
        return self.members.get(discord_id)


@pytest_asyncio.fixture
async def lobbies(db) -> LobbyStack:
    return LobbyStack(db)


# --- the check and what it marks -------------------------------------------------------------


async def test_the_sweep_marks_only_players_discord_says_are_gone(lobbies):
    await lobbies.register(5)
    server = Server(1, 2, 3, 4, 5)
    lobbies.players.membership = server
    assert await lobbies.players.sweep_left() == []
    server.leave(2)
    server.members[3] = None  # Discord could not answer: never treated as gone
    gone = await lobbies.players.sweep_left()
    assert [p.discord_id for p in gone] == [2]
    assert (await lobbies.players.repo.get_by_discord(2)).left_at is not None  # type: ignore[union-attr]
    assert (await lobbies.players.repo.get_by_discord(3)).left_at is None  # type: ignore[union-attr]
    with pytest.raises(PlayerError):
        await lobbies.players.require(2)
    # Already marked: not asked about again.
    server.asked.clear()
    assert await lobbies.players.sweep_left() == []
    assert 2 not in server.asked


async def test_the_sweep_does_nothing_without_a_membership_check(lobbies):
    await lobbies.register(2)
    assert await lobbies.players.sweep_left() == []


async def test_a_sweep_that_finds_most_of_the_server_gone_is_not_believed(lobbies):
    await lobbies.register(5)
    server = Server(1, 2, 3, 4, 5)
    lobbies.players.membership = server
    for i in (1, 2, 3):
        server.leave(i)
    assert await lobbies.players.sweep_left() == []
    assert all(p.left_at is None for p in await lobbies.players.repo.list_active())
    # Three of eight is a normal clear-out, not a failure of the check.
    await lobbies.register(3, start=6)
    for i in (6, 7, 8):
        server.members[i] = True
    assert len(await lobbies.players.sweep_left()) == 3


async def test_mock_players_are_never_checked(lobbies):
    await lobbies.register(2)
    await lobbies.players.register(900, "Mock", is_mock=True)
    server = Server(1, 2)
    lobbies.players.membership = server
    await lobbies.players.sweep_left()
    assert 900 not in server.asked


# --- lobbies ---------------------------------------------------------------------------------


async def test_a_player_who_left_is_taken_out_of_the_open_lobby(lobbies):
    await lobbies.register(4)
    update = await lobbies.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    for i in (2, 3, 4):
        await lobbies.lobbies.join(lobby_id, i)
    server = Server(1, 2, 3, 4)
    lobbies.players.membership = server
    server.leave(3)
    await lobbies.players.sweep_left()
    updates = await lobbies.lobbies.drop_left_players()
    assert [u.events for u in updates] == [["left"]]
    snapshot = await lobbies.lobbies.snapshot(lobby_id)
    assert sorted(v.discord_id for v in snapshot.roster) == [1, 2, 4]
    assert await lobbies.lobbies.drop_left_players() == []  # nothing left to do


async def test_a_game_already_in_progress_keeps_its_players(lobbies):
    from lobby_fakes import start_game

    lobby_id, _ = await start_game(lobbies)
    server = Server(*range(1, 11))
    lobbies.players.membership = server
    server.leave(4)
    await lobbies.players.sweep_left()
    assert await lobbies.lobbies.drop_left_players() == []
    assert len((await lobbies.lobbies.snapshot(lobby_id)).roster) == 10


# --- coming back -----------------------------------------------------------------------------


async def test_register_brings_a_returning_player_back_with_their_history(lobbies):
    await lobbies.register(1)
    player = await lobbies.players.repo.get_by_discord(1)
    assert player is not None
    before = await lobbies.ratings.repo.get_stored(player.id)
    await lobbies.players.mark_left(player)
    back = await lobbies.players.register(1, "Ana Nova")
    assert (back.id, back.nickname, back.left_at) == (player.id, "Ana Nova", None)
    assert await lobbies.ratings.repo.get_stored(player.id) == before  # nothing was reset
    assert (await lobbies.players.require(1)).id == player.id


async def test_a_returning_player_cannot_take_someone_elses_nickname(lobbies):
    await lobbies.register(2)
    player = await lobbies.players.repo.get_by_discord(1)
    assert player is not None
    await lobbies.players.mark_left(player)
    with pytest.raises(PlayerError, match="já está em uso"):
        await lobbies.players.register(1, "player 02")
    assert (await lobbies.players.repo.get_by_discord(1)).left_at is not None  # type: ignore[union-attr]
    assert (await lobbies.players.register(1, "Player 01")).left_at is None  # their own is theirs


# --- Riot ------------------------------------------------------------------------------------


@pytest.fixture
def world() -> FakeRiot:
    w = FakeRiot()
    w.add_account("Alpha", "EUW", "puuid-alpha")
    w.add_account("Beta", "EUW", "puuid-beta")
    w.set_rank("puuid-alpha", "MASTER", "I", 100)
    w.set_rank("puuid-beta", "GOLD", "II", 40)
    return w


@pytest_asyncio.fixture
async def riot(db, world) -> Stack:
    s = Stack(db, world)
    s.players.on_left.append(s.riot.forget_player)
    await s.register(1, "Ana")
    await s.register(2, "Rui")
    return s


async def tables(riot: Stack) -> dict[str, int]:
    counts = {}
    for table in ("riot_accounts", "riot_role_games", "rank_signals"):
        rows = await riot.rows(f"SELECT COUNT(*) FROM {table}")
        counts[table] = rows[0][0]
    return counts


async def test_leaving_unlinks_every_riot_account_and_keeps_the_player(riot, world):
    await riot.riot.link(1, "Alpha#EUW", None, 1)
    await riot.riot.link(2, "Beta#EUW", None, 2)
    await riot.riot.wait_for_background()
    assert (await tables(riot))["riot_accounts"] == 2
    player = await riot.players.repo.get_by_discord(1)
    assert player is not None
    await riot.players.mark_left(player)
    assert await riot.riot_repo.list_accounts(player.id) == []
    rui = await riot.players.repo.get_by_discord(2)
    assert rui is not None and len(await riot.riot_repo.list_accounts(rui.id)) == 1
    signals = await riot.rows("SELECT account_key FROM rank_signals WHERE source = 'riot'")
    assert [r[0] for r in signals] == ["puuid-beta"]
    assert (await riot.players.repo.get(player.id)) is not None  # history still points at it


async def test_a_player_who_left_is_not_posted_about_even_before_the_next_sweep(riot, world):
    clock = tick(riot)
    await start_watching(riot, clock)
    server = Server(1, 2)
    riot.players.membership = server
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 127)
    server.leave(1)  # left between two sweeps
    posts = Posts()
    summary = await riot.riot.poll_new_games(posts)
    assert posts.items == [] and summary.posted == 0
    player = await riot.players.repo.get_by_discord(1)
    assert player is not None and player.left_at is not None
    assert await riot.riot_repo.list_accounts(player.id) == []
    # The next cycle has nothing to watch.
    assert (await riot.riot.poll_new_games(posts)).watched == 0


async def test_a_present_player_is_posted_as_before(riot, world):
    clock = tick(riot)
    await start_watching(riot, clock)
    riot.players.membership = Server(1, 2)
    clock.advance(minutes=5)
    finish(world, clock, "puuid-alpha", "EUW1_1", 1)
    world.set_rank("puuid-alpha", "MASTER", "I", 127)
    posts = Posts()
    assert (await riot.riot.poll_new_games(posts)).posted == 1


# --- asking Discord --------------------------------------------------------------------------


def cog_with(guild: Any, found: Any = None, error: Exception | None = None) -> Players:
    async def fetch_member(discord_id: int) -> Any:
        if error is not None:
            raise error
        return found

    if guild is not None:
        guild.fetch_member = fetch_member
    bot = SimpleNamespace(get_guild=lambda _id: guild, settings=SimpleNamespace(guild_id=GUILD))
    service = SimpleNamespace(membership=None)
    return Players(bot, service, None)  # type: ignore[arg-type]


def http_error(cls: type, code: int, status: int) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="x")
    exc = cls(response, {"code": code, "message": "x"})  # type: ignore[arg-type]
    return exc


async def test_only_discords_unknown_member_answer_means_gone():
    guild = SimpleNamespace()
    assert await cog_with(guild, found=object())._is_member(1) is True
    gone = http_error(discord.NotFound, UNKNOWN_MEMBER, 404)
    assert await cog_with(guild, error=gone)._is_member(1) is False
    # Unknown guild (another 404), a Discord outage or a missing guild prove nothing.
    wrong_guild = http_error(discord.NotFound, 10004, 404)
    assert await cog_with(guild, error=wrong_guild)._is_member(1) is None
    outage = http_error(discord.HTTPException, 0, 503)
    assert await cog_with(guild, error=outage)._is_member(1) is None
    assert await cog_with(None)._is_member(1) is None
