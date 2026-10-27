import asyncio
import logging

from bot.app import KDBot
from bot.config import ConfigError, Settings, load_settings
from bot.logging_setup import setup_logging

log = logging.getLogger("bot")


async def run(settings: Settings) -> None:
    async with KDBot(settings) as bot:
        await bot.start(settings.token)


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    log_file = setup_logging(settings.log_level, settings.log_dir)
    log.info("Logging to %s", log_file or "the console only")
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
    except Exception:
        log.critical("The bot crashed", exc_info=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
