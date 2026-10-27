"""Uploading images as Discord application emojis (shared by the role and champion icons).

An application emoji belongs to the bot, works in every server and needs no permission to use.
`application_emojis` is the only place that talks to Discord for this; the services receive the
result as plain data and an `UploadIcon` function, so tests never need Discord.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bot.app import KDBot


class IconUploadError(Exception):
    """An icon could not be uploaded (the caller turns Discord's errors into this)."""


# name, PNG bytes -> the new emoji's id
UploadIcon = Callable[[str, bytes], Awaitable[int]]


async def application_emojis(bot: "KDBot") -> tuple[dict[str, int], UploadIcon]:
    """The bot's existing application emojis (name -> id) and a function that adds one."""
    existing = {e.name: e.id for e in await bot.fetch_application_emojis()}

    async def upload(name: str, image: bytes) -> int:
        try:
            return (await bot.create_application_emoji(name=name, image=image)).id
        except discord.HTTPException as exc:
            raise IconUploadError(f"Discord answered {exc.status}") from exc

    return existing, upload
