"""The daily Solo/Duo leaderboard: LP maths, ordering and trend, pages, saved ranks, the command."""

from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from riot_fakes import NOW, FakeRiot, Stack
from test_riot_cog import ENV, FakeInteraction

from bot.config import ConfigError, load_settings
from bot.features.riot.client import SoloRank
from bot.features.riot.cog import NO_FEED_CHANNEL, NOT_ADMIN_POST, Riot
from bot.features.riot.leaderboard import (
    PAGE_SIZE,
    MainRank,
    build_embed,
    build_rows,
    next_page,
    page_count,
    page_of_footer,
    trend_text,
    updated_text,
)
from bot.features.riot.lp import ladder_points, lp_delta, rank_label
from bot.features.riot.views import LeaderboardView


def desc(embed: discord.Embed) -> str:
    return embed.description or ""


def foot(embed: discord.Embed) -> str:
    return embed.footer.text or ""


def main(account_id, nickname, tier, division, lp):
    return MainRank(account_id, nickname, f"{nickname}#EUW", tier, division, lp)


# --- LP maths ------------------------------------------------------------------------------


def test_ladder_points_are_a_straight_scale():
    assert ladder_points("IRON", "IV", 0) == 0
    assert ladder_points("GOLD", "II", 40) == ladder_points("GOLD", "II", 0) + 40
    assert ladder_points("DIAMOND", "I", 75) == 2775
    # Master, Grandmaster and Challenger share one scale above Diamond I.
    assert ladder_points("MASTER", None, 0) == 2800
    assert ladder_points("GRANDMASTER", None, 1804) == ladder_points("MASTER", None, 1804)


def test_unknown_tier_or_division_is_refused():
    with pytest.raises(ValueError):
        ladder_points("WOOD", "I", 0)
    with pytest.raises(ValueError):
        ladder_points("GOLD", None, 0)


def test_lp_delta_within_a_division_and_across_promotions():
    assert lp_delta(SoloRank("GOLD", "II", 40), SoloRank("GOLD", "II", 67)) == 27
    assert lp_delta(SoloRank("GOLD", "II", 40), SoloRank("GOLD", "II", 21)) == -19
    # Promotion: Gold II 90 LP -> Gold I 10 LP is +20, not -80.
    assert lp_delta(SoloRank("GOLD", "II", 90), SoloRank("GOLD", "I", 10)) == 20
    # Demotion the other way round.
    assert lp_delta(SoloRank("GOLD", "I", 10), SoloRank("GOLD", "II", 85)) == -25
    # Diamond I 90 LP -> Master 10 LP.
    assert lp_delta(SoloRank("DIAMOND", "I", 90), SoloRank("MASTER", None, 10)) == 20
    assert lp_delta(SoloRank("MASTER", None, 600), SoloRank("MASTER", None, 573)) == -27


def test_lp_delta_is_unknown_without_both_ranks():
    assert lp_delta(None, SoloRank("GOLD", "II", 1)) is None
    assert lp_delta(SoloRank("GOLD", "II", 1), None) is None


def test_rank_label_has_a_division_only_below_master():
    assert rank_label("DIAMOND", "I", 75) == "DIAMOND I · 75 LP"
    assert rank_label("grandmaster", None, 1804) == "GRANDMASTER · 1804 LP"
    assert rank_label("MASTER", "I", 790) == "MASTER · 790 LP"  # Riot's apex "I" is not shown


# --- rows, trend, pages --------------------------------------------------------------------


def test_rows_are_ordered_by_lp_then_unranked_by_name():
    rows = build_rows(
        [
            main(1, "Zed", None, None, None),
            main(2, "Mid", "MASTER", None, 100),
            main(3, "Top", "DIAMOND", "I", 75),
            main(4, "Ace", None, None, None),
            main(5, "Big", "GRANDMASTER", None, 1804),
        ],
        {},
    )
    assert [r.main.nickname for r in rows] == ["Big", "Mid", "Top", "Ace", "Zed"]
    assert [r.position for r in rows] == [1, 2, 3, 4, 5]


def test_equal_lp_puts_the_higher_tier_first():
    rows = build_rows([main(1, "A", "MASTER", None, 0), main(2, "B", "GRANDMASTER", None, 0)], {})
    assert [r.main.nickname for r in rows] == ["B", "A"]


def test_trend_is_measured_against_the_previous_snapshot():
    rows = build_rows(
        [
            main(1, "Up", "GOLD", "II", 60),
            main(2, "Down", "GOLD", "II", 30),
            main(3, "New", "GOLD", "II", 0),
        ],
        {1: ladder_points("GOLD", "II", 15), 2: ladder_points("GOLD", "II", 50)},
    )
    by_name = {r.main.nickname: r.trend for r in rows}
    assert by_name == {"Up": 45, "Down": -20, "New": None}
    assert [trend_text(t) for t in (45, -20, 0, None)] == ["▲ +45", "▼ -20", "—", "—"]


def test_an_unknown_tier_is_listed_as_unranked_instead_of_failing():
    rows = build_rows([main(1, "Odd", "WOOD", "I", 5), main(2, "Ok", "GOLD", "I", 5)], {})
    assert [r.main.nickname for r in rows] == ["Ok", "Odd"]


def test_pages_hold_ten_rows_and_wrap_around():
    mains = [main(i, f"P{i:02d}", "GOLD", "II", 100 - i) for i in range(1, 19)]
    rows = build_rows(mains, {})
    assert page_count(rows) == 2 and page_count([]) == 1
    first = build_embed(rows, 1, NOW, NOW)
    second = build_embed(rows, 2, NOW, NOW)
    assert desc(first).count("\n") == PAGE_SIZE - 1
    assert "P01" in desc(first) and "P11" in desc(second)
    assert desc(second).count("\n") == 7  # 8 rows
    assert next_page(2, 2, 1) == 1 and next_page(1, 2, -1) == 2 and next_page(1, 3, 1) == 2
    assert page_of_footer(foot(second)) == 2
    assert page_of_footer(None) == 1 and page_of_footer("something else") == 1
    assert foot(build_embed(rows, 99, NOW, NOW)).startswith("Página 2/2")  # clamped


def test_embed_text_has_the_agreed_layout_and_no_riot_disclaimer():
    rows = build_rows([main(1, "Exduardo", "MASTER", None, 1034)], {1: 2800 + 1007})
    embed = build_embed(rows, 1, NOW, NOW)
    assert embed.title == "🏆 Classificação Solo/Duo"
    assert embed.description == "**1** · Exduardo (Exduardo#EUW) · MASTER · 1034 LP · ▲ +27"
    assert embed.footer.text == "Página 1/1 · Atualizado hoje às 13:00 · ▲▼ = LP desde ontem"
    assert "Riot" not in embed.footer.text


def test_names_cannot_inject_markdown_and_unranked_say_so():
    rows = build_rows([main(1, "*bold*", None, None, None)], {})
    description = desc(build_embed(rows, 1, NOW, NOW))
    assert description.endswith("· Sem rank")
    assert "\\*bold\\*" in description


def test_updated_text_names_the_day_when_it_is_not_today():
    assert updated_text(NOW, NOW) == "hoje às 13:00"  # 12:00 UTC is 13:00 in Lisbon in June
    assert updated_text(NOW - timedelta(days=2), NOW) == "30/05 às 13:00"


def test_empty_leaderboard_says_so():
    assert "Ainda não há" in desc(build_embed([], 1, NOW, NOW))


# --- saved ranks (service) -----------------------------------------------------------------


@pytest.fixture
def world() -> FakeRiot:
    w = FakeRiot()
    for name in ("Alpha", "Beta", "Gamma"):
        w.add_account(name, "EUW", f"puuid-{name.lower()}")
    w.set_rank("puuid-alpha", "GOLD", "II", 40)
    w.set_rank("puuid-beta", "MASTER", "I", 120)
    w.set_rank("puuid-gamma", None)
    return w


@pytest_asyncio.fixture
async def stack(db, world) -> Stack:
    s = Stack(db, world)
    for discord_id, nickname, account in (
        (1, "Ana", "Alpha#EUW"),
        (2, "Rui", "Beta#EUW"),
        (3, "Ze", "Gamma#EUW"),
    ):
        await s.register(discord_id, nickname)
        await s.riot.link(discord_id, account, None, discord_id)
    await s.riot.wait_for_background()
    return s


async def test_refresh_saves_tier_division_and_lp_of_every_main(stack):
    assert await stack.riot.refresh_main_ranks() == 3
    rows, updated = await stack.riot.leaderboard()
    assert [(r.main.nickname, r.main.tier, r.main.division, r.main.lp) for r in rows] == [
        ("Rui", "MASTER", None, 120),
        ("Ana", "GOLD", "II", 40),
        ("Ze", None, None, None),  # confirmed unranked, listed last
    ]
    assert updated == NOW


async def test_only_main_accounts_of_players_still_in_the_group_are_listed(stack, world):
    world.add_account("Alt", "EUW", "puuid-alt")
    world.set_rank("puuid-alt", "CHALLENGER", None, 2000)
    await stack.riot.link(1, "Alt#EUW", None, 1)  # a second, non-main account of Ana
    await stack.db.conn.execute(
        "UPDATE players SET left_at = ? WHERE discord_id = 3", ("2026-05-01T00:00:00+00:00",)
    )
    await stack.db.conn.commit()
    await stack.riot.wait_for_background()
    await stack.riot.refresh_main_ranks()
    rows, _ = await stack.riot.leaderboard()
    assert [r.main.nickname for r in rows] == ["Rui", "Ana"]
    assert all("Alt" not in r.main.riot_id for r in rows)


async def test_a_failed_lookup_keeps_the_previous_rank(stack, world):
    await stack.riot.refresh_main_ranks()
    world.forced["/lol/league"] = 500
    assert await stack.riot.refresh_main_ranks() == 0
    rows, _ = await stack.riot.leaderboard()
    assert rows[0].main.lp == 120 and rows[1].main.lp == 40


async def test_a_rejected_key_stops_the_refresh(stack, world):
    world.forced["/lol/league"] = 401
    calls = world.count("/lol/league")
    assert await stack.riot.refresh_main_ranks() == 0
    assert world.count("/lol/league") == calls + 1  # it did not go on asking with a bad key


async def test_trend_comes_from_the_previous_days_snapshot(stack, world):
    await stack.riot.refresh_main_ranks()
    await stack.riot.save_daily_snapshot()
    rows, _ = await stack.riot.leaderboard()
    assert all(r.trend is None for r in rows)  # nothing earlier to compare with
    stack.riot.clock = lambda: NOW + timedelta(days=1)
    world.set_rank("puuid-alpha", "GOLD", "II", 67)
    world.set_rank("puuid-beta", "MASTER", "I", 100)
    await stack.riot.refresh_main_ranks()
    rows, _ = await stack.riot.leaderboard()
    assert {r.main.nickname: r.trend for r in rows} == {"Rui": -20, "Ana": 27, "Ze": None}
    await stack.riot.save_daily_snapshot()
    await stack.riot.save_daily_snapshot()  # the same day twice replaces, it does not duplicate
    count = await stack.rows("SELECT COUNT(*) FROM riot_rank_snapshots WHERE day = ?", "2026-06-02")
    assert count[0][0] == 3


async def test_stale_check_asks_riot_only_when_a_main_is_missing_or_old(stack, world):
    assert await stack.riot.leaderboard_stale() is True  # never looked up
    assert await stack.riot.refresh_ranks_for_view() is True
    calls = world.count("/lol/league")
    assert await stack.riot.leaderboard_stale() is False
    assert await stack.riot.refresh_ranks_for_view() is False
    assert world.count("/lol/league") == calls
    stack.riot.clock = lambda: NOW + timedelta(hours=2)
    assert await stack.riot.leaderboard_stale() is True


async def test_unlinking_removes_the_rank_and_snapshots(stack):
    await stack.riot.refresh_main_ranks()
    await stack.riot.save_daily_snapshot()
    await stack.riot.unlink(2, "Beta#EUW")
    assert (await stack.rows("SELECT COUNT(*) FROM riot_rank_snapshots"))[0][0] == 2
    rows, _ = await stack.riot.leaderboard()
    assert [r.main.nickname for r in rows] == ["Ana", "Ze"]


async def test_nothing_is_requested_without_a_key(db):
    s = Stack(db, FakeRiot(), enabled=False)
    assert await s.riot.leaderboard_stale() is False
    assert await s.riot.refresh_ranks_for_view() is False


# --- the command ---------------------------------------------------------------------------


class Channel(discord.abc.Messageable):
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def _get_channel(self):  # pragma: no cover - never used, send is overridden
        raise NotImplementedError

    async def send(self, **kwargs):  # type: ignore[override]
        self.sent.append(kwargs)


def make_cog(stack: Stack, channel: Channel | None = None, channel_id: int | None = 555) -> Riot:
    env = {**ENV, "RIOT_FEED_CHANNEL_ID": str(channel_id)} if channel_id else dict(ENV)
    bot = SimpleNamespace(settings=load_settings(env), get_channel=lambda _id: channel)
    return Riot(bot, stack.riot)  # type: ignore[arg-type]


async def test_leaderboard_command_asks_riot_first_when_ranks_are_missing(stack):
    cog = make_cog(stack)
    ix = FakeInteraction(5)
    await cog.leaderboard.callback(cog, ix, False)  # type: ignore[arg-type]
    assert ix.deferred  # Riot was asked, so the reply is a follow-up
    reply = ix.sent[0]
    assert "ephemeral" not in reply  # public
    assert reply["embed"].description.startswith("**1** · Rui (Beta#EUW) · MASTER · 120 LP")
    assert "view" not in reply  # three players: one page, no buttons


async def test_leaderboard_command_shows_the_saved_ranks_without_asking_riot(stack, world):
    await stack.riot.refresh_main_ranks()
    calls = len(world.calls)
    cog = make_cog(stack)
    ix = FakeInteraction(5)
    await cog.leaderboard.callback(cog, ix, False)  # type: ignore[arg-type]
    assert not ix.deferred and len(world.calls) == calls
    assert "Ana (Alpha#EUW) · GOLD II · 40 LP" in ix.sent[0]["embed"].description


async def test_many_players_get_the_page_buttons(stack):
    for i in range(10, 25):
        await stack.register(i, f"P{i}")
        await stack.db.conn.execute(
            "INSERT INTO riot_accounts (player_id, puuid, game_name, tag_line, platform, is_main,"
            " linked_by, linked_at, rank_tier, rank_division, rank_lp, rank_checked_at)"
            " SELECT id, ?, ?, 'EUW', 'euw1', 1, 1, ?, 'SILVER', 'I', 10, ?"
            " FROM players WHERE discord_id = ?",
            (f"puuid-{i}", f"P{i}", NOW.isoformat(), NOW.isoformat(), i),
        )
    await stack.db.conn.commit()
    await stack.riot.refresh_main_ranks()
    cog = make_cog(stack)
    ix = FakeInteraction(5)
    await cog.leaderboard.callback(cog, ix, False)  # type: ignore[arg-type]
    view = ix.sent[0]["view"]
    assert {c.custom_id for c in view.children} == {"riotlb:prev", "riotlb:next"}
    assert view.timeout is None  # persistent: it must outlive a restart
    assert ix.sent[0]["embed"].footer.text.startswith("Página 1/2")


async def test_the_buttons_edit_the_message_in_place_and_wrap_around(stack):
    for i in range(10, 25):
        await stack.register(i, f"P{i}")
        await stack.db.conn.execute(
            "INSERT INTO riot_accounts (player_id, puuid, game_name, tag_line, platform, is_main,"
            " linked_by, linked_at, rank_tier, rank_division, rank_lp, rank_checked_at)"
            " SELECT id, ?, ?, 'EUW', 'euw1', 1, 1, ?, 'SILVER', 'I', 10, ?"
            " FROM players WHERE discord_id = ?",
            (f"puuid-{i}", f"P{i}", NOW.isoformat(), NOW.isoformat(), i),
        )
    await stack.db.conn.commit()
    view = LeaderboardView(stack.riot)
    shown = {"footer": "Página 1/2 · Atualizado hoje às 13:00"}

    async def edit_message(embed, view):
        shown["footer"] = embed.footer.text

    def click():
        message = SimpleNamespace(
            embeds=[SimpleNamespace(footer=SimpleNamespace(text=shown["footer"]))]
        )
        return SimpleNamespace(message=message, response=SimpleNamespace(edit_message=edit_message))

    await view._flip(click(), 1)  # type: ignore[arg-type]
    assert shown["footer"].startswith("Página 2/2")
    await view._flip(click(), 1)  # type: ignore[arg-type]
    assert shown["footer"].startswith("Página 1/2")  # ▶ on the last page wraps to the first
    await view._flip(click(), -1)  # type: ignore[arg-type]
    assert shown["footer"].startswith("Página 2/2")  # ◀ on the first wraps to the last


async def test_only_admins_may_post_to_the_channel(stack):
    channel = Channel()
    cog = make_cog(stack, channel)
    ix = FakeInteraction(5)
    await cog.leaderboard.callback(cog, ix, True)  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == NOT_ADMIN_POST and ix.sent[0]["ephemeral"] is True
    assert channel.sent == []


async def test_an_admin_post_refreshes_and_posts_without_a_snapshot(stack):
    channel = Channel()
    cog = make_cog(stack, channel)
    ix = FakeInteraction(99)
    await cog.leaderboard.callback(cog, ix, True)  # type: ignore[arg-type]
    assert "publicado" in ix.sent[0]["content"] and ix.sent[0]["ephemeral"] is True
    posted = channel.sent[0]["embed"]
    assert "Rui (Beta#EUW)" in posted.description
    assert (await stack.rows("SELECT COUNT(*) FROM riot_rank_snapshots"))[0][0] == 0


async def test_the_daily_post_also_records_the_snapshot(stack):
    channel = Channel()
    cog = make_cog(stack, channel)
    assert await cog.post_leaderboard(snapshot=True) is True
    assert (await stack.rows("SELECT COUNT(*) FROM riot_rank_snapshots"))[0][0] == 3
    assert len(channel.sent) == 1


async def test_without_a_usable_channel_nothing_is_posted_and_the_admin_is_told(stack):
    cog = make_cog(stack, None, channel_id=None)
    assert await cog.post_leaderboard(snapshot=True) is False
    ix = FakeInteraction(99)
    await cog.leaderboard.callback(cog, ix, True)  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == NO_FEED_CHANNEL


async def test_a_discord_error_while_posting_is_survived(stack):
    class Broken(Channel):
        async def send(self, **kwargs):  # type: ignore[override]
            raise discord.HTTPException(
                SimpleNamespace(status=403, reason="no"),  # type: ignore[arg-type]
                "Missing Access",
            )

    cog = make_cog(stack, Broken())
    assert await cog.post_leaderboard(snapshot=False) is False


# --- settings ------------------------------------------------------------------------------


def test_feed_channel_is_an_optional_numeric_id():
    base = {"DISCORD_TOKEN": "t", "GUILD_ID": "1"}
    assert load_settings(base).riot_feed_channel_id is None
    assert load_settings({**base, "RIOT_FEED_CHANNEL_ID": " 123 "}).riot_feed_channel_id == 123
    with pytest.raises(ConfigError, match="RIOT_FEED_CHANNEL_ID"):
        load_settings({**base, "RIOT_FEED_CHANNEL_ID": "abc"})
