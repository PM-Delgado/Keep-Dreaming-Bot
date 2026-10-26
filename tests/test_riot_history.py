"""/riot history: stored stats, view model, formatting, service rules and the command."""

import sqlite3
from datetime import timedelta

import aiosqlite
import pytest
import pytest_asyncio
from riot_fakes import NOW, FakeRiot, Stack, days_ago
from test_riot_cog import FakeInteraction, member

from bot.config import load_settings
from bot.db import MIGRATIONS, Database
from bot.features.players.service import PlayerError
from bot.features.riot.client import RiotUnavailable, parse_ranked_game
from bot.features.riot.cog import Riot, build_history_embed
from bot.features.riot.history import (
    format_ago,
    format_count,
    format_duration,
    format_line,
    format_summary,
    summarize,
    to_entry,
)
from bot.features.riot.repository import StoredGame
from bot.features.riot.service import CONSENT_NOTICE

ENV = {
    "DISCORD_TOKEN": "t",
    "GUILD_ID": "123",
    "ASSETS_URL": "https://x.invalid/a/",
    "DB_PATH": ":memory:",
    "ADMIN_USER_IDS": "99",
}
STATS = {
    "totalMinionsKilled": 180,
    "neutralMinionsKilled": 12,
    "totalDamageDealtToChampions": 24_130,
    "goldEarned": 12_345,
    "visionScore": 25,
}


def stored(**overrides) -> StoredGame:
    base = dict(
        match_id="EUW1_1",
        riot_id="Alpha#EUW",
        played_at=NOW - timedelta(days=2),
        team_position="MIDDLE",
        individual_position="MIDDLE",
        win=True,
        kills=7,
        deaths=2,
        assists=9,
        duration_s=1860,
        early_surrender=False,
        champion="Ahri",
        cs=192,
        damage=24_130,
        gold=12_345,
        vision_score=25,
    )
    return StoredGame(**{**base, **overrides})


# --- parsing the extra stats ----------


def payload(**participant):
    return {
        "metadata": {"matchId": "EUW1_9"},
        "info": {
            "queueId": 420,
            "gameCreation": 1_700_000_000_000,
            "participants": [
                {
                    "puuid": "p",
                    "win": True,
                    "timePlayed": 1800,
                    "championName": "Jinx",
                    **participant,
                }
            ],
        },
    }


def test_parser_reads_the_extra_stats_and_sums_cs():
    game = parse_ranked_game(payload(**STATS), "p")
    assert game is not None
    assert (game.cs, game.damage, game.gold, game.vision_score) == (192, 24_130, 12_345, 25)


def test_parser_leaves_missing_stats_as_none_not_zero():
    game = parse_ranked_game(payload(), "p")
    assert game is not None
    assert (game.cs, game.damage, game.gold, game.vision_score) == (None, None, None, None)
    only_jungle = parse_ranked_game(payload(neutralMinionsKilled=40), "p")
    assert only_jungle is not None
    assert only_jungle.cs == 40
    junk = parse_ranked_game(payload(goldEarned="lots", visionScore=True), "p")
    assert junk is not None
    assert (junk.gold, junk.vision_score) == (None, None)


def test_a_malformed_match_is_still_an_error():
    with pytest.raises(RiotUnavailable):
        parse_ranked_game({"info": {"queueId": 420, "participants": [{"puuid": "p"}]}}, "p")


# --- pure view model and formatting ----------


def test_result_champion_role_and_kda():
    entry = to_entry(stored())
    assert (entry.result, entry.champion, entry.role) == ("win", "Ahri", "MID")
    assert entry.kda == pytest.approx(8.0)
    assert entry.cs_per_min == pytest.approx(192 / 31)
    assert to_entry(stored(win=False)).result == "loss"


def test_remakes_and_deathless_games():
    assert to_entry(stored(duration_s=200)).result == "remake"
    assert to_entry(stored(early_surrender=True)).result == "remake"
    perfect = to_entry(stored(kills=5, deaths=0, assists=5))
    assert perfect.kda == pytest.approx(10.0)  # deaths floored at 1, never a division error


def test_unknown_role_and_missing_stats_degrade_gracefully():
    entry = to_entry(stored(team_position="", cs=None, champion=None))
    assert entry.role is None
    assert entry.champion == "?"
    assert entry.cs_per_min is None
    line = format_line(entry, NOW)
    assert "—" in line  # unknown role
    assert "CS" not in line  # nothing invented


def test_format_line_contains_the_requested_details():
    line = format_line(to_entry(stored()), NOW)
    assert line.startswith("✅ **Ahri**")
    for part in ("Mid", "7/2/9 (8.0 KDA)", "192 CS (6.2/min)", "31 min", "há 2 d"):
        assert part in line
    assert "Alpha#EUW" not in line
    assert "*Alpha#EUW*" in format_line(to_entry(stored()), NOW, show_account=True)
    assert format_line(to_entry(stored(win=False)), NOW).startswith("❌")
    assert format_line(to_entry(stored(duration_s=200)), NOW).startswith("🔁")


def test_details_are_available_for_later_restyling():
    plain = format_line(to_entry(stored()), NOW)
    detailed = format_line(to_entry(stored()), NOW, details=True)
    assert detailed.startswith(plain)
    assert "24.1k dano" in detailed
    assert "12.3k ouro" in detailed
    assert "visão 25" in detailed
    assert "\n" not in plain


def test_helpers():
    assert format_duration(1860) == "31 min"
    assert format_count(999) == "999"
    assert format_count(12_345) == "12.3k"
    assert format_ago(NOW - timedelta(minutes=5), NOW) == "há 5 min"
    assert format_ago(NOW - timedelta(hours=7), NOW) == "há 7 h"
    assert format_ago(NOW - timedelta(days=3, hours=2), NOW) == "há 3 d"
    assert format_ago(NOW + timedelta(minutes=5), NOW) == "há 0 min"  # clock skew never negative


def test_summary_ignores_remakes_for_kda_but_counts_them():
    entries = [
        to_entry(stored(kills=10, deaths=2, assists=10)),
        to_entry(stored(win=False, kills=2, deaths=6, assists=4)),
        to_entry(stored(duration_s=190, kills=0, deaths=9, assists=0)),
    ]
    summary = summarize(entries)
    assert (summary.games, summary.wins, summary.losses, summary.remakes) == (3, 1, 1, 1)
    assert summary.kda == pytest.approx((10 + 10 + 2 + 4) / (2 + 6))
    text = format_summary(summary)
    assert "1V 1D" in text
    assert "1 remake" in text
    assert "3.25" in text
    assert summarize([]).kda == 0


# --- storage and service ----------


@pytest.fixture
def world() -> FakeRiot:
    w = FakeRiot()
    w.add_account("Alpha", "EUW", "puuid-alpha")
    w.add_account("Beta", "EUW", "puuid-beta")
    return w


@pytest_asyncio.fixture
async def stack(db, world) -> Stack:
    s = Stack(db, world)
    await s.register(1, "Ana")
    await s.register(2, "Rui")
    return s


async def test_sync_stores_the_extra_stats_and_history_returns_newest_first(stack, world):
    for i in range(12):
        world.add_game(
            "puuid-alpha",
            f"A_{i}",
            "MIDDLE",
            days_ago(NOW, i + 1),
            champion=f"Champ{i}",
            win=i % 2 == 0,
            kda=(i, 1, 2),
            stats=STATS,
        )
    await stack.riot.link(1, "Alpha#EUW", None, 99)
    await stack.riot.wait_for_background()
    view = await stack.riot.recent_history(1)
    assert len(view.games) == 10  # the last ten, not all twelve
    assert [g.champion for g in view.games][:3] == ["Champ0", "Champ1", "Champ2"]  # newest first
    assert view.games[0].win is True
    assert view.games[1].win is False
    assert (view.games[0].cs, view.games[0].damage, view.games[0].gold) == (192, 24_130, 12_345)
    assert view.last_synced == NOW
    assert not view.multiple_accounts
    assert view.nickname == "Ana"
    assert len((await stack.riot.recent_history(1, limit=3)).games) == 3


async def test_history_merges_accounts_and_can_be_limited_to_one(stack, world):
    world.add_game("puuid-alpha", "A_new", "TOP", days_ago(NOW, 1), champion="Garen")
    world.add_game("puuid-beta", "B_mid", "MIDDLE", days_ago(NOW, 2), champion="Zed")
    world.add_game("puuid-alpha", "A_old", "TOP", days_ago(NOW, 3), champion="Darius")
    await stack.riot.link(1, "Alpha#EUW", None, 99)
    await stack.riot.link(1, "Beta#EUW", None, 99)
    await stack.riot.wait_for_background()
    merged = await stack.riot.recent_history(1)
    assert [g.champion for g in merged.games] == ["Garen", "Zed", "Darius"]
    assert [g.riot_id for g in merged.games] == ["Alpha#EUW", "Beta#EUW", "Alpha#EUW"]
    assert merged.multiple_accounts
    only_beta = await stack.riot.recent_history(1, riot_id="beta#euw")
    assert [g.champion for g in only_beta.games] == ["Zed"]
    assert only_beta.account is not None
    assert not only_beta.multiple_accounts


async def test_history_error_messages(stack, world):
    with pytest.raises(PlayerError, match="não tem contas"):
        await stack.riot.recent_history(1)
    with pytest.raises(PlayerError, match="não está registado"):
        await stack.riot.recent_history(404)
    await stack.riot.link(1, "Alpha#EUW", None, 99)
    await stack.riot.wait_for_background()  # synced, but there are no games
    with pytest.raises(PlayerError, match="Não há jogos ranked"):
        await stack.riot.recent_history(1)
    with pytest.raises(PlayerError, match="não está ligada"):
        await stack.riot.recent_history(1, riot_id="Beta#EUW")
    # An account whose first sync has not finished says so instead of "no games".
    await stack.db.conn.execute("UPDATE riot_accounts SET history_synced_at = NULL")
    await stack.db.conn.commit()
    with pytest.raises(PlayerError, match="a sincronizar"):
        await stack.riot.recent_history(1)


async def test_history_needs_no_key_and_makes_no_requests(db, world):
    on = Stack(db, world)
    await on.register(1, "Ana")
    world.add_game("puuid-alpha", "A_1", "MIDDLE", days_ago(NOW, 1))
    await on.riot.link(1, "Alpha#EUW", None, 99)
    await on.riot.wait_for_background()
    off = Stack(db, world, enabled=False)
    calls = len(world.calls)
    view = await off.riot.recent_history(1)
    assert len(view.games) == 1
    assert len(world.calls) == calls


# --- the command ----------


@pytest_asyncio.fixture
async def cog(stack):
    from types import SimpleNamespace

    return Riot(SimpleNamespace(settings=load_settings(ENV)), stack.riot)  # type: ignore[arg-type]


async def test_history_command_is_public_and_shows_ten_games(stack, world, cog):
    for i in range(14):
        world.add_game(
            "puuid-alpha",
            f"A_{i}",
            "BOTTOM",
            days_ago(NOW, i * 0.1),
            champion="Jinx",
            win=i < 6,
            kda=(8, 2, 6),
            stats=STATS,
        )
    await stack.riot.link(1, "Alpha#EUW", None, 99)
    await stack.riot.wait_for_background()
    ix = FakeInteraction(7)  # someone else in the server looks
    await cog.history.callback(cog, ix, member(1), None)  # type: ignore[arg-type]
    reply = ix.sent[0]
    assert "ephemeral" not in reply  # the group can see it
    embed = reply["embed"]
    assert embed.title == "Últimos 10 jogos ranked Solo/Duo de Ana"
    lines = embed.description.split("\n\n")[1].split("\n")
    assert len(lines) == 10
    assert all("**Jinx**" in line and "ADC" in line and "8/2/6 (7.0 KDA)" in line for line in lines)
    assert embed.description.startswith("**6V 4D**")
    assert "Guardado no último sync" in embed.footer.text
    assert "/riot refresh" in embed.footer.text


async def test_history_command_defaults_to_yourself_and_reports_problems_privately(stack, cog):
    ix = FakeInteraction(1)
    await cog.history.callback(cog, ix, None, None)  # type: ignore[arg-type]
    assert ix.sent[0]["ephemeral"] is True
    assert "não tem contas" in ix.sent[0]["content"]
    ix = FakeInteraction(1)
    await cog.history.callback(cog, ix, member(404), None)  # type: ignore[arg-type]
    assert "não está registado" in ix.sent[0]["content"]


async def test_history_embed_shows_the_account_when_games_come_from_several(stack, world):
    world.add_game("puuid-alpha", "A_1", "TOP", days_ago(NOW, 1))
    world.add_game("puuid-beta", "B_1", "TOP", days_ago(NOW, 2))
    await stack.riot.link(1, "Alpha#EUW", None, 99)
    await stack.riot.link(1, "Beta#EUW", None, 99)
    await stack.riot.wait_for_background()
    both = build_history_embed(await stack.riot.recent_history(1), NOW)
    assert "*Alpha#EUW*" in (both.description or "")
    assert "*Beta#EUW*" in (both.description or "")
    one = build_history_embed(await stack.riot.recent_history(1, riot_id="Beta#EUW"), NOW)
    assert "(Beta#EUW)" in (one.title or "")
    assert "*Alpha#EUW*" not in (one.description or "")


async def test_history_autocomplete_is_open_to_everyone_unlike_management(stack, cog):
    from types import SimpleNamespace

    await stack.riot.link(1, "Alpha#EUW", None, 99)
    await stack.riot.link(1, "Beta#EUW", None, 99)
    await stack.riot.wait_for_background()
    ix = FakeInteraction(2)  # a non-admin looking at someone else's accounts
    ix.namespace = SimpleNamespace(player=member(1))
    names = [c.name for c in await cog._history_autocomplete(ix, "")]  # type: ignore[arg-type]
    assert names == ["Alpha#EUW", "Beta#EUW"]
    assert await cog._account_autocomplete(ix, "") == []  # type: ignore[arg-type]  # still private


def test_the_consent_notice_says_history_is_visible_to_the_group():
    assert "/riot history" in CONSENT_NOTICE
    assert "visíveis a todos" in CONSENT_NOTICE


# --- migration 4 on a database that already has games ----------


async def test_migration_4_keeps_existing_games_and_adds_null_stats(tmp_path):
    path = tmp_path / "kdbot.sqlite3"
    async with aiosqlite.connect(path) as raw:
        for i, script in enumerate(MIGRATIONS[:3], start=1):
            await raw.executescript(f"BEGIN;{script};PRAGMA user_version = {i};COMMIT;")
        await raw.execute(
            "INSERT INTO players (id, discord_id, nickname, created_at) VALUES (1, 1, 'A', 'x')"
        )
        await raw.execute(
            "INSERT INTO riot_accounts (id, player_id, puuid, game_name, tag_line, platform,"
            " linked_by, linked_at) VALUES (1, 1, 'p', 'A', 'EUW', 'euw1', 1, 'x')"
        )
        await raw.execute(
            "INSERT INTO riot_role_games VALUES (1, 'M1', '2026-01-01T00:00:00+00:00', 'TOP',"
            " 'TOP', 1, 1, 1, 1, 1800, 0, 'Garen')"
        )
        await raw.commit()
    database = Database(path)
    await database.connect()
    try:
        async with database.conn.execute(
            "SELECT champion, cs, damage, gold, vision_score FROM riot_role_games"
        ) as cur:
            assert list(await cur.fetchall()) == [("Garen", None, None, None, None)]
    finally:
        await database.close()
    assert len(MIGRATIONS) >= 4


def test_sqlite_has_the_window_functions_the_history_queries_rely_on():
    # recent_games/observations use ROW_NUMBER() (SQLite 3.25+).
    assert sqlite3.sqlite_version_info >= (3, 25)
