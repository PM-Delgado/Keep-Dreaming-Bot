import discord

from bot.app import EXTENSIONS, KDBot
from bot.config import load_settings
from bot.features.nameday.cog import build_leaderboard_embed

ENV = {
    "DISCORD_TOKEN": "t",
    "GUILD_ID": "123",
    "ASSETS_URL": "https://x.invalid/a/",
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
        "mock",
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


def test_leaderboard_embed_handles_fewer_than_three():
    embed = build_leaderboard_embed([("Ana", 2)], "https://x.invalid/a/")
    assert [f.name for f in embed.fields] == ["🥈 —", "🥇 Ana", "🏅 —"]
    assert isinstance(embed, discord.Embed)


def test_no_retired_dependencies_imported():
    import sys

    assert "pomice" not in sys.modules
