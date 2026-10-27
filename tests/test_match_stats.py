"""Champion and KDA per player per game (M3b): entering, validating, correcting, migration."""

import pytest
import pytest_asyncio
from lobby_fakes import ADMIN, GUILD, LobbyStack, sides_of, start_game

from bot.db import Database
from bot.features.players.service import PlayerError


@pytest_asyncio.fixture
async def stack(db: Database) -> LobbyStack:
    stack = LobbyStack(db)
    await stack.champions.start()  # the bundled list
    await stack.register(10)  # first, so that player ids equal Discord ids for players 1..10
    await stack.register(1, start=ADMIN)
    return stack


async def confirmed_game(stack: LobbyStack):
    """A finished, confirmed game; returns (match id, blue ids, red ids). Players are 1..10."""
    lobby_id, match = await start_game(stack)
    sides = await sides_of(stack, match.id)
    await stack.lobbies.report_result(lobby_id, ADMIN, "blue")  # an admin's report is final
    return match.id, sides["blue"], sides["red"]


async def row(stack: LobbyStack, match_id: int, player: int):
    found = await stack.match_repo.participant(match_id, player)
    assert found is not None
    return found


# --- entering ----


async def test_a_player_enters_their_stats_and_they_wait_for_an_admin(stack):
    match_id, blue, _ = await confirmed_game(stack)
    result = await stack.matches.enter_stats(blue[0], "ahri", 8, 2, 11, match_id)
    assert result.champion_name == "Ahri"
    mine = await row(stack, match_id, blue[0])
    assert (mine.champion, mine.kills, mine.deaths, mine.assists) == ("Ahri", 8, 2, 11)
    assert mine.stats_status == "pending"
    assert mine.stats_set_by == blue[0] and mine.stats_validated_by is None
    audit = await stack.match_repo.audit(match_id)
    assert audit[-1].action == "stats"
    assert audit[-1].subject_id == blue[0]
    assert audit[-1].new_value == "Ahri 8/2/11"
    assert audit[-1].old_value is None


async def test_the_champion_is_stored_as_its_data_dragon_id(stack):
    match_id, blue, _ = await confirmed_game(stack)
    await stack.matches.enter_stats(blue[0], "Wukong", 1, 1, 1, match_id)
    assert (await row(stack, match_id, blue[0])).champion == "MonkeyKing"


async def test_without_a_match_number_it_is_the_latest_game_without_validated_stats(stack):
    first, blue1, _ = await confirmed_game(stack)
    second, blue2, _ = await confirmed_game(stack)
    player = 1
    result = await stack.matches.enter_stats(player, "Ahri", 1, 2, 3)
    assert result.match_id == second
    await stack.matches.validate_stats(ADMIN, second, player)
    result = await stack.matches.enter_stats(player, "Zed", 4, 5, 6)
    assert result.match_id == first  # the newest game is validated, so the older one is next
    assert blue1 and blue2


async def test_with_nothing_left_to_fill_it_says_so(stack):
    await stack.register(1)
    with pytest.raises(PlayerError, match="Indica o número do jogo"):
        await stack.matches.enter_stats(1, "Ahri", 1, 2, 3)


async def test_only_a_confirmed_game_takes_stats(stack):
    lobby_id, match = await start_game(stack)  # in progress
    with pytest.raises(PlayerError, match="confirmado"):
        await stack.matches.enter_stats(1, "Ahri", 1, 2, 3, match.id)
    await stack.lobbies.report_result(lobby_id, 1, "blue")  # reported, not confirmed
    with pytest.raises(PlayerError, match="confirmado"):
        await stack.matches.enter_stats(1, "Ahri", 1, 2, 3, match.id)


async def test_a_voided_game_takes_no_stats(stack):
    match_id, blue, _ = await confirmed_game(stack)
    await stack.lobbies.void_match(match_id, ADMIN, "teste")
    with pytest.raises(PlayerError, match="anulado"):
        await stack.matches.enter_stats(blue[0], "Ahri", 1, 2, 3, match_id)


async def test_only_those_who_played_can_enter_stats(stack):
    match_id, _, _ = await confirmed_game(stack)
    await stack.register(1, start=50)
    with pytest.raises(PlayerError, match="Não jogaste"):
        await stack.matches.enter_stats(50, "Ahri", 1, 2, 3, match_id)
    with pytest.raises(PlayerError, match="não existe"):
        await stack.matches.enter_stats(1, "Ahri", 1, 2, 3, 9999)


async def test_unknown_champions_and_silly_numbers_are_refused(stack):
    match_id, blue, _ = await confirmed_game(stack)
    with pytest.raises(PlayerError, match="Não conheço o campeão"):
        await stack.matches.enter_stats(blue[0], "Not A Champion", 1, 2, 3, match_id)
    for bad in ((-1, 0, 0), (0, 100, 0), (0, 0, 100)):
        with pytest.raises(PlayerError, match="entre 0 e 99"):
            await stack.matches.enter_stats(blue[0], "Ahri", *bad, match_id)
    assert (await row(stack, match_id, blue[0])).stats_status == "none"


async def test_a_player_must_be_registered(stack):
    with pytest.raises(PlayerError):
        await stack.matches.enter_stats(777, "Ahri", 1, 2, 3, 1)


async def test_repeating_the_same_values_is_refused_and_changes_nothing(stack):
    match_id, blue, _ = await confirmed_game(stack)
    await stack.matches.enter_stats(blue[0], "Ahri", 8, 2, 11, match_id)
    with pytest.raises(PlayerError, match="falta um admin validar"):
        await stack.matches.enter_stats(blue[0], "Ahri", 8, 2, 11, match_id)
    await stack.matches.validate_stats(ADMIN, match_id, blue[0])
    with pytest.raises(PlayerError, match="já estão validados"):
        await stack.matches.enter_stats(blue[0], "Ahri", 8, 2, 11, match_id)
    assert (await row(stack, match_id, blue[0])).stats_status == "validated"
    assert [a.action for a in await stack.match_repo.audit(match_id)].count("stats") == 1


async def test_without_a_champion_list_stats_are_unavailable(db):
    stack = LobbyStack(db)
    await stack.register(1)
    stack.matches.champions = None
    with pytest.raises(PlayerError, match="lista de campeões"):
        await stack.matches.enter_stats(1, "Ahri", 1, 2, 3, 1)


# --- validating ----


async def test_an_admin_validates_one_player_or_the_whole_game(stack):
    match_id, blue, red = await confirmed_game(stack)
    for player in (blue[0], blue[1], red[0]):
        await stack.matches.enter_stats(player, "Ahri", 1, 2, 3, match_id)
    [one] = await stack.matches.validate_stats(ADMIN, match_id, blue[0])
    assert one.participant.stats_status == "validated"
    assert one.participant.stats_validated_by == (await stack.players.require(ADMIN)).id
    rest = await stack.matches.validate_stats(ADMIN, match_id)
    assert {r.participant.player_id for r in rest} == {blue[1], red[0]}
    audit = await stack.match_repo.audit(match_id)
    assert [a.action for a in audit].count("stats_validate") == 3
    with pytest.raises(PlayerError, match="Não há stats por validar"):
        await stack.matches.validate_stats(ADMIN, match_id)


async def test_only_an_admin_validates_and_only_pending_stats(stack):
    match_id, blue, _ = await confirmed_game(stack)
    await stack.matches.enter_stats(blue[0], "Ahri", 1, 2, 3, match_id)
    with pytest.raises(PlayerError, match="admin"):
        await stack.matches.validate_stats(blue[0], match_id)
    with pytest.raises(PlayerError, match="Não há stats por validar"):
        await stack.matches.validate_stats(ADMIN, match_id, blue[1])  # no stats yet
    assert (await row(stack, match_id, blue[0])).stats_status == "pending"


async def test_editing_validated_stats_sends_them_back_to_pending(stack):
    match_id, blue, _ = await confirmed_game(stack)
    await stack.matches.enter_stats(blue[0], "Ahri", 8, 2, 11, match_id)
    await stack.matches.validate_stats(ADMIN, match_id, blue[0])
    await stack.matches.enter_stats(blue[0], "Ahri", 9, 2, 11, match_id)
    mine = await row(stack, match_id, blue[0])
    assert (mine.kills, mine.stats_status, mine.stats_validated_by) == (9, "pending", None)
    last = (await stack.match_repo.audit(match_id))[-1]
    assert (last.old_value, last.new_value) == ("Ahri 8/2/11", "Ahri 9/2/11")


async def test_the_pending_list_is_per_server_oldest_game_first(stack):
    first, blue1, _ = await confirmed_game(stack)
    second, blue2, _ = await confirmed_game(stack)
    await stack.matches.enter_stats(blue2[0], "Ahri", 1, 2, 3, second)
    await stack.matches.enter_stats(blue1[0], "Zed", 1, 2, 3, first)
    pending = await stack.matches.pending_stats(GUILD, ADMIN)
    assert [(p.match_id, p.player_id) for p in pending] == [(first, blue1[0]), (second, blue2[0])]
    assert await stack.matches.pending_stats(GUILD + 1, ADMIN) == []
    with pytest.raises(PlayerError, match="admin"):
        await stack.matches.pending_stats(GUILD, 1)
    await stack.matches.validate_stats(ADMIN, first)
    assert [p.match_id for p in await stack.matches.pending_stats(GUILD, ADMIN)] == [second]


# --- admin set ----


async def test_an_admin_sets_stats_and_they_count_as_validated(stack):
    match_id, blue, _ = await confirmed_game(stack)
    result = await stack.matches.set_stats(ADMIN, match_id, blue[0], "Lux", 3, 4, 5, "SUPPORT")
    mine = result.participant
    admin_id = (await stack.players.require(ADMIN)).id
    assert (mine.champion, mine.kills, mine.deaths, mine.assists) == ("Lux", 3, 4, 5)
    assert mine.stats_status == "validated"
    assert (mine.stats_set_by, mine.stats_validated_by) == (admin_id, admin_id)
    assert mine.played_role == "SUPPORT"
    last = (await stack.match_repo.audit(match_id))[-1]
    assert (last.action, last.subject_id, last.new_value) == (
        "stats_set",
        blue[0],
        "Lux 3/4/5 (Suporte)",
    )


async def test_set_keeps_the_fields_left_out(stack):
    match_id, blue, _ = await confirmed_game(stack)
    await stack.matches.enter_stats(blue[0], "Ahri", 8, 2, 11, match_id)
    await stack.matches.set_stats(ADMIN, match_id, blue[0], deaths=3)
    mine = await row(stack, match_id, blue[0])
    assert (mine.champion, mine.kills, mine.deaths, mine.assists) == ("Ahri", 8, 3, 11)
    assert mine.stats_status == "validated"
    last = (await stack.match_repo.audit(match_id))[-1]
    assert (last.old_value, last.new_value) == ("Ahri 8/2/11", "Ahri 8/3/11")


async def test_set_needs_all_four_stats_for_a_player_without_any(stack):
    match_id, blue, _ = await confirmed_game(stack)
    with pytest.raises(PlayerError, match="ainda não tem stats"):
        await stack.matches.set_stats(ADMIN, match_id, blue[0], "Ahri", 1)
    assert (await row(stack, match_id, blue[0])).stats_status == "none"


async def test_set_can_correct_only_the_role_played(stack):
    match_id, blue, _ = await confirmed_game(stack)
    await stack.matches.set_stats(ADMIN, match_id, blue[0], played_role="JUNGLE")
    mine = await row(stack, match_id, blue[0])
    assert (mine.played_role, mine.stats_status, mine.champion) == ("JUNGLE", "none", None)
    await stack.matches.enter_stats(blue[0], "Ahri", 1, 2, 3, match_id)
    mine = await row(stack, match_id, blue[0])
    assert (mine.played_role, mine.stats_status) == ("JUNGLE", "pending")


async def test_set_rules(stack):
    match_id, blue, _ = await confirmed_game(stack)
    with pytest.raises(PlayerError, match="admin"):
        await stack.matches.set_stats(blue[0], match_id, blue[1], "Ahri", 1, 2, 3)
    with pytest.raises(PlayerError, match="Diz o que corrigir"):
        await stack.matches.set_stats(ADMIN, match_id, blue[0])
    with pytest.raises(PlayerError, match="Role inválida"):
        await stack.matches.set_stats(ADMIN, match_id, blue[0], played_role="CARRY")
    with pytest.raises(PlayerError, match="não registado"):
        await stack.matches.set_stats(ADMIN, match_id, 4242, "Ahri", 1, 2, 3)
    await stack.register(1, start=60)
    with pytest.raises(PlayerError, match="não jogou"):
        await stack.matches.set_stats(ADMIN, match_id, 60, "Ahri", 1, 2, 3)
    with pytest.raises(PlayerError, match="Não conheço"):
        await stack.matches.set_stats(ADMIN, match_id, blue[0], "Nobody", 1, 2, 3)
    with pytest.raises(PlayerError, match="entre 0 e 99"):
        await stack.matches.set_stats(ADMIN, match_id, blue[0], "Ahri", 100, 2, 3)


async def test_a_failure_while_setting_stats_leaves_nothing_behind(stack, monkeypatch):
    match_id, blue, _ = await confirmed_game(stack)

    async def boom(*_a, **_k):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(stack.match_repo, "add_audit", boom)
    with pytest.raises(RuntimeError):
        await stack.matches.set_stats(ADMIN, match_id, blue[0], "Ahri", 1, 2, 3)
    assert (await row(stack, match_id, blue[0])).stats_status == "none"


# --- stats never touch the result or the ratings ----


async def test_stats_do_not_change_ratings_or_the_result(stack):
    match_id, blue, red = await confirmed_game(stack)
    before = {r.player_id: (r.mu, r.sigma) for r in await stack.ratings.repo.list_stored()}
    await stack.matches.enter_stats(blue[0], "Ahri", 99, 0, 99, match_id)
    await stack.matches.set_stats(ADMIN, match_id, red[0], "Zed", 0, 99, 0)
    after = {r.player_id: (r.mu, r.sigma) for r in await stack.ratings.repo.list_stored()}
    assert after == before
    match = await stack.match_repo.get(match_id)
    assert match is not None and (match.status, match.winner) == ("confirmed", "blue")


# --- migration 9 ----


async def test_migration_9_keeps_existing_matches_and_audit_rows(tmp_path):
    import aiosqlite

    from bot.db import MIGRATIONS

    path = tmp_path / "kdbot.sqlite3"
    async with aiosqlite.connect(path) as raw:
        for i, script in enumerate(MIGRATIONS[:8], start=1):
            await raw.executescript(f"BEGIN;{script};PRAGMA user_version = {i};COMMIT;")
        await raw.execute("PRAGMA foreign_keys = ON")
        await raw.execute(
            "INSERT INTO players (id, discord_id, nickname, created_at) VALUES (1, 11, 'A', 'x')"
        )
        await raw.execute(
            "INSERT INTO lobbies (id, guild_id, channel_id, kind, status, created_by, created_at,"
            " updated_at) VALUES (1, 1, 1, 'spontaneous', 'COMPLETED', 1, 'x', 'x')"
        )
        await raw.execute(
            "INSERT INTO matches (id, lobby_id, guild_id, status, winner, expected_blue_win,"
            " created_at) VALUES (1, 1, 1, 'confirmed', 'blue', 0.5, 'x')"
        )
        await raw.execute(
            "INSERT INTO match_participants (match_id, player_id, side, role, h_mu_before,"
            " h_sigma_before, est_mu, est_sigma) VALUES (1, 1, 'blue', 'TOP', 25, 8, 25, 8)"
        )
        await raw.execute(
            "INSERT INTO match_audit (match_id, action, actor_id, old_value, new_value, created_at)"
            " VALUES (1, 'report', 11, NULL, 'blue', 'x')"
        )
        await raw.commit()
    database = Database(path)
    await database.connect()
    try:
        async with database.conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone()) == (len(MIGRATIONS),)
        async with database.conn.execute(
            "SELECT action, actor_id, new_value, subject_id FROM match_audit"
        ) as cur:
            assert list(await cur.fetchall()) == [("report", 11, "blue", None)]
        async with database.conn.execute(
            "SELECT champion, kills, stats_status, played_role FROM match_participants"
        ) as cur:
            assert list(await cur.fetchall()) == [(None, None, "none", None)]
        await database.conn.execute(  # the recreated CHECK now allows the stats actions
            "INSERT INTO match_audit (match_id, action, created_at) VALUES (1, 'stats_set', 'x')"
        )
        with pytest.raises(Exception, match="CHECK"):
            await database.conn.execute(
                "INSERT INTO match_audit (match_id, action, created_at) VALUES (1, 'nope', 'x')"
            )
        async with database.conn.execute("PRAGMA foreign_key_check") as cur:
            assert list(await cur.fetchall()) == []
    finally:
        await database.close()
