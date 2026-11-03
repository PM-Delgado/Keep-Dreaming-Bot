"""`/riot duo`: the embeds (`duo_render`), the command, the tab buttons and their registration."""

import re
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from riot_fakes import NOW, FakeRiot, Stack, days_ago
from test_pair_avatar import RED, YELLOW, avatars_of, decode, fake_guild
from test_riot_cog import ENV
from test_riot_duo import day, duo_game, row, stats_of

from bot.config import load_settings
from bot.features.players.service import NOT_REGISTERED
from bot.features.riot.cog import Riot
from bot.features.riot.duo import MIN_COMPARE, build_duo
from bot.features.riot.duo_render import (
    NOTHING_SHARED,
    TAB_GAMES,
    TAB_RECORDS,
    TAB_SUMMARY,
    TABS,
    build_duo_embed,
    champion_ids,
)
from bot.features.riot.duo_views import DUO_ID, DuoTabButton, duo_view
from bot.features.riot.service import SAME_PLAYER

ADMIN = 99
ROLE_ICONS = {"ADC": "<:r_adc:1>", "SUPPORT": "<:r_sup:2>"}
CHAMPION_ICONS = {"Jinx": "<:c_jinx:3>", "Thresh": "<:c_thresh:4>"}
NAMES = {"Jinx": "Jinx", "Thresh": "Thresh", "Kaisa": "Kai'Sa"}


def embed_of(stats, tab=TAB_SUMMARY, a="Ana", b="Rui", roles=None, icons=None, names=None):
    return build_duo_embed(
        a, b, stats, tab, NOW, roles or {}, icons or {}, names or NAMES, "https://cdn.invalid/a.png"
    )


def text_of(stats, tab=TAB_SUMMARY, **kwargs) -> str:
    return embed_of(stats, tab, **kwargs).description or ""


def shared(n: int, wins: int | None = None, start: int = 0):
    """`n` games a day apart, the first `wins` of them won (default: all)."""
    wins = n if wins is None else wins
    return [duo_game(f"m{start + i}", day(start + i), win=i < wins) for i in range(n)]


# --- the summary tab --------------------------------------------------------------------------


def test_the_summary_headline_record_and_streaks():
    text = text_of(stats_of(shared(8, wins=6)))
    assert text.startswith("# 🤝 Duo · Ana + Rui")
    assert "Jogos: **8** | 6V 2D (75%)" in text
    assert "Sequência atual: 💀 **2 derrotas** · Maior: 6" in text  # wins first, then 2 losses


def test_a_short_current_streak_is_left_out_and_the_longest_stands_alone():
    games = [*shared(3), duo_game("l", day(5), win=False)]
    text = text_of(stats_of(games))
    assert "Sequência atual" not in text
    assert "Maior sequência de vitórias: 3" in text
    assert "Maior sequência" not in text_of(stats_of([duo_game("w", day(0)), *shared(1, 0, 1)]))


def test_a_streak_inside_one_day_shows_one_date():
    games = [duo_game(f"l{i}", day(0) + timedelta(hours=i), win=False) for i in range(2)]
    assert "💀 Maior sequência de derrotas: **2** (01/09/2026)" in text_of(
        stats_of(games), TAB_RECORDS
    )


def test_a_current_winning_streak_is_a_fire():
    games = [duo_game("a", day(0), win=False), *shared(3, start=1)]
    assert "Sequência atual: 🔥 **3 vitórias** · Maior: 3" in text_of(stats_of(games))


def test_percentages_wait_for_a_minimum_sample():
    text = text_of(stats_of(shared(4, wins=3)))
    headline = next(line for line in text.split("\n") if line.startswith("Jogos:"))
    assert headline == "Jogos: **4** | 3V 1D"  # no percentage
    assert "Amostra pequena: percentagens a partir de 5 jogos." in text


def test_role_and_champion_pairs_use_icons_when_there_are_some_and_names_when_not():
    stats = stats_of(shared(6, wins=4))
    plain = text_of(stats)
    assert "**Roles** (Ana + Rui)" in plain and "ADC + Suporte" in plain
    assert "**Jinx** + **Thresh** · 4V 2D (67%)" in plain
    icons = text_of(stats, roles=ROLE_ICONS, icons=CHAMPION_ICONS)
    assert "<:r_adc:1> + <:r_sup:2> · 4V 2D (67%)" in icons
    assert "<:c_jinx:3> + <:c_thresh:4> · 4V 2D (67%)" in icons


def test_only_the_three_most_played_pairs_are_listed():
    games = []
    for i, role in enumerate(("TOP", "JUNGLE", "MIDDLE", "BOTTOM")):
        games.append(duo_game(f"g{i}", day(i), roles=(role, "UTILITY")))
    pairs = [line for line in text_of(stats_of(games)).split("\n") if " + " in line and "·" in line]
    assert len([p for p in pairs if "Suporte" in p]) == 3


def test_kda_together_per_player():
    games = [duo_game("m1", day(0), kda_a=(10, 2, 6), kda_b=(2, 4, 12))]
    text = text_of(stats_of(games))
    assert "**Ana** | 10.0/2.0/6.0 (8.0)" in text and "**Rui** | 2.0/4.0/12.0 (3.5)" in text


def test_with_and_without_appears_only_when_both_sides_have_enough_games():
    with_games = shared(MIN_COMPARE)
    alone_a = [row(1, f"a{i}", day(i), team=100, win=i < 5) for i in range(MIN_COMPARE)]
    alone_b = [row(2, f"b{i}", day(i), team=100, win=i < 2) for i in range(MIN_COMPARE)]
    stats = build_duo(
        [g[0] for g in with_games] + alone_a, [g[1] for g in with_games] + alone_b, day(-1)
    )
    text = text_of(stats)
    assert "**Com e sem** (desde 31/08/2026)" in text  # the coverage date, in Lisbon time
    assert "**Ana**: 100% com Rui (10) · 50% sem (10)" in text
    assert "**Rui**: 100% com Ana (10) · 20% sem (10)" in text
    assert "indicativa" not in text  # no disclaimer under the numbers
    few = text_of(stats_of(shared(MIN_COMPARE), coverage=day(-1)))
    assert "Com e sem" not in few  # nobody has "without" games


def test_there_is_no_footer_unless_something_is_wrong_with_the_data():
    games = shared(3)
    for tab in (TAB_SUMMARY, TAB_RECORDS):
        text = text_of(stats_of(games, coverage=day(1)), tab)
        for gone in ("Baseado em", "não prova", "Histórico completo", "indicativa"):
            assert gone not in text
        assert not text.endswith("\n")  # nothing trails the numbers
    syncing = text_of(stats_of(games, syncing=("Beta#EUW",)))
    assert "histórico da conta Beta#EUW ainda está a sincronizar" in syncing
    two = text_of(stats_of(games, syncing=("A#EUW", "B#EUW")))
    assert "histórico das contas A#EUW, B#EUW ainda está a sincronizar" in two
    ambiguous = build_duo(
        [g[0] for g in games] + [row(1, "x", day(9), team=100, account=11)] * 2,
        [g[1] for g in games] + [row(2, "x", day(9), team=100)],
    )
    assert "1 jogo ignorado: duas contas do mesmo jogador no mesmo jogo." in text_of(ambiguous)


def test_nicknames_are_escaped():
    assert "# 🤝 Duo · a\\*b + c\\_d" in text_of(stats_of(shared(1)), a="a*b", b="c_d")


# --- the games and records tabs ---------------------------------------------------------------


def test_the_games_tab_has_two_rows_per_game_and_names_other_accounts():
    stats = stats_of(shared(12, wins=9))
    text = text_of(stats, TAB_GAMES, roles=ROLE_ICONS, icons=CHAMPION_ICONS)
    assert "Últimos 10 de 12 jogos:" in text
    assert text.count("✅") + text.count("❌") == 10
    assert (
        "✅ <:r_adc:1> <:c_jinx:3> 5/2/7 + <:r_sup:2> <:c_thresh:4> 1/4/15\n-# 30 min · há" in text
    )
    smurf = replace(duo_game("s", day(0))[0], riot_id="Smurf#EUW", is_main=False)
    other = duo_game("s", day(0))[1]
    text = text_of(build_duo([smurf], [other]), TAB_GAMES)
    assert "· Smurf#EUW" in text  # a game on a non-main account says which


def test_a_short_list_is_just_counted():
    assert "1 jogo:" in text_of(stats_of(shared(1)), TAB_GAMES)
    assert "3 jogos:" in text_of(stats_of(shared(3)), TAB_GAMES)


def test_a_missing_role_or_champion_does_not_break_a_line():
    blank = (
        row(1, "m", day(0), team=100, position=None, champion=None),
        row(2, "m", day(0), team=100, position=None, champion=None),
    )
    assert "✅ — ? 5/2/7 + — ? 5/2/7" in text_of(stats_of([blank]), TAB_GAMES)


def test_the_records_tab():
    games = [
        *shared(3, start=0),
        duo_game("l1", day(10), win=False),
        duo_game("l2", day(11), win=False),
        duo_game("big", day(20), kda_a=(15, 0, 9), kda_b=(0, 9, 3)),
        *[duo_game(f"k{i}", day(30 + i), roles=("TOP", "JUNGLE")) for i in range(5)],
    ]
    text = text_of(stats_of(games), TAB_RECORDS)
    assert text.startswith("# 🏆 Ana & Rui · Recordes")
    assert "🔥 Maior sequência de vitórias: **6**" in text  # big + 5 TOP/JUNGLE games
    assert "💀 Maior sequência de derrotas: **2** (11/09/2026 – 12/09/2026)" in text
    assert "⭐ Melhor combinação de roles: Top + Jungle · 5V 0D (100%) (mín. 5 jogos)" in text
    assert "🃏 Melhor combinação de campeões: **Jinx** + **Thresh**" in text
    assert "🎯 Melhor jogo de **Ana**: **Jinx** 15/0/9 (24.0)" in text
    assert "🎯 Melhor jogo de **Rui**: **Thresh**" in text
    assert "📅 Mais jogos num dia: **1**" in text
    assert "🗓️ Primeiro jogo guardado juntos: 01/09/2026" in text


def test_a_record_that_needs_more_games_is_not_invented():
    text = text_of(stats_of(shared(2)), TAB_RECORDS)
    assert "⭐" not in text and "🃏" not in text and "💀" not in text
    assert "🔥 Maior sequência de vitórias: **2**" in text


def test_no_shared_game_says_so_on_every_tab_and_keeps_the_data_notes():
    empty = build_duo(
        [row(1, "x", day(0), team=100)], [row(2, "y", day(0), team=100)], syncing=("Beta#EUW",)
    )
    for tab in TABS:
        text = text_of(empty, tab)
        assert NOTHING_SHARED in text and "ainda está a sincronizar" in text
    assert "-#" not in text_of(build_duo([], []))  # nothing to warn about: just the answer


def test_an_unknown_tab_shows_the_summary():
    assert text_of(stats_of(shared(2)), "nope").startswith("# 🤝 Duo")


def test_the_longest_tab_fits_in_an_embed_description():
    games = [
        duo_game(
            f"m{i}", day(i), champions=("MissFortune", "TahmKench"), roles=("BOTTOM", "UTILITY")
        )
        for i in range(30)
    ]
    long_name = "N" * 32
    for tab in TABS:
        assert len(text_of(stats_of(games), tab, a=long_name, b=long_name)) < 4096


def test_the_champions_to_look_up_cover_every_tab():
    ids = champion_ids(stats_of([duo_game("m", day(0), champions=("Jinx", "Thresh"))]))
    assert set(ids) == {"Jinx", "Thresh"}


# --- the command and the buttons --------------------------------------------------------------


class FakeInteraction:
    def __init__(self, user_id: int, members: dict | None = None):
        self.user = SimpleNamespace(id=user_id)
        self.guild = SimpleNamespace(get_member=(members or {}).get) if members else None
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        outer = self

        class Response:
            async def send_message(self, content=None, **kwargs):
                outer.sent.append({"content": content, **kwargs})

            async def edit_message(self, **kwargs):
                outer.edited.append(kwargs)

        self.response = Response()


def member(discord_id: int, name: str = "X") -> SimpleNamespace:
    return SimpleNamespace(
        id=discord_id,
        display_name=name,
        display_avatar=SimpleNamespace(url="https://cdn.invalid/x"),
    )


class FakeChampions:
    async def names(self, ids):
        return {c: c for c in ids}

    async def icon_markup(self, ids):
        return {c: f"<:champ_{c}:9>" for c in ids}


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
    await s.register(3, "Eva")
    return s


@pytest.fixture
def cog(stack) -> Riot:
    bot = SimpleNamespace(settings=load_settings(ENV), get_guild=lambda _id: None)
    return Riot(bot, stack.riot, None, FakeChampions())  # type: ignore[arg-type]


async def play(stack: Stack, world: FakeRiot, games: int = 3) -> None:
    alpha = (await stack.riot.link(1, "Alpha#EUW", None, ADMIN)).account
    beta = (await stack.riot.link(2, "Beta#EUW", None, ADMIN)).account
    await stack.riot.wait_for_background()
    for i in range(games):
        when = days_ago(NOW, i + 1)
        world.add_game("puuid-alpha", f"S{i}", "BOTTOM", when, champion="Jinx", team=100)
        world.add_game("puuid-beta", f"S{i}", "UTILITY", when, champion="Thresh", team=100)
    await stack.riot.sync_history(alpha)
    await stack.riot.sync_history(beta)


async def test_duo_defaults_to_the_caller_and_replies_publicly_with_three_tabs(stack, world, cog):
    await play(stack, world)
    ix = FakeInteraction(1)
    await cog.duo.callback(cog, ix, member(2))  # type: ignore[arg-type]
    reply = ix.sent[0]
    assert "ephemeral" not in reply and reply["allowed_mentions"].users is False
    assert "Ana + Rui" in (reply["embed"].description or "")
    assert "<:champ_Jinx:9>" in (reply["embed"].description or "")
    ids = [b.custom_id for b in reply["view"].children]
    assert [i.split(":")[1] for i in ids] == list(TABS) and ids[0].endswith(":1")
    assert sum(b.item.disabled for b in reply["view"].children) == 1  # the open tab


async def test_duo_compares_two_other_players_when_both_are_given(stack, world, cog):
    await play(stack, world)
    ix = FakeInteraction(3)
    await cog.duo.callback(cog, ix, member(1), member(2))  # type: ignore[arg-type]
    assert "Ana + Rui" in ix.sent[0]["embed"].description
    assert ix.sent[0]["view"].children[0].custom_id.endswith(":3")  # owner = the caller


async def test_duo_without_a_shared_game_has_no_buttons(stack, world, cog):
    alpha = (await stack.riot.link(1, "Alpha#EUW", None, ADMIN)).account
    await stack.riot.link(2, "Beta#EUW", None, ADMIN)
    await stack.riot.wait_for_background()
    await stack.riot.sync_history(alpha)
    ix = FakeInteraction(1)
    await cog.duo.callback(cog, ix, member(2))  # type: ignore[arg-type]
    assert NOTHING_SHARED in ix.sent[0]["embed"].description and "view" not in ix.sent[0]


async def test_duo_asks_riot_for_nothing(stack, world, cog):
    await play(stack, world)
    world.calls.clear()
    await cog.duo.callback(cog, FakeInteraction(1), member(2))  # type: ignore[arg-type]
    assert world.calls == []


async def test_duo_refuses_privately_and_names_who_is_missing(stack, world, cog):
    ix = FakeInteraction(1)
    await cog.duo.callback(cog, ix, member(1))  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == SAME_PLAYER and ix.sent[0]["ephemeral"] is True
    ix = FakeInteraction(404)
    await cog.duo.callback(cog, ix, member(2))  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == NOT_REGISTERED
    ix = FakeInteraction(1, members={404: member(404, "Rui *")})
    await cog.duo.callback(cog, ix, member(404, "Rui *"))  # type: ignore[arg-type]
    assert ix.sent[0]["content"] == "KD Scrims: **Rui \\*** não está registado."
    ix = FakeInteraction(1)
    await cog.duo.callback(cog, ix, member(2))  # type: ignore[arg-type]
    assert "não tem contas Riot ligadas" in ix.sent[0]["content"]
    assert ix.sent[0]["ephemeral"] is True


async def test_the_owner_flips_the_public_message_anybody_else_gets_a_private_copy(
    stack, world, cog
):
    await play(stack, world)
    owner = FakeInteraction(1)
    await cog.flip_duo(owner, TAB_GAMES, 1, 2, 1)  # type: ignore[arg-type]
    assert not owner.sent and "Jogos" in owner.edited[0]["embed"].description
    assert owner.edited[0]["view"].children[1].item.disabled  # Jogos is now the open tab
    guest = FakeInteraction(5)
    await cog.flip_duo(guest, TAB_RECORDS, 1, 2, 1)  # type: ignore[arg-type]
    assert not guest.edited and guest.sent[0]["ephemeral"] is True
    assert guest.sent[0]["view"].children[0].custom_id.endswith(":5")  # their own buttons


async def test_a_click_after_an_unlink_is_refused_privately(stack, world, cog):
    await play(stack, world)
    await stack.riot.unlink(2, "Beta#EUW")
    ix = FakeInteraction(1)
    await cog.flip_duo(ix, TAB_SUMMARY, 1, 2, 1)  # type: ignore[arg-type]
    assert not ix.edited and ix.sent[0]["ephemeral"] is True
    assert "não tem contas Riot ligadas" in ix.sent[0]["content"]


def test_the_button_ids_round_trip_and_fit_discord_limits():
    view = duo_view(TAB_GAMES, 2**62, 2**62 + 1, 2**62 + 2)
    ids = [button.custom_id for button in view.children]  # type: ignore[attr-defined]
    assert all(len(i) <= 100 for i in ids)
    parsed = [re.fullmatch(DUO_ID, i) for i in ids]
    assert all(parsed) and [m["tab"] for m in parsed if m] == list(TABS)
    button = view.children[1]
    assert isinstance(button, DuoTabButton) and button.item.disabled and button.tab == TAB_GAMES
    assert view.timeout is None


async def test_a_button_from_an_old_message_is_rebuilt_from_its_id():
    match = re.fullmatch(DUO_ID, "duo:records:11:22:33")
    assert match is not None
    button = await DuoTabButton.from_custom_id(None, None, match)  # type: ignore[arg-type]
    assert (button.tab, button.a, button.b, button.owner) == (TAB_RECORDS, 11, 22, 33)
    assert not button.item.disabled  # only the tab being shown is disabled


async def test_a_button_click_goes_to_the_riot_cog(cog):
    from discord.ext import commands

    flipped: list[tuple] = []

    async def flip_duo(*args):
        flipped.append(args)

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.get_cog = lambda name: SimpleNamespace(flip_duo=flip_duo) if name == "Riot" else None  # type: ignore[method-assign]
    ix = SimpleNamespace(client=bot)
    await DuoTabButton(TAB_GAMES, 1, 2, 3).callback(ix)  # type: ignore[arg-type]
    assert flipped == [(ix, TAB_GAMES, 1, 2, 3)]


async def test_the_cog_registers_the_tab_buttons(cog):
    class Bot:
        def __init__(self):
            self.items: list = []
            self.views: list = []
            self.settings = load_settings(ENV)

        def add_dynamic_items(self, *items):
            self.items.extend(items)

        def remove_dynamic_items(self, *items):
            self.items = [i for i in self.items if i not in items]

        def add_view(self, view):
            self.views.append(view)

    bot = Bot()
    cog.bot = bot  # type: ignore[assignment]
    await cog.cog_load()
    assert DuoTabButton in bot.items
    await cog.cog_unload()
    assert DuoTabButton not in bot.items


# --- the half-and-half picture on the message -------------------------------------------------


def guild_cog(stack: Stack, avatars: dict) -> Riot:
    """A cog whose server knows only these members (the rest are 'unknown members')."""
    bot = SimpleNamespace(settings=load_settings(ENV), get_guild=lambda _id: fake_guild(avatars))
    return Riot(bot, stack.riot, None, FakeChampions())  # type: ignore[arg-type]


async def test_the_duo_message_carries_the_half_and_half_picture(stack, world):
    await play(stack, world)
    cog = guild_cog(stack, avatars_of([]))
    ix = FakeInteraction(1)
    await cog.duo.callback(cog, ix, member(2))  # type: ignore[arg-type]
    reply = ix.sent[0]
    assert reply["embed"].thumbnail.url == "attachment://pair.png"
    assert reply["file"].filename == "pair.png"
    picture = decode(reply["file"].fp.read())
    assert picture.getpixel((10, 128)) == RED and picture.getpixel((246, 128)) == YELLOW


async def test_a_flip_attaches_the_picture_again_or_clears_a_stale_one(stack, world):
    await play(stack, world)
    cog = guild_cog(stack, avatars_of([]))
    owner = FakeInteraction(1)
    await cog.flip_duo(owner, TAB_GAMES, 1, 2, 1)  # type: ignore[arg-type]
    assert owner.edited[0]["embed"].thumbnail.url == "attachment://pair.png"
    assert [f.filename for f in owner.edited[0]["attachments"]] == ["pair.png"]
    guest = FakeInteraction(5)
    await cog.flip_duo(guest, TAB_GAMES, 1, 2, 1)  # type: ignore[arg-type]
    assert guest.sent[0]["file"].filename == "pair.png" and not guest.edited
    nobody = guild_cog(stack, {})
    again = FakeInteraction(1)
    await nobody.flip_duo(again, TAB_GAMES, 1, 2, 1)  # type: ignore[arg-type]
    assert again.edited[0]["attachments"] == []  # no stale picture left on the message


async def test_without_reachable_members_the_message_has_no_picture_at_all(stack, world):
    await play(stack, world)
    cog = guild_cog(stack, {})
    ix = FakeInteraction(1)
    await cog.duo.callback(cog, ix, member(2))  # type: ignore[arg-type]
    assert ix.sent[0]["embed"].thumbnail.url is None and "file" not in ix.sent[0]
    assert "Ana + Rui" in ix.sent[0]["embed"].description  # the answer itself is unharmed
