import asyncio
import logging

import discord

from bot.app import KDBot
from bot.config import ConfigError, load_settings

log = logging.getLogger("bot")


async def main() -> None:
    settings = load_settings()
    async with KDBot(settings) as bot:
        await bot.start(settings.token)


if __name__ == "__main__":
    discord.utils.setup_logging(level=logging.INFO)
    try:
        asyncio.run(main())
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
