import random

import pytest
import pytest_asyncio
from lobby_fakes import ADMIN, CHANNEL, GUILD, LobbyStack

from bot.db import Database
from bot.features.lobby.mock import DISABLED, MockPlayers
from bot.features.players.service import PlayerError


@pytest_asyncio.fixture
async def stack(db: Database) -> LobbyStack:
    return LobbyStack(db)


def make_mock(stack: LobbyStack, enabled: bool = True, seed: int = 3) -> MockPlayers:
    return MockPlayers(stack.players, stack.ratings, stack.lobbies, enabled, random.Random(seed))


async def test_disabled_by_default_and_everything_refuses(stack):
    mock = make_mock(stack, enabled=False)
    for call in (
        mock.create(3, ADMIN),
        mock.all_players(),
        mock.clear(),
        mock.fill(1, 3, ADMIN),
        mock.ready_all(1),
        mock.drop_all(1),
    ):
        with pytest.raises(PlayerError, match="ENABLE_MOCK_PLAYERS"):
            await call
    assert DISABLED.endswith("(ENABLE_MOCK_PLAYERS).")
    assert await stack.players.repo.list_mock() == []


async def test_created_players_are_flagged_unique_and_never_look_like_discord_users(stack):
    mock = make_mock(stack)
    first = await mock.create(3, ADMIN)
    second = await mock.create(2, ADMIN)
    everyone = first + second
    assert [p.nickname for p in everyone] == [f"Mock {i:02d}" for i in range(1, 6)]
    assert all(p.is_mock for p in everyone)
    assert all(p.discord_id < 0 for p in everyone)  # Discord snowflakes are positive
    assert len({p.discord_id for p in everyone}) == 5
    assert len(await mock.all_players()) == 5


async def test_mock_players_get_a_rank_a_rating_and_role_comfort_like_real_ones(stack):
    mock = make_mock(stack)
    [player] = await mock.create(1, ADMIN)
    inspection = await stack.ratings.inspect(player.id)
    assert inspection.resolution.rank is not None  # a self-reported League rank
    assert inspection.stored.sigma == pytest.approx(6.5)  # seeded from it
    lines = await stack.players.role_lines(player.id)
    values = [line.proficiency.value for line in lines]
    assert all(v is not None for v in values)
    assert 5 in values  # one main role


async def test_creation_limits_and_real_players_are_not_mocks(stack):
    mock = make_mock(stack)
    for bad in (0, 31):
        with pytest.raises(PlayerError, match="entre 1 e 30"):
            await mock.create(bad, ADMIN)
    await stack.register(1)
    real = await stack.players.require(1)
    assert not real.is_mock
    assert real.discord_id > 0


async def test_one_real_player_plus_mocks_can_play_a_whole_lobby(stack):
    mock = make_mock(stack)
    await stack.register(1)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    result = await mock.fill(lobby_id, 9, ADMIN)
    assert len(result.players) == 9
    assert result.events.count("ready_check_started") == 1
    snap = await stack.lobbies.snapshot(lobby_id)
    assert snap.lobby.status == "READY_CHECK"  # the ninth mock was the tenth player
    assert sum(v.is_mock for v in snap.roster) == 9

    update = await mock.ready_all(lobby_id)
    assert update.snapshot.lobby.status == "READY_CHECK"  # the real player has not pressed Ready
    update = await stack.lobbies.ready(lobby_id, 1)
    assert "teams_proposed" in update.events
    assert update.snapshot.proposal is not None
    update = await stack.lobbies.confirm(lobby_id, 1)
    assert update.snapshot.lobby.status == "IN_PROGRESS"


async def test_fill_reuses_free_mocks_and_creates_more_only_when_needed(stack):
    mock = make_mock(stack)
    await stack.register(1)
    await mock.create(4, ADMIN)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    joined = (await mock.fill(update.snapshot.lobby.id, 6, ADMIN)).players
    assert {p.nickname for p in joined} >= {"Mock 01", "Mock 02", "Mock 03", "Mock 04"}
    assert len(await mock.all_players()) == 6  # two new ones were created
    again = (await mock.fill(update.snapshot.lobby.id, 1, ADMIN)).players
    assert again[0].nickname not in {p.nickname for p in joined}  # nobody joins twice


async def test_ready_all_and_drop_all_need_the_right_state(stack):
    mock = make_mock(stack)
    await stack.register(1)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    with pytest.raises(PlayerError, match="Não há ready-check"):
        await mock.ready_all(lobby_id)
    with pytest.raises(PlayerError, match="Não há jogadores de teste"):
        await mock.drop_all(lobby_id)
    await mock.fill(lobby_id, 9, ADMIN)
    update = await mock.drop_all(lobby_id)  # every mock leaves: back to a lobby with one player
    assert update.snapshot.lobby.status == "OPEN"
    assert len(update.snapshot.roster) == 1


async def test_a_dropped_mock_is_backfilled_like_any_player(stack):
    mock = make_mock(stack)
    await stack.register(1)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    await mock.fill(lobby_id, 10, ADMIN)  # nine in the roster, one reserve
    snap = await stack.lobbies.snapshot(lobby_id)
    assert snap.lobby.status == "READY_CHECK"
    assert (len(snap.roster), len(snap.reserves)) == (10, 1)
    leaver = snap.roster[1]
    update = await stack.lobbies.leave_player(lobby_id, leaver.player_id)
    assert leaver.player_id not in [v.player_id for v in update.snapshot.roster]
    assert len(update.snapshot.roster) == 10  # the reserve moved up


async def test_clear_removes_only_mocks_and_only_when_no_lobby_uses_them(stack):
    mock = make_mock(stack)
    await stack.register(2)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    await mock.fill(lobby_id, 4, ADMIN)
    with pytest.raises(PlayerError, match="lobbies ativas"):
        await mock.clear()
    await stack.lobbies.cancel(lobby_id, 1)
    assert await mock.clear() == 4
    assert await stack.players.repo.list_mock() == []
    assert (await stack.players.require(1)).nickname == "Player 01"  # real players are untouched
    assert (await stack.players.require(2)).nickname == "Player 02"
    for table in ("rating_events", "rank_signals", "ratings", "player_roles", "lobby_entries"):
        async with stack.db.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE player_id < 0 OR player_id IN"
            " (SELECT id FROM players WHERE is_mock = 1)"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0
    async with stack.db.conn.execute("PRAGMA foreign_key_check") as cur:
        assert list(await cur.fetchall()) == []
    assert (await mock.create(1, ADMIN))[0].nickname == "Mock 01"  # numbering starts over


async def test_real_players_ratings_survive_a_clear(stack):
    mock = make_mock(stack)
    await stack.register(1)
    before = await stack.ratings.repo.get_stored((await stack.players.require(1)).id)
    await mock.create(3, ADMIN)
    await mock.clear()
    assert await stack.ratings.repo.get_stored((await stack.players.require(1)).id) == before


async def test_create_with_a_main_role_makes_every_mock_main_it(stack):
    mock = make_mock(stack)
    junglers = await mock.create(5, ADMIN, main_role="JUNGLE")
    for player in junglers:
        values = {
            line.role: line.proficiency.value for line in await stack.players.role_lines(player.id)
        }
        assert values["JUNGLE"] == 5
        assert all(v is not None and 1 <= v <= 4 for r, v in values.items() if r != "JUNGLE")
    with pytest.raises(PlayerError, match="Role inválida"):
        await mock.create(1, ADMIN, main_role="BOT")


async def test_describe_shows_the_best_roles_and_the_rank(stack):
    mock = make_mock(stack)
    [player] = await mock.create(1, ADMIN, main_role="ADC")
    text = await mock.describe(player)
    assert text.startswith("Mock 01 — ADC 5")
    assert "·" in text
    assert any(tier in text for tier in ("Iron", "Bronze", "Silver", "Gold", "Platinum", "Emerald"))


async def test_a_lobby_full_of_junglers_still_gets_one_jungler_per_team(stack):
    mock = make_mock(stack)
    await stack.register(1)
    await mock.create(5, ADMIN, main_role="JUNGLE")
    await mock.create(4, ADMIN)
    update = await stack.lobbies.open(GUILD, CHANNEL, 1)
    lobby_id = update.snapshot.lobby.id
    await mock.fill(lobby_id, 9, ADMIN)
    await mock.ready_all(lobby_id)
    update = await stack.lobbies.ready(lobby_id, 1)
    proposal = update.snapshot.proposal
    assert proposal is not None
    jungle = [s for s in proposal.blue + proposal.red if s.role == "JUNGLE"]
    assert len(jungle) == 2
    assert {s.player_id for s in jungle}.isdisjoint({s.player_id for s in proposal.blue}) is False
    assert sum(1 for s in proposal.blue if s.role == "JUNGLE") == 1
    assert sum(1 for s in proposal.red if s.role == "JUNGLE") == 1
