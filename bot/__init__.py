import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
ASSETS_URL = os.getenv("ASSETS_URL")
DATA_DIR = os.getenv("DATA_DIR")
