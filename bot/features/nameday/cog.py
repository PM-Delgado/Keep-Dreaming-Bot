"""Slash commands, embeds and the daily task for the name of the day."""

import datetime as dt
import logging
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


def build_name_embed(pick: Pick, assets_url: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"💃🏻 Puta do Dia: **{pick.name.upper()}**",
        description=pick.phrase,
        color=discord.Color.red(),
    )
    embed.set_thumbnail(url=assets_url + "name_thumbnail.jpg")
    embed.set_footer(text=FOOTER)
    return embed


def build_leaderboard_embed(leaderboard: list[tuple[str, int]], assets_url: str) -> discord.Embed:
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
    embed.set_image(url=assets_url + "name_image.jpg")
    embed.set_footer(text=FOOTER)
    return embed


class NameDay(commands.Cog):
    def __init__(self, bot: "KDBot", service: NameDayService) -> None:
        self.bot = bot
        self.service = service
        self.daily_pick.start()

    async def cog_unload(self) -> None:
        self.daily_pick.cancel()

    # Fires at 00:00 Europe/Lisbon; /puta also picks on demand if the bot missed it.
    @tasks.loop(time=dt.time(hour=0, minute=0, tzinfo=LISBON))
    async def daily_pick(self) -> None:
        try:
            pick = await self.service.get_or_pick_today()
        except Exception:
            log.exception("Daily name pick failed")
            return
        log.info("Name of the day: %s", pick.name if pick else "no names available")

    @app_commands.command(
        name="puta", description="Mostra qual foi a puta escolhida para o dia de hoje."
    )
    async def puta(self, interaction: discord.Interaction) -> None:
        pick = await self.service.get_or_pick_today()
        if pick is None:
            await interaction.response.send_message("Não há nomes disponíveis.", ephemeral=True)
            return
        embed = build_name_embed(pick, self.bot.settings.assets_url)
        await interaction.response.send_message(content=pick.phrase, embed=embed)

    @app_commands.command(name="putaindor", description="Ranking das putas mais escolhidas.")
    async def putaindor(self, interaction: discord.Interaction) -> None:
        leaderboard = await self.service.leaderboard(3)
        if not leaderboard:
            await interaction.response.send_message(
                "Ainda não há ranking; usa /puta para começar.", ephemeral=True
            )
            return
        embed = build_leaderboard_embed(leaderboard, self.bot.settings.assets_url)
        await interaction.response.send_message(embed=embed)


async def setup(bot: "KDBot") -> None:
    repo = NameDayRepository(bot.db)
    await repo.seed_if_empty()
    await bot.add_cog(NameDay(bot, NameDayService(repo)))
