from dataclasses import replace
from datetime import timedelta

import pytest
import pytest_asyncio
from riot_fakes import NOW, FakeRiot, Stack, days_ago

from bot.features.players.service import PlayerError
from bot.features.ratings.params import MatchmakingParams
from bot.features.ratings.rank import rank_mu
from bot.features.riot.service import (
    CONSENT_NOTICE,
    parse_riot_id,
)

ADMIN = 99
P = MatchmakingParams()


@pytest.fixture
def world() -> FakeRiot:
    w = FakeRiot()
    w.add_account("Alpha", "EUW", "puuid-alpha")
    w.add_account("Beta", "EUW", "puuid-beta")
    w.add_account("Gamma", "NA1", "puuid-gamma", platform="na1")
    return w


@pytest_asyncio.fixture
async def stack(db, world) -> Stack:
    s = Stack(db, world)
    await s.register(1, "Ana")
    await s.register(2, "Rui")
    return s


# --- parsing --------------------------------------------------------------------------------


def test_riot_id_parsing():
    assert parse_riot_id(" Some Name #EUW ") == ("Some Name", "EUW")
    assert parse_riot_id("a#b#TAG") == ("a#b", "TAG")
    for bad in ("NoTag", "#EUW", "Name#", "Name#TA G", "Name#" + "x" * 9, "x" * 33 + "#EUW"):
        with pytest.raises(PlayerError, match="Riot ID inválido"):
            parse_riot_id(bad)


def test_consent_notice_says_what_is_fetched_used_and_visible():
    text = CONSENT_NOTICE.lower()
    for needle in ("rank atual", "ranked", "rating inicial", "visíveis", "não** prova", "unlink"):
        assert needle in text


# --- linking --------------------------------------------------------------------------------


async def test_first_account_becomes_main_gets_its_rank_and_seeds_the_rating(stack, world):
    world.set_rank("puuid-alpha", "GOLD", "II", 40)
    result = await stack.riot.link(1, "alpha#euw", None, linked_by=ADMIN)
    assert result.is_main
    assert result.account.riot_id == "Alpha#EUW"  # canonical casing comes from Riot
    assert result.account.linked_by == ADMIN
    assert result.rank is not None
    assert (result.rank.status, result.rank.label) == ("ranked", "Gold II")
    player = await stack.players.require(1)
    stored = await stack.ratings.repo.get_stored(player.id)
    assert stored is not None
    assert stored.mu == pytest.approx(rank_mu("GOLD", "II", P))  # reseeded: no in-house games yet
    rows = await stack.rows("SELECT source, detail FROM rating_events ORDER BY id")
    assert ("riot", "rank Gold II (riot)") in rows
    assert all("Alpha" not in (r[1] or "") for r in rows)  # no Riot ID in seed details


async def test_second_account_is_not_main_and_adds_no_rank_evidence(stack, world):
    world.set_rank("puuid-alpha", "GOLD", "II")
    world.set_rank("puuid-beta", "DIAMOND", "I")  # a stronger second account
    await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    before = world.count("/lol/league")
    result = await stack.riot.link(1, "Beta#EUW", None, ADMIN)
    assert not result.is_main
    assert result.rank is None
    assert world.count("/lol/league") == before  # no rank lookup for non-main accounts
    player = await stack.players.require(1)
    rank = (await stack.ratings.resolve(player.id)).rank
    assert rank is not None
    assert rank.label() == "Gold II"  # the main alone decides R
    assert await stack.players.account_labeler(player.id) == [
        "Alpha#EUW ★",
        "Beta#EUW",
    ]


async def test_unranked_main_links_without_a_rank_and_keeps_the_default_seed(stack, world):
    result = await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    assert result.rank is not None
    assert result.rank.status == "unranked"
    player = await stack.players.require(1)
    stored = await stack.ratings.repo.get_stored(player.id)
    assert stored is not None
    assert stored.mu == 25.0


async def test_a_puuid_can_only_be_linked_once(stack):
    await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    with pytest.raises(PlayerError, match="já está ligada"):
        await stack.riot.link(1, "alpha#euw", None, ADMIN)
    with pytest.raises(PlayerError, match="já está ligada"):
        await stack.riot.link(2, "Alpha#EUW", None, ADMIN)
    assert len(await stack.rows("SELECT * FROM riot_accounts")) == 1


async def test_link_errors_are_clear_and_leave_nothing_behind(stack, world):
    with pytest.raises(PlayerError, match="Não encontrei"):
        await stack.riot.link(1, "Nobody#EUW", None, ADMIN)
    with pytest.raises(PlayerError, match="plataforma NA1"):
        await stack.riot.link(1, "Alpha#EUW", "na1", ADMIN)  # exists, but not on that platform
    with pytest.raises(PlayerError, match="não registado"):
        await stack.riot.link(404, "Alpha#EUW", None, ADMIN)
    world.forced["/riot/account"] = 403
    with pytest.raises(PlayerError, match="chave da API"):
        await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    assert await stack.rows("SELECT * FROM riot_accounts") == []


async def test_platform_defaults_and_overrides(stack):
    result = await stack.riot.link(1, "Gamma#NA1", "NA1", ADMIN)
    assert result.account.platform == "na1"


async def test_a_failed_rank_lookup_still_links_but_records_nothing(stack, world):
    world.set_rank("puuid-alpha", "GOLD", "II")
    world.forced["/lol/league"] = 500
    result = await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    assert result.rank is not None
    assert result.rank.status == "pending"
    player = await stack.players.require(1)
    assert await stack.ratings.repo.list_signals(player.id) == []
    assert (await stack.ratings.resolve(player.id)).riot_pending
    del world.forced["/lol/league"]
    outcome = await stack.riot.refresh_rank(player.id, ADMIN)
    assert outcome.status == "ranked"


# --- disabled integration -------------------------------------------------------------------


async def test_without_a_key_riot_actions_are_refused_but_stored_data_still_works(db, world):
    s = Stack(db, world)
    await s.register(1, "Ana")
    world.set_rank("puuid-alpha", "GOLD", "II")
    await s.riot.link(1, "Alpha#EUW", None, ADMIN)
    await s.riot.wait_for_background()

    off = Stack(db, world, enabled=False)
    assert not off.riot.enabled
    with pytest.raises(PlayerError, match="desativada"):
        await off.riot.link(1, "Beta#EUW", None, ADMIN)
    with pytest.raises(PlayerError, match="desativada"):
        await off.riot.refresh(1, ADMIN, force=True)
    player = await off.players.require(1)
    assert [a.riot_id for a, _ in await off.riot.accounts(1)] == ["Alpha#EUW"]
    assert (await off.ratings.resolve(player.id)).rank is not None  # stored rank still counts
    await off.riot.role_estimates(player.id)  # estimates keep working from stored games


# --- main account and R ------------------------------------------------------------------------


async def test_changing_main_never_leaves_the_old_accounts_rank_active(stack, world):
    world.set_rank("puuid-alpha", "MASTER")
    world.set_rank("puuid-beta", "SILVER", "III")
    await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    await stack.riot.link(1, "Beta#EUW", None, ADMIN)
    player = await stack.players.require(1)
    rank = (await stack.ratings.resolve(player.id)).rank
    assert rank is not None
    assert rank.tier == "MASTER"

    world.forced["/lol/league"] = 500  # the new main cannot be looked up right now
    account, outcome = await stack.riot.set_main(1, "beta#euw", ADMIN)
    assert account.is_main
    assert outcome.status == "pending"
    resolution = await stack.ratings.resolve(player.id)
    assert resolution.rank is None  # not Master: the old account's rank is no longer R
    assert resolution.riot_pending

    del world.forced["/lol/league"]
    outcome = (await stack.riot.set_main(1, "Beta#EUW", ADMIN))[1]
    assert outcome.label == "Silver III"
    stored = await stack.ratings.repo.get_stored(player.id)
    assert stored is not None
    assert stored.mu == pytest.approx(rank_mu("SILVER", "III", P))  # reseeded (0 games)


async def test_confirmed_unranked_supersedes_but_a_failed_refresh_does_not(stack, world):
    world.set_rank("puuid-alpha", "GOLD", "II")
    await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    player = await stack.players.require(1)

    world.forced["/lol/league"] = 429
    with pytest.raises(PlayerError):
        await stack.riot.refresh_rank(player.id, ADMIN)
    rank = (await stack.ratings.resolve(player.id)).rank
    assert rank is not None
    assert rank.label() == "Gold II"  # the failure changed nothing

    del world.forced["/lol/league"]
    world.set_rank("puuid-alpha", None)  # Riot now confirms: no Solo/Duo rank
    assert (await stack.riot.refresh_rank(player.id, ADMIN)).status == "unranked"
    assert (await stack.ratings.resolve(player.id)).rank is None


async def test_set_main_for_an_account_that_is_not_theirs_is_refused(stack):
    await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    with pytest.raises(PlayerError, match="não está ligada"):
        await stack.riot.set_main(2, "Alpha#EUW", ADMIN)


# --- unlinking: delete means delete -------------------------------------------------------------


async def test_unlink_deletes_everything_keyed_by_the_account_and_ratings_are_unchanged(
    stack, world
):
    now = NOW
    world.set_rank("puuid-alpha", "GOLD", "II")
    for i in range(5):
        world.add_game("puuid-alpha", f"EUW1_{i}", "MIDDLE", days_ago(now, i + 1))
    await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    await stack.riot.wait_for_background()
    player = await stack.players.require(1)
    assert len(await stack.rows("SELECT * FROM riot_role_games")) == 5
    ratings_before = await stack.rows("SELECT * FROM ratings")
    events_before = await stack.rows("SELECT * FROM rating_events")
    history_before = await stack.rows("SELECT * FROM rating_history")

    result = await stack.riot.unlink(1, "alpha#euw")
    assert result.removed.riot_id == "Alpha#EUW"
    assert result.new_main is None  # it was the only account
    for table, column in (
        ("riot_accounts", "puuid"),
        ("rank_signals", "account_key"),
    ):
        assert await stack.rows(f"SELECT * FROM {table} WHERE {column} = 'puuid-alpha'") == []
    assert await stack.rows("SELECT * FROM riot_role_games") == []
    assert await stack.rows("SELECT * FROM rank_signals WHERE source = 'riot'") == []
    # Ratings, seeds and history are untouched, and nothing in them names the account.
    assert await stack.rows("SELECT * FROM ratings") == ratings_before
    assert await stack.rows("SELECT * FROM rating_events") == events_before
    assert await stack.rows("SELECT * FROM rating_history") == history_before
    dump = " ".join(str(r) for r in await stack.rows("SELECT * FROM rating_events"))
    assert "Alpha" not in dump
    assert "puuid-alpha" not in dump

    # Nothing left to promote; R no longer comes from the removed account.
    assert await stack.riot_repo.main_puuid(player.id) is None
    assert (await stack.ratings.resolve(player.id)).rank is None
    async with stack.db.conn.execute("PRAGMA foreign_key_check") as cur:
        assert list(await cur.fetchall()) == []


async def test_relinking_starts_clean(stack, world):
    world.add_game("puuid-alpha", "EUW1_1", "MIDDLE", days_ago(NOW, 1))
    await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    await stack.riot.wait_for_background()
    await stack.riot.unlink(1, "Alpha#EUW")
    again = await stack.riot.link(2, "Alpha#EUW", None, ADMIN)  # even for someone else
    await stack.riot.wait_for_background()
    assert again.is_main
    assert await stack.riot_repo.count_games(again.account.id) == 1  # re-fetched, not inherited


# --- history sync and role estimates ------------------------------------------------------------


async def test_history_sync_stores_new_games_incrementally(stack, world):
    for i in range(6):
        world.add_game("puuid-alpha", f"EUW1_{i}", "MIDDLE", days_ago(NOW, i + 1))
    result = await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    await stack.riot.wait_for_background()
    account = result.account
    assert await stack.riot_repo.count_games(account.id) == 6
    assert (await stack.riot_repo.get(account.id)).history_synced_at == NOW  # type: ignore[union-attr]

    detail_calls = world.count("/lol/match/v5/matches/EUW1")
    world.add_game("puuid-alpha", "EUW1_new", "TOP", days_ago(NOW, 0.1))
    assert await stack.riot.sync_history(account) == 1
    assert world.count("/lol/match/v5/matches/EUW1") == detail_calls + 1  # only the new game


async def test_history_sync_skips_removed_matches_and_other_queues(stack, world):
    world.add_game("puuid-alpha", "EUW1_a", "MIDDLE", days_ago(NOW, 1))
    world.add_game("puuid-alpha", "EUW1_flex", "MIDDLE", days_ago(NOW, 2), queue=440)
    world.match_ids["puuid-alpha"].append("EUW1_gone")  # listed but deleted by Riot
    result = await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    await stack.riot.wait_for_background()
    assert await stack.riot_repo.count_games(result.account.id) == 1


async def test_history_sync_is_resumable_after_a_rate_limit(stack, world):
    for i in range(4):
        world.add_game("puuid-alpha", f"EUW1_{i}", "MIDDLE", days_ago(NOW, i + 1))
    result = await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    await stack.riot.wait_for_background()
    account = result.account
    world.add_game("puuid-alpha", "EUW1_x", "MIDDLE", days_ago(NOW, 0.5))
    world.add_game("puuid-alpha", "EUW1_y", "MIDDLE", days_ago(NOW, 0.4))
    # Newest first: EUW1_y is stored, then the rate limit hits on the older EUW1_x.
    world.forced["/lol/match/v5/matches/EUW1_x"] = 429
    with pytest.raises(PlayerError, match="limitar"):
        await stack.riot.sync_history(account)
    assert await stack.riot_repo.count_games(account.id) == 5
    del world.forced["/lol/match/v5/matches/EUW1_x"]
    assert await stack.riot.sync_history(account) == 1
    assert await stack.riot_repo.count_games(account.id) == 6


async def test_old_and_surplus_games_are_pruned(stack, world):
    account = (await stack.riot.link(1, "Alpha#EUW", None, ADMIN)).account
    await stack.riot.wait_for_background()
    await stack.riot.sync_history(account)
    stack.riot.history = replace(stack.riot.history, max_games_per_account=3)
    for i in range(6):
        world.add_game("puuid-alpha", f"EUW1_{i}", "MIDDLE", days_ago(NOW, i + 1))
    await stack.riot.sync_history(account)
    assert await stack.riot_repo.count_games(account.id) == 3  # newest three kept


async def test_estimates_pool_every_linked_account_into_one_person(stack, world):
    for i in range(14):
        world.add_game("puuid-alpha", f"A_{i}", "MIDDLE", days_ago(NOW, i + 1))
    for i in range(14):
        world.add_game("puuid-beta", f"B_{i}", "MIDDLE", days_ago(NOW, i + 1))
    world.add_game("puuid-beta", "B_top", "TOP", days_ago(NOW, 2))
    await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    await stack.riot.link(1, "Beta#EUW", None, ADMIN)
    await stack.riot.wait_for_background()
    player = await stack.players.require(1)
    estimates = await stack.riot.role_estimates(player.id)
    assert estimates["MID"] == 4  # two accounts' games add up (about 22 weighted games)
    assert estimates["TOP"] == 1
    assert estimates["ADC"] == 1  # a confident profile with no games there: "1", not unknown
    nobody = await stack.players.require(2)  # no accounts linked: everything unknown
    assert await stack.riot.role_estimates(nobody.id) == dict.fromkeys(estimates)


async def test_profile_uses_estimates_below_declarations_and_lists_accounts(stack, world):
    for i in range(14):
        world.add_game("puuid-alpha", f"A_{i}", "BOTTOM", days_ago(NOW, i + 1))
    await stack.riot.link(1, "Alpha#EUW", None, ADMIN)
    await stack.riot.wait_for_background()
    await stack.players.set_declared(1, "SUPPORT", 5)
    profile = await stack.players.profile(1)
    by_role = {line.role: line.proficiency for line in profile.roles}
    assert (by_role["ADC"].value, by_role["ADC"].source) == (3, "estimated")
    assert (by_role["SUPPORT"].value, by_role["SUPPORT"].source) == (5, "declared")
    assert profile.accounts == ["Alpha#EUW ★"]


# --- refresh --------------------------------------------------------------------------------------


async def test_refresh_updates_names_rank_and_enforces_the_cooldown(db, world):
    clock = {"now": NOW}
    s = Stack(db, world)
    s.riot.clock = lambda: clock["now"]
    await s.register(1, "Ana")
    world.set_rank("puuid-alpha", "SILVER", "I")
    await s.riot.link(1, "Alpha#EUW", None, ADMIN)
    await s.riot.wait_for_background()

    world.accounts[("alpha", "euw")]["gameName"] = "AlphaRenamed"  # the player renamed in-game
    world.set_rank("puuid-alpha", "GOLD", "IV")
    outcome = await s.riot.refresh(1, 1)
    assert outcome.label == "Gold IV"
    assert (await s.riot.accounts(1))[0][0].riot_id == "AlphaRenamed#EUW"
    with pytest.raises(PlayerError, match="Tenta de novo"):
        await s.riot.refresh(1, 1)
    await s.riot.refresh(1, ADMIN, force=True)  # admins skip the cooldown
    clock["now"] = NOW + timedelta(minutes=11)
    await s.riot.refresh(1, 1)
    await s.riot.wait_for_background()


async def test_refresh_without_accounts(stack):
    with pytest.raises(PlayerError, match="não tem contas"):
        await stack.riot.refresh(1, 1, force=True)
