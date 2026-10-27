from types import SimpleNamespace

import discord
import pytest_asyncio
from discord import app_commands

from bot.app import EXTENSIONS, KDBot
from bot.config import load_settings
from bot.features.players.admin import InhouseAdmin, build_inspection_embed
from bot.features.players.cog import Players, build_profile_embed, progress_bar
from bot.features.players.repository import PlayerRepository
from bot.features.players.service import PlayerService
from bot.features.ratings.ladder import DEFAULT_LADDER
from bot.features.ratings.params import MatchmakingParams
from bot.features.ratings.repository import RatingRepository
from bot.features.ratings.service import RatingService

ENV = {
    "DISCORD_TOKEN": "t",
    "GUILD_ID": "123",
    "ASSETS_URL": "https://x.invalid/a/",
    "DB_PATH": ":memory:",
    "ADMIN_USER_IDS": "99",
}


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})

    def is_done(self) -> bool:
        return bool(self.sent)


def fake_interaction(user_id: int):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), response=FakeResponse())


@pytest_asyncio.fixture
async def stack(db):
    ratings = RatingService(RatingRepository(db))
    players = PlayerService(PlayerRepository(db), ratings)
    bot = SimpleNamespace(settings=load_settings(ENV))
    return Players(bot, players, ratings), InhouseAdmin(bot, players, ratings), players, ratings  # type: ignore[arg-type]


def embed_text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.footer.text or ""]
    for field in embed.fields:
        parts += [field.name or "", field.value or ""]
    return "\n".join(parts)


def test_progress_bar():
    assert progress_bar(None) == ""
    assert progress_bar(0.0) == "▱" * 10
    assert progress_bar(0.5) == "▰" * 5 + "▱" * 5
    assert progress_bar(1.0) == "▰" * 10


async def test_register_command_replies_ephemerally_and_rejects_duplicates(stack):
    players_cog, _, _, _ = stack
    first = fake_interaction(1)
    await players_cog.register.callback(players_cog, first, "Ana")
    assert first.response.sent[0]["ephemeral"] is True
    assert "Registado" in first.response.sent[0]["content"]
    second = fake_interaction(1)
    await players_cog.register.callback(players_cog, second, "Ana2")
    assert "Já estás registado" in second.response.sent[0]["content"]


async def test_profile_embed_shows_group_rank_but_no_raw_numbers(stack):
    players_cog, _, players, ratings = stack
    await players.register(1, "Ana")
    player = await players.require(1)
    await ratings.set_self_reported_rank(player.id, "GOLD", "II", 1)
    await players.set_declared(1, "MID", 4)
    embed = build_profile_embed(await players.profile(1), DEFAULT_LADDER)
    text = embed_text(embed)
    assert "Sem rank (colocação 0/5)" in text  # no in-house games yet: not a League rank
    assert "Gold II (declarado)" in text
    assert "**Mid**: 4/5 (declarado)" in text
    assert "não equivale a um rank do League" in text
    assert "isn't endorsed by Riot Games" in text
    assert "25.2" not in text  # mu of Gold II must never be shown to normal players
    assert "6.5" not in text  # nor sigma
    assert "sigma" not in text.lower()


async def test_profile_view_for_unregistered_user_is_a_polite_ephemeral(stack):
    players_cog, _, _, _ = stack
    ix = fake_interaction(5)
    member = SimpleNamespace(id=404)
    await players_cog.view.callback(players_cog, ix, member)
    assert ix.response.sent[0]["ephemeral"] is True
    assert "não está registado" in ix.response.sent[0]["content"]


async def test_inspection_embed_is_the_only_place_with_raw_numbers(stack):
    _, admin_cog, players, ratings = stack
    await players.register(1, "Ana")
    player = await players.require(1)
    await ratings.set_self_reported_rank(player.id, "MASTER", None, 1)
    inspection = await ratings.inspect(player.id)
    text = embed_text(build_inspection_embed("Ana", inspection, "colocação 0/5"))
    assert "mu 31.20" in text
    assert f"sigma {MatchmakingParams().sigma_rank_seed:.2f}" in text


def walk(command):
    yield command
    for child in getattr(command, "commands", []):
        yield from walk(child)


async def test_every_admin_command_is_allowlist_gated():
    bot = KDBot(load_settings(ENV))
    async with bot:
        await bot.db.connect()
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        admin = next(c for c in bot.tree.get_commands() if c.name == "admin")
        leaves = [c for c in walk(admin) if isinstance(c, app_commands.Command)]
        assert {c.qualified_name for c in leaves} == {
            "admin player register",
            "admin rating set",
            "admin rating inspect",
            "admin rating recompute",
            "admin rating rank",
            "admin roles set",
        }
        for leaf in leaves:
            assert any(check.__name__ == "_admin_predicate" for check in leaf.checks), leaf.name
        for ext in EXTENSIONS:
            await bot.unload_extension(ext)
        await bot.db.close()


def fake_member(user_id: int, display_name: str):
    return SimpleNamespace(id=user_id, display_name=display_name, mention=f"<@{user_id}>")


async def test_admin_register_uses_the_discord_display_name_by_default(stack):
    _, admin_cog, players, ratings = stack
    ix = fake_interaction(99)
    await admin_cog.player_register.callback(admin_cog, ix, fake_member(5, "  Joana  da   Silva "))
    reply = ix.response.sent[0]
    assert reply["ephemeral"] is True
    assert "registado como **Joana da Silva**" in reply["content"]
    player = await players.require(5)  # identity is the Discord user ID
    assert player.nickname == "Joana da Silva"
    # Initialised exactly like self-registration: default rating, no ranks, unknown roles.
    stored = await ratings.repo.get_stored(player.id)
    assert stored is not None
    assert (stored.mu, stored.games, stored.placed) == (25.0, 0, False)
    assert (await ratings.resolve(player.id)).rank is None
    assert all(line.proficiency.value is None for line in (await players.profile(5)).roles)
    async with ratings.repo.db.conn.execute(
        "SELECT source, created_by FROM rating_events WHERE player_id = ?", (player.id,)
    ) as cur:
        assert list(await cur.fetchall()) == [("default", 99)]  # audit: the admin did it


async def test_admin_register_with_a_custom_display_name(stack):
    _, admin_cog, players, _ = stack
    ix = fake_interaction(99)
    await admin_cog.player_register.callback(admin_cog, ix, fake_member(6, "discordname"), "Rui")
    assert (await players.require(6)).nickname == "Rui"


async def test_self_registration_still_records_the_player_as_the_creator(stack):
    players_cog, _, players, ratings = stack
    await players_cog.register.callback(players_cog, fake_interaction(7), "Eva")
    player = await players.require(7)
    async with ratings.repo.db.conn.execute(
        "SELECT created_by FROM rating_events WHERE player_id = ?", (player.id,)
    ) as cur:
        assert list(await cur.fetchall()) == [(7,)]


async def test_admin_register_of_an_existing_player_changes_nothing(stack):
    _, admin_cog, players, ratings = stack
    await players.register(8, "Original")
    player = await players.require(8)
    await ratings.set_self_reported_rank(player.id, "GOLD", "II", 8)
    await ratings.adjust(player.id, 27.0, 5.0, 99)
    await players.set_declared(8, "MID", 4)
    await players.set_admin_override(8, "TOP", 2)

    async def snapshot():
        out = {}
        for table in ("players", "player_roles", "rating_events", "rank_signals", "ratings"):
            async with ratings.repo.db.conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2") as cur:
                out[table] = list(await cur.fetchall())
        return out

    before = await snapshot()
    for name in (None, "Outro Nome"):
        ix = fake_interaction(99)
        await admin_cog.player_register.callback(admin_cog, ix, fake_member(8, "Novo"), name)
        reply = ix.response.sent[0]
        assert reply["ephemeral"] is True
        assert reply["content"] == "Original já está registado. Nada foi alterado."
    assert await snapshot() == before


async def test_admin_register_reports_name_problems_clearly(stack):
    _, admin_cog, players, _ = stack
    await players.register(1, "Ana")
    ix = fake_interaction(99)
    await admin_cog.player_register.callback(admin_cog, ix, fake_member(2, "x" * 40))
    assert "entre 2 e 24" in ix.response.sent[0]["content"]
    assert "display_name" in ix.response.sent[0]["content"]
    assert await players.repo.get_by_discord(2) is None

    ix = fake_interaction(99)
    await admin_cog.player_register.callback(admin_cog, ix, fake_member(3, "ana"))
    assert "já está em uso" in ix.response.sent[0]["content"]
    assert await players.repo.get_by_discord(3) is None


async def test_admin_register_is_denied_for_non_admins():
    from bot.permissions import _admin_predicate

    bot = KDBot(load_settings(ENV))  # ADMIN_USER_IDS = 99
    async with bot:
        await bot.db.connect()
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        admin = next(c for c in bot.tree.get_commands() if c.name == "admin")
        command = next(c for c in walk(admin) if c.qualified_name == "admin player register")
        assert isinstance(command, app_commands.Command)
        assert _admin_predicate in command.checks
        outsider = SimpleNamespace(client=bot, user=SimpleNamespace(id=1))
        allowed = SimpleNamespace(client=bot, user=SimpleNamespace(id=99))
        assert await _admin_predicate(outsider) is False  # type: ignore[arg-type]
        assert await _admin_predicate(allowed) is True  # type: ignore[arg-type]
        for ext in EXTENSIONS:
            await bot.unload_extension(ext)
        await bot.db.close()


async def test_profile_roles_get_their_icon_in_front_when_it_exists(stack):
    _, _, players, _ = stack
    await players.register(1, "Ana")
    await players.set_declared(1, "MID", 4)
    profile = await players.profile(1)
    plain = embed_text(build_profile_embed(profile, DEFAULT_LADDER))
    assert "<:role_mid" not in plain and "**Mid**: 4/5 (declarado)" in plain
    icons = {"MID": "<:role_mid:8>"}
    text = embed_text(build_profile_embed(profile, DEFAULT_LADDER, icons))
    assert "<:role_mid:8> **Mid**: 4/5 (declarado)" in text
    assert "**Top**: —" in text  # no icon for Top: just the name, as before
