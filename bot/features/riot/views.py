"""The leaderboard's ◀ ▶ buttons: fixed `custom_id`s and no timeout, so the one registered view
answers every leaderboard message, including those posted before a restart. A click edits the
message in place; the page it shows is read from the embed's footer ("Página 1/2 ...").
"""

from datetime import UTC, datetime
from typing import Any

import discord

from bot.features.riot.leaderboard import (
    build_embed,
    next_page,
    page_count,
    page_of_footer,
)
from bot.features.riot.service import RiotService

NO_MENTIONS = discord.AllowedMentions.none()


async def leaderboard_message(service: RiotService, page: int = 1) -> dict[str, Any]:
    """Keyword arguments for `send`: the embed for one page of the saved ranks, and the buttons
    (only when there is more than one page)."""
    rows, updated = await service.leaderboard()
    message: dict[str, Any] = {
        "embed": build_embed(rows, page, updated, datetime.now(UTC)),
        "allowed_mentions": NO_MENTIONS,
    }
    if page_count(rows) > 1:
        message["view"] = LeaderboardView(service)
    return message


class LeaderboardView(discord.ui.View):
    def __init__(self, service: RiotService) -> None:
        super().__init__(timeout=None)
        self.service = service

    async def _flip(self, interaction: discord.Interaction, step: int) -> None:
        message = interaction.message
        footer = message.embeds[0].footer.text if message and message.embeds else None
        rows, updated = await self.service.leaderboard()
        page = next_page(page_of_footer(footer), page_count(rows), step)
        embed = build_embed(rows, page, updated, datetime.now(UTC))
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="riotlb:prev")
    async def previous(
        self, interaction: discord.Interaction, button: discord.ui.Button["LeaderboardView"]
    ) -> None:
        await self._flip(interaction, -1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="riotlb:next")
    async def following(
        self, interaction: discord.Interaction, button: discord.ui.Button["LeaderboardView"]
    ) -> None:
        await self._flip(interaction, 1)
