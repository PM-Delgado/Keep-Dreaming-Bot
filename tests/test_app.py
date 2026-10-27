import discord

from bot.app import EXTENSIONS, KDBot
from bot.config import load_settings
from bot.features.nameday.cog import build_leaderboard_embed

ENV = {
    "DISCORD_TOKEN": "t",
    "GUILD_ID": "123",
    "DB_PATH": ":memory:",
}


async def test_registers_exactly_the_expected_commands():
    bot = KDBot(load_settings(ENV))
    async with bot:
        await bot.db.connect()
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        names = {c.name for c in bot.tree.get_commands()}
        for ext in EXTENSIONS:  # stops the daily task
            await bot.unload_extension(ext)
        await bot.db.close()
    assert names == {
        "puta",
        "putaindor",
        "sync",
        "register",
        "profile",
        "admin",
        "riot",
        "lobby",
        "match",
        "history",
        "stats",
        "leaderboard",
    }
    assert EXTENSIONS == (
        "bot.features.nameday.cog",
        "bot.features.admin.cog",
        "bot.features.icons.cog",
        "bot.features.champions.cog",
        "bot.features.players.cog",
        "bot.features.riot.cog",
        "bot.features.lobby.cog",
        "bot.features.stats.cog",
    )


async def _top_level_commands(env: dict[str, str]) -> set[str]:
    bot = KDBot(load_settings(env))
    async with bot:
        await bot.db.connect()
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        names = {c.name for c in bot.tree.get_commands()}
        for ext in EXTENSIONS:
            await bot.unload_extension(ext)
        await bot.db.close()
    return names


async def test_mock_commands_are_only_offered_when_testing_is_on():
    assert "mock" not in await _top_level_commands(ENV)
    assert "mock" in await _top_level_commands({**ENV, "ENABLE_MOCK_PLAYERS": "true"})


def test_leaderboard_embed_handles_fewer_than_three():
    embed = build_leaderboard_embed([("Ana", 2)])
    assert [f.name for f in embed.fields] == ["🥈 —", "🥇 Ana", "🏅 —"]
    assert isinstance(embed, discord.Embed)


def test_no_retired_dependencies_imported():
    import sys

    assert "pomice" not in sys.modules
