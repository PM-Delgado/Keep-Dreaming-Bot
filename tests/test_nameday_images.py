"""The name-of-the-day images are attached files, so no web address has to host them."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot.features.nameday.cog import (
    IMAGE,
    THUMBNAIL,
    NameDay,
    asset_file,
    build_leaderboard_embed,
    build_name_embed,
)
from bot.features.nameday.repository import Pick
from bot.features.nameday.service import LISBON, NameDayService


def test_the_two_images_ship_with_the_repository():
    for name in (THUMBNAIL, IMAGE):
        file = asset_file(name)
        assert file.filename == name
        assert file.fp.read(2) == b"\xff\xd8"  # a JPEG
        file.close()


def test_embeds_point_at_the_attached_files():
    name = build_name_embed(Pick(date(2026, 1, 1), "Ana", "Hoje: Ana"))
    assert name.thumbnail.url == f"attachment://{THUMBNAIL}"
    board = build_leaderboard_embed([("Ana", 2)])
    assert board.image.url == f"attachment://{IMAGE}"


async def test_the_commands_send_the_image_with_the_embed(repo):
    await repo.db.conn.execute("INSERT INTO names (name) VALUES ('Ana')")
    await repo.db.conn.execute("INSERT INTO phrases (text) VALUES ('Hoje: {name}')")
    await repo.db.conn.commit()
    service = NameDayService(repo, clock=lambda: datetime(2026, 1, 10, 9, 0, tzinfo=LISBON))
    cog = NameDay(SimpleNamespace(), service)  # type: ignore[arg-type]
    try:
        for command, expected in (
            (cog.goomah, THUMBNAIL),
            (cog.puta, THUMBNAIL),
            (cog.goomahdor, IMAGE),
            (cog.putaindor, IMAGE),
        ):
            interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
            await command.callback(cog, interaction)  # type: ignore[arg-type]
            kwargs = interaction.response.send_message.call_args.kwargs
            assert isinstance(kwargs["embed"], discord.Embed)
            assert kwargs["file"].filename == expected
            kwargs["file"].close()
    finally:
        await cog.cog_unload()


async def test_each_command_answers_the_same_under_both_names(repo):
    await repo.db.conn.execute("INSERT INTO names (name) VALUES ('Ana')")
    await repo.db.conn.execute("INSERT INTO phrases (text) VALUES ('Hoje: {name}')")
    await repo.db.conn.commit()
    service = NameDayService(repo, clock=lambda: datetime(2026, 1, 10, 9, 0, tzinfo=LISBON))
    cog = NameDay(SimpleNamespace(), service)  # type: ignore[arg-type]
    try:
        for new_name, original in ((cog.goomah, cog.puta), (cog.goomahdor, cog.putaindor)):
            answers = []
            for command in (new_name, original):
                interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
                await command.callback(cog, interaction)  # type: ignore[arg-type]
                kwargs = interaction.response.send_message.call_args.kwargs
                kwargs["file"].close()
                answers.append((kwargs.get("content"), kwargs["embed"].to_dict()))
            assert answers[0] == answers[1]
            assert new_name.description == original.description
    finally:
        await cog.cog_unload()
