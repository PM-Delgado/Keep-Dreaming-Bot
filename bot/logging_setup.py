"""Logging for the running bot: console plus a daily rotating file.

One format everywhere, so a line reads the same in the terminal and in `logs/kdbot.log`:

    2026-10-27 21:04:11 INFO    bot.app: Logged in as KD Bot (id 123) ...

The Discord libraries are chatty at INFO (every HTTP request), so their loggers are held at
WARNING except the gateway, whose connect/resume lines are worth keeping.
"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE = "kdbot.log"
KEEP_DAYS = 14

# Library loggers that are quiet unless something is wrong.
QUIET_LOGGERS = ("discord", "discord.http", "aiohttp", "asyncio")
KEEP_LOGGERS = ("discord.gateway", "discord.client")


class _NoVoiceWarnings(logging.Filter):
    """discord.py warns that voice is unsupported at every start; this bot has no voice features."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> Path | None:
    """Configure the root logger. Returns the log file path, or None when only the console is used.

    Safe to call again (handlers installed by an earlier call are replaced).
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_kdbot", False)]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    formatter = logging.Formatter(FORMAT, DATE_FORMAT)

    # A Windows console may not encode every character of a nickname or an emoji: never crash on it.
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    _install(root, console)

    path: Path | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / LOG_FILE
        file_handler = TimedRotatingFileHandler(
            path, when="midnight", backupCount=KEEP_DAYS, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        _install(root, file_handler)

    quiet = logging.INFO if root.level <= logging.DEBUG else logging.WARNING
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(quiet)
    for name in KEEP_LOGGERS:
        logging.getLogger(name).setLevel(root.level)
    return path


def _install(root: logging.Logger, handler: logging.Handler) -> None:
    handler._kdbot = True  # type: ignore[attr-defined]
    handler.addFilter(_NoVoiceWarnings())
    root.addHandler(handler)
