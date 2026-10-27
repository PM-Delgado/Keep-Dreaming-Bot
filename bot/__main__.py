import asyncio
import contextlib
import logging
import signal

from bot.app import KDBot
from bot.config import ConfigError, Settings, load_settings
from bot.logging_setup import setup_logging

log = logging.getLogger("bot")


def stop_on_sigterm(task: asyncio.Task[None]) -> None:
    """Railway (like systemd) stops a service with SIGTERM: cancel the bot so it closes cleanly."""
    # Windows has no loop signal handlers; there Ctrl+C is the way to stop it.
    with contextlib.suppress(NotImplementedError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)


async def run(settings: Settings) -> None:
    async with KDBot(settings) as bot:  # leaving the block closes Discord and the database
        task = asyncio.create_task(bot.start(settings.token))
        stop_on_sigterm(task)
        try:
            await task
        except asyncio.CancelledError:
            log.info("Stop signal received.")


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
