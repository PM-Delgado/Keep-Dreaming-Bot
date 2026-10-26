import os

# Must be set before importing the bot package (it reads the environment at import time).
# Real environment values / .env are never needed for tests.
os.environ["GUILD_ID"] = "123456789012345678"
os.environ["DISCORD_TOKEN"] = "test-token"
os.environ["ASSETS_URL"] = "https://example.invalid/assets/"
