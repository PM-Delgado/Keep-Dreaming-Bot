import discord
import os
import logging
import time
from discord.ext import commands, tasks
from discord import app_commands
from bot.utils.db_manager import DBManager
from bot import ASSETS_URL

class NameDay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._daily_name_check.start()

    def _log(self, level: str, msg: str) -> None:
        logger = logging.getLogger('discord')
        log_method = getattr(logger, level.lower(), logger.info)
        log_method(msg)

    @tasks.loop(minutes=5)
    async def _daily_name_check(self):
        # Check every 5 minutes if we need to update the name of the day
        current_name = DBManager.get_current_name()
        if not current_name:
            new_name = DBManager.update_name_of_the_day()
            if new_name:
                self._log('INFO', f"Updated name of the day to: {new_name}")
        
        self._log('INFO', f'Checked name of the day; no update needed.')

    def _create_embed_leaderboard(self, leaderboard):
        # Helper to create an embed for name selection
        embed = discord.Embed(
            title=f"💃🏻 PUTAIN D'OR 🏆",
            description="Eis que são reveladas as putas mais escolhidas pelos nossos estimados clientes! 👑",
            color=discord.Color.gold()
        )

        # Extrai os 3 primeiros
        second = leaderboard[1] if len(leaderboard) > 1 else ("—", 0)
        first = leaderboard[0]
        third = leaderboard[2] if len(leaderboard) > 2 else ("—", 0)

        # 2nd Place
        embed.add_field(
                    name=f"🥈 {second[0]}",
                    value=f"{second[1]} votos"
                )
        
        # 1st Place
        embed.add_field(
                    name=f"🥇 {first[0]}",
                    value=f"{first[1]} votos"
                )
        
        # 3rd Place
        embed.add_field(
                    name=f"🏅 {third[0]}",
                    value=f"{third[1]} votos"
                )

        embed.set_image(url=ASSETS_URL + f"name_image.jpg?cachebust={int(time.time())}")
        embed.set_footer(text="Bada Bing! - Route 17, Lodi, New Jersey")
        return embed

    def _create_embed_name(self, name, phrase):
        # Helper to create an embed for name selection
        embed = discord.Embed(
            title=f"💃🏻 Puta do Dia: **{name.upper()}**",
            description=phrase,
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=ASSETS_URL + f"name_thumbnail.jpg?cachebust={int(time.time())}")
        embed.set_footer(text="Bada Bing! - Route 17, Lodi, New Jersey")
        return embed
    
    @app_commands.command(
        name="puta",
        description="Mostra qual foi a puta escolhida para o dia de hoje.")
    @app_commands.guilds(discord.Object(id=int(os.getenv('GUILD_ID'))))
    async def get_name(self, ctx):
        # Get the current name of the day
        current_name = DBManager.get_current_name()
        if not current_name:
            current_name = DBManager.update_name_of_the_day() or "No names available"
            
        current_phrase = DBManager.get_current_phrase()
        embed = self._create_embed_name(current_name, current_phrase)
        content = f"{current_phrase}"
        await ctx.response.send_message(content=content, embed=embed)

    @app_commands.command(
        name="putaindor",
        description="Ranking das putas mais escolhidas.")
    @app_commands.guilds(discord.Object(id=int(os.getenv('GUILD_ID'))))
    async def name_leaderboard(self, ctx):
        # Show top 3 most selected names
        leaderboard = DBManager.get_leaderboard()
        
        embed = self._create_embed_leaderboard(leaderboard)
                
        await ctx.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(NameDay(bot))
