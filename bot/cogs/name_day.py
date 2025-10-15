import discord
from discord.ext import commands, tasks
from discord import app_commands
from bot.utils.db_manager import DBManager
from datetime import datetime, timezone
import os

class NameDay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_name_check.start()

    @tasks.loop(minutes=5)
    async def daily_name_check(self):
        """Check every 5 minutes if we need to update the name of the day"""
        current_name = DBManager.get_current_name()
        if not current_name:
            new_name = DBManager.update_name_of_the_day()
            if new_name:
                print(f"Selected new name of the day: {new_name}")

    @app_commands.command(
        name="name",
        description="Set the server's language preference.")
    @app_commands.guilds(discord.Object(id=int(os.getenv('GUILD_ID'))))
    async def get_name(self, ctx):
        """Get the current name of the day"""
        current_name = DBManager.get_current_name()
        if not current_name:
            current_name = DBManager.update_name_of_the_day() or "No names available"
            
        embed = discord.Embed(
            title="Name of the Day",
            description=f"Today's special name is: **{current_name}**",
            color=discord.Color.gold()
        )
        await ctx.response.send_message(embed=embed)

    @app_commands.command(
        name="leaderboard",
        description="leaderboard")
    @app_commands.guilds(discord.Object(id=int(os.getenv('GUILD_ID'))))
    async def name_leaderboard(self, ctx):
        """Show top 3 most selected names"""
        leaderboard = DBManager.get_leaderboard()
        
        embed = discord.Embed(
            title="Name Leaderboard",
            description="Top 3 most chosen names",
            color=discord.Color.blue()
        )
        
        if not leaderboard:
            embed.add_field(name="No data", value="No names have been selected yet", inline=False)
        else:
            for i, (name, count) in enumerate(leaderboard, 1):
                embed.add_field(
                    name=f"{i}. {name}",
                    value=f"Selected {count} times",
                    inline=False
                )
                
        await ctx.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(NameDay(bot))
