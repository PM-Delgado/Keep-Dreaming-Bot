import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ASSETS_URL = os.getenv("ASSETS_URL")
DATA_DIR = os.getenv("DATA_DIR")

try:
    GUILD_ID = int(os.getenv("GUILD_ID", ""))
except ValueError:
    raise RuntimeError("GUILD_ID is missing or not a number; set it in .env (see .env.example).") from None
