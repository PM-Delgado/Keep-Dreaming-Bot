from types import SimpleNamespace
from typing import Any

import discord
import pytest_asyncio
from riot_fakes import NOW, FakeRiot, Stack, days_ago

from bot.app import EXTENSIONS, KDBot
from bot.config import load_settings
from bot.features.players.profile_embed import build_profile_embed
from bot.features.ratings.ladder import DEFAULT_LADDER
from bot.features.riot.cog import NOT_ADMIN, Riot, build_list_embed, link_summary
from bot.features.riot.service import CONSENT_NOTICE, LinkResult

ENV = {
    "DISCORD_TOKEN": "t",
    "GUILD_ID": "123",
    "DB_PATH": ":memory:",
    "ADMIN_USER_IDS": "99",
}


class FakeInteraction:
    def __init__(self, user_id: int) -> None:
        self.user = SimpleNamespace(
            id=user_id, display_avatar=SimpleNamespace(url="https://cdn.invalid/a.png")
        )
        self.sent: list[dict] = []  # everything said, in order
        self._done = False
        self.namespace = SimpleNamespace()
        outer = self

        class Response:
            async def defer(self, ephemeral: bool = False, **_):
                outer._done = True

            async def send_message(self, content=None, **kwargs):
                outer._done = True
                outer.sent.append({"content": content, **kwargs})

            def is_done(self) -> bool:
                return outer._done

        class Followup:
            async def send(self, content=None, **kwargs):
                outer.sent.append({"content": content, **kwargs})

        self.response = Response()
        self.followup = Followup()


def member(user_id: int):
    return SimpleNamespace(
        id=user_id, display_avatar=SimpleNamespace(url="https://cdn.invalid/a.png")
    )


@pytest_asyncio.fixture
async def cog_stack(db):
    world = FakeRiot()
    world.add_account("Alpha", "EUW", "puuid-alpha")
    world.add_account("Beta", "EUW", "puuid-beta")
    world.set_rank("puuid-alpha", "GOLD", "II")
    stack = Stack(db, world)
    await stack.register(1, "Ana")
    await stack.register(2, "Rui")
    bot = SimpleNamespace(settings=load_settings(ENV))
    return Riot(bot, stack.riot), stack, world  # type: ignore[arg-type]


async def test_linking_your_own_account_shows_the_consent_notice(cog_stack):
    cog, stack, _ = cog_stack
    ix = FakeInteraction(1)
    await cog.link.callback(cog, ix, "Alpha#EUW", None, None)  # type: ignore[arg-type]
    assert len(ix.sent) == 1  # nobody else needs a public notice
    reply = ix.sent[0]
    assert reply["ephemeral"] is True
    assert "ligada como principal" in reply["content"]
    assert "Gold II" in reply["content"]
    assert CONSENT_NOTICE in reply["content"]
    await stack.riot.wait_for_background()


async def test_an_admin_linking_for_someone_posts_nothing_and_pings_nobody(cog_stack):
    cog, stack, _ = cog_stack
    ix = FakeInteraction(99)
    await cog.link.callback(cog, ix, "Alpha#EUW", None, member(1))  # type: ignore[arg-type]
    assert len(ix.sent) == 1  # only the admin's private summary: nothing in the channel
    reply = ix.sent[0]
    assert reply["ephemeral"] is True
    assert "ligada como principal" in reply["content"]
    assert CONSENT_NOTICE not in reply["content"]
    assert "<@1>" not in reply["content"] and "allowed_mentions" not in reply
    accounts = await stack.riot.accounts(1)
    assert accounts[0][0].linked_by == 99
    await stack.riot.wait_for_background()


async def test_a_non_admin_cannot_manage_someone_elses_accounts(cog_stack):
    cog, stack, _ = cog_stack
    await stack.riot.link(2, "Beta#EUW", None, 99)
    for command, args in (
        (cog.link, ("Alpha#EUW", None, member(2))),
        (cog.main, ("Beta#EUW", member(2))),
        (cog.unlink, ("Beta#EUW", member(2))),
        (cog.refresh, (member(2),)),
    ):
        ix = FakeInteraction(1)
        await command.callback(cog, ix, *args)  # type: ignore[arg-type]
        assert ix.sent[0]["content"] == NOT_ADMIN
    assert [a.riot_id for a, _ in await stack.riot.accounts(2)] == ["Beta#EUW"]  # untouched
    await stack.riot.wait_for_background()


async def test_main_unlink_and_list_flow(cog_stack):
    cog, stack, world = cog_stack
    world.set_rank("puuid-beta", "SILVER", "I")
    await stack.riot.link(1, "Alpha#EUW", None, 1)
    await stack.riot.link(1, "Beta#EUW", None, 1)
    await stack.riot.wait_for_background()

    ix = FakeInteraction(1)
    await cog.main.callback(cog, ix, "beta#euw", None)  # type: ignore[arg-type]
    assert "Beta#EUW** é agora a conta principal" in ix.sent[0]["content"]
    assert "Silver I" in ix.sent[0]["content"]

    ix = FakeInteraction(5)  # anyone in the server can look
    await cog.list_accounts.callback(cog, ix, member(1))  # type: ignore[arg-type]
    listing = ix.sent[0]
    assert "ephemeral" not in listing  # visible to the group, by design
    description = listing["embed"].description
    assert description.index("Beta#EUW") < description.index("Alpha#EUW")  # main first
    assert description.count("★") == 1

    ix = FakeInteraction(1)
    await cog.unlink.callback(cog, ix, "Beta#EUW", None)  # type: ignore[arg-type]
    text = ix.sent[0]["content"]
    assert "removida" in text
    assert "**Alpha#EUW** é agora a conta principal" in text  # it was the main: Alpha is promoted
    assert "Gold II" in text

    ix = FakeInteraction(1)
    await cog.unlink.callback(cog, ix, "Alpha#EUW", None)  # type: ignore[arg-type]
    assert "Já não há contas ligadas" in ix.sent[0]["content"]


async def test_unknown_account_and_unregistered_player_get_clear_messages(cog_stack):
    cog, _, _ = cog_stack
    ix = FakeInteraction(1)
    await cog.unlink.callback(cog, ix, "Nope#EUW", None)  # type: ignore[arg-type]
    assert "não está ligada" in ix.sent[0]["content"]
    ix = FakeInteraction(404)
    await cog.link.callback(cog, ix, "Alpha#EUW", None, None)  # type: ignore[arg-type]
    assert "não registado" in ix.sent[0]["content"]


async def test_integration_disabled_without_a_key(db):
    world = FakeRiot()
    stack = Stack(db, world, enabled=False)
    await stack.register(1, "Ana")
    cog = Riot(SimpleNamespace(settings=load_settings(ENV)), stack.riot)  # type: ignore[arg-type]
    ix = FakeInteraction(1)
    await cog.link.callback(cog, ix, "Alpha#EUW", None, None)  # type: ignore[arg-type]
    assert "desativada" in ix.sent[0]["content"]
    assert world.calls == []  # not a single request went out


async def test_autocomplete_offers_own_accounts_and_hides_others_from_non_admins(cog_stack):
    cog, stack, _ = cog_stack
    await stack.riot.link(1, "Alpha#EUW", None, 1)
    await stack.riot.link(1, "Beta#EUW", None, 1)
    await stack.riot.wait_for_background()
    ix = FakeInteraction(1)
    names = [c.name for c in await cog._account_autocomplete(ix, "be")]  # type: ignore[arg-type]
    assert names == ["Beta#EUW"]
    ix = FakeInteraction(2)
    ix.namespace = SimpleNamespace(player=member(1))
    assert await cog._account_autocomplete(ix, "") == []  # type: ignore[arg-type]
    ix = FakeInteraction(99)
    ix.namespace = SimpleNamespace(player=member(1))
    assert len(await cog._account_autocomplete(ix, "")) == 2  # type: ignore[arg-type]


async def test_refresh_command_reports_and_respects_the_cooldown(cog_stack):
    cog, stack, _ = cog_stack
    await stack.riot.link(1, "Alpha#EUW", None, 1)
    await stack.riot.wait_for_background()
    ix = FakeInteraction(1)
    await cog.refresh.callback(cog, ix, None)  # type: ignore[arg-type]
    assert "Atualizado" in ix.sent[0]["content"]
    assert "Gold II" in ix.sent[0]["content"]
    ix = FakeInteraction(1)
    await cog.refresh.callback(cog, ix, None)  # type: ignore[arg-type]
    assert "Tenta de novo" in ix.sent[0]["content"]
    await stack.riot.wait_for_background()


async def test_profile_and_list_embeds_show_accounts_rank_age_and_state(cog_stack):
    cog, stack, world = cog_stack
    for i in range(3):
        world.add_game("puuid-alpha", f"EUW1_{i}", "MIDDLE", days_ago(NOW, i + 1))
    await stack.riot.link(1, "Alpha#EUW", None, 1)
    await stack.riot.wait_for_background()
    profile = await stack.players.profile(1)
    embed = build_profile_embed(profile, DEFAULT_LADDER)
    fields = {f.name: f.value or "" for f in embed.fields}
    assert fields["Contas Riot"] == "Alpha#EUW ★"
    assert fields["Rank League"] == "Gold II"
    assert "Sem rank (colocação 0/5)" in fields["Scrims"]  # never mirrors Riot's rank
    listing = build_list_embed(
        "Ana", await stack.riot.accounts(1), NOW, "https://cdn.invalid/a.png", discord.Color(7)
    )
    assert "3 jogos ranked" in (listing.description or "")
    assert (listing.description or "").startswith("# Contas Riot · Ana")
    assert listing.footer.text is None and listing.thumbnail.url == "https://cdn.invalid/a.png"
    assert listing.color == discord.Color(7)


def test_link_summary_mentions_pending_rank_and_background_sync():
    from bot.features.riot.repository import AccountRow
    from bot.features.riot.service import RankOutcome

    account = AccountRow(1, 1, "p", "Alpha", "EUW", "euw1", False, None, 1, NOW)
    text = link_summary(LinkResult(account, False, RankOutcome("pending")))
    assert "secundária" in text
    assert "Não consegui ler o rank" in text
    assert "segundo plano" in text


async def test_extension_plugs_into_the_core_and_is_removable():
    # (Classes are looked up by cog name: discord.py drops an unloaded extension's module, so
    # types imported at the top of this file can differ from the freshly loaded ones.)
    bot = KDBot(load_settings(ENV))  # no RIOT_API_KEY
    async with bot:
        await bot.db.connect()
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        riot_cog: Any = bot.get_cog("Riot")
        players_cog: Any = bot.get_cog("Players")
        assert riot_cog is not None
        assert players_cog is not None
        assert riot_cog.service.client is None  # disabled, but loaded
        assert players_cog.ratings.main_puuid_of == riot_cog.service.main_puuid_of
        riot_group: Any = next(c for c in bot.tree.get_commands() if c.name == "riot")
        assert {c.name for c in riot_group.commands} == {
            "link",
            "main",
            "unlink",
            "list",
            "history",
            "refresh",
        }
        for ext in EXTENSIONS:
            await bot.unload_extension(ext)
        await bot.db.close()


async def test_without_the_riot_extension_the_core_still_works():
    bot = KDBot(load_settings(ENV))
    async with bot:
        await bot.db.connect()
        await bot.load_extension("bot.features.players.cog")
        players_cog: Any = bot.get_cog("Players")
        assert players_cog is not None
        player = await players_cog.players.register(1, "Ana")
        resolution = await players_cog.ratings.resolve(player.id)
        assert resolution.rank is None
        assert all(c.name != "riot" for c in bot.tree.get_commands())
        await bot.unload_extension("bot.features.players.cog")
        await bot.db.close()
