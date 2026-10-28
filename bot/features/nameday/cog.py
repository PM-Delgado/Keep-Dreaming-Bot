"""Slash commands, embeds and the daily task for the name of the day."""

import datetime as dt
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.features.nameday.repository import NameDayRepository, Pick
from bot.features.nameday.service import LISBON, NameDayService

if TYPE_CHECKING:
    from bot.app import KDBot

log = logging.getLogger(__name__)

FOOTER = "Bada Bing! - Route 17, Lodi, New Jersey"
PLACEHOLDER = ("—", 0)
TODAY_DESCRIPTION = "Mostra qual foi a puta escolhida para o dia de hoje."
LEADERBOARD_DESCRIPTION = "Ranking das putas mais escolhidas."

# The two images live in the repository and are sent with the message (an embed can show an
# attached file as `attachment://name`), so no web address has to host them.
ASSETS_DIR = Path(__file__).resolve().parents[3] / "assets"
THUMBNAIL = "name_thumbnail.jpg"
IMAGE = "name_image.jpg"


def asset_file(name: str) -> discord.File:
    """A fresh attachment for one of the bundled images (a File can only be sent once)."""
    return discord.File(ASSETS_DIR / name, filename=name)


def build_name_embed(pick: Pick) -> discord.Embed:
    embed = discord.Embed(
        title=f"💃🏻 Puta do Dia: **{pick.name.upper()}**",
        description=pick.phrase,
        color=discord.Color.red(),
    )
    embed.set_thumbnail(url=f"attachment://{THUMBNAIL}")
    embed.set_footer(text=FOOTER)
    return embed


def build_leaderboard_embed(leaderboard: list[tuple[str, int]]) -> discord.Embed:
    embed = discord.Embed(
        title="💃🏻 PUTAIN D'OR 🏆",
        description=(
            "Eis que são reveladas as putas mais escolhidas pelos nossos estimados clientes! 👑"
        ),
        color=discord.Color.gold(),
    )
    first, second, third = (list(leaderboard) + [PLACEHOLDER] * 3)[:3]
    # Podium order: 2nd, 1st, 3rd.
    for medal, (name, count) in (("🥈", second), ("🥇", first), ("🏅", third)):
        embed.add_field(name=f"{medal} {name}", value=f"{count} votos")
    embed.set_image(url=f"attachment://{IMAGE}")
    embed.set_footer(text=FOOTER)
    return embed


class NameDay(commands.Cog):
    def __init__(self, bot: "KDBot", service: NameDayService) -> None:
        self.bot = bot
        self.service = service
        self.daily_pick.start()

    async def cog_unload(self) -> None:
        self.daily_pick.cancel()

    # Fires at 00:00 Europe/Lisbon; /goomah also picks on demand if the bot missed it.
    @tasks.loop(time=dt.time(hour=0, minute=0, tzinfo=LISBON))
    async def daily_pick(self) -> None:
        try:
            pick = await self.service.get_or_pick_today()
        except Exception:
            log.exception("Daily name pick failed")
            return
        log.info("Name of the day: %s", pick.name if pick else "no names available")

    async def _show_today(self, interaction: discord.Interaction) -> None:
        pick = await self.service.get_or_pick_today()
        if pick is None:
            await interaction.response.send_message("Não há nomes disponíveis.", ephemeral=True)
            return
        await interaction.response.send_message(
            content=pick.phrase, embed=build_name_embed(pick), file=asset_file(THUMBNAIL)
        )

    async def _show_leaderboard(self, interaction: discord.Interaction) -> None:
        leaderboard = await self.service.leaderboard(3)
        if not leaderboard:
            await interaction.response.send_message(
                "Ainda não há ranking; usa /puta para começar.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=build_leaderboard_embed(leaderboard), file=asset_file(IMAGE)
        )

    # Each command exists under two names (a reference to The Sopranos, and the original): both run
    # the same code, so they can never drift apart. The documentation only mentions the first name.
    @app_commands.command(name="goomah", description=TODAY_DESCRIPTION)
    async def goomah(self, interaction: discord.Interaction) -> None:
        await self._show_today(interaction)

    @app_commands.command(name="puta", description=TODAY_DESCRIPTION)
    async def puta(self, interaction: discord.Interaction) -> None:
        await self._show_today(interaction)

    @app_commands.command(name="goomahdor", description=LEADERBOARD_DESCRIPTION)
    async def goomahdor(self, interaction: discord.Interaction) -> None:
        await self._show_leaderboard(interaction)

    @app_commands.command(name="putaindor", description=LEADERBOARD_DESCRIPTION)
    async def putaindor(self, interaction: discord.Interaction) -> None:
        await self._show_leaderboard(interaction)


async def setup(bot: "KDBot") -> None:
    repo = NameDayRepository(bot.db)
    await repo.seed_if_empty()
    await bot.add_cog(NameDay(bot, NameDayService(repo)))
