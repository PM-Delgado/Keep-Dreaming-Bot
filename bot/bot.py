import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv
import logging
from bot import TOKEN, GUILD_ID

intents = discord.Intents.default()
intents.message_content = True

# Configure centralized logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger('discord')

bot = commands.Bot(command_prefix="/", intents=intents)

@bot.command()
@commands.is_owner()
async def sync(ctx):
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        logger.info(f'✅ Synced {len(synced)} commands to Guild {GUILD_ID}')
        logger.info(f'📋 Synced commands: {[cmd.name for cmd in synced]}')
        
    except Exception as e:
        logger.error(f'❌ Failed to sync commands: {e}')

# Load all cogs
async def load_cogs():
    cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
    for filename in os.listdir(cogs_dir):
        if filename.endswith(".py") and not filename.startswith("__"):
            cog_name = filename[:-3]
            await bot.load_extension(f"bot.cogs.{cog_name}")
            print(f"✅ Loaded cog: {cog_name}")

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

async def main():
    async with bot:
        await load_cogs()
        await bot.start(TOKEN)

try:
    asyncio.run(main())
except KeyboardInterrupt:
    logger.info(f'Bot stopped by user.')