import logging
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands

from bot import interaction_log
from bot.app import KDBot
from bot.config import ConfigError, load_settings
from bot.db import Database
from bot.logging_setup import LOG_FILE, setup_logging

ENV = {
    "DISCORD_TOKEN": "SECRET-TOKEN",
    "GUILD_ID": "123",
    "DB_PATH": ":memory:",
    "RIOT_API_KEY": "RGAPI-secret",
}


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    before = (root.level, list(root.handlers))
    yield root
    for handler in list(root.handlers):
        if handler not in before[1]:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(before[0])


def fake_interaction(*, kind=discord.InteractionType.application_command, **extra) -> Any:
    base = {
        "type": kind,
        "command": SimpleNamespace(qualified_name="lobby open"),
        "namespace": [("lobby_id", 3), ("note", "x" * 200)],
        "user": SimpleNamespace(id=42, name="ana"),
        "channel": SimpleNamespace(name="scrims"),
        "channel_id": 7,
        "message": None,
        "data": {"custom_id": "lobby:join"},
        "extras": {},
        "response": SimpleNamespace(is_done=lambda: True),
        "followup": SimpleNamespace(send=_noop),
    }
    return SimpleNamespace(**{**base, **extra})


async def _noop(*args, **kwargs):
    return None


# --- settings ---


def test_log_settings_default_to_info_and_a_logs_folder():
    s = load_settings(ENV)
    assert (s.log_level, str(s.log_dir)) == ("INFO", "logs")


def test_log_settings_are_read_and_an_empty_dir_means_console_only():
    s = load_settings({**ENV, "LOG_LEVEL": " debug ", "LOG_DIR": " "})
    assert (s.log_level, s.log_dir) == ("DEBUG", None)


def test_invalid_log_level_raises():
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_settings({**ENV, "LOG_LEVEL": "loud"})


def test_the_startup_summary_names_the_switches_but_not_the_secrets():
    text = KDBot(load_settings(ENV)).describe_settings()
    assert "riot=on" in text and "mock_players=off" in text and "guild=123" in text
    assert "SECRET-TOKEN" not in text and "RGAPI" not in text


# --- file and console setup ---


def test_setup_writes_a_file_in_one_format_and_is_repeatable(tmp_path, clean_root):
    path = setup_logging("INFO", tmp_path / "logs")
    setup_logging("INFO", tmp_path / "logs")  # a second call replaces, not stacks, the handlers
    logging.getLogger("bot.test").info("hello %s", "world")
    logging.getLogger("discord.http").info("every HTTP request")  # held back
    for handler in clean_root.handlers:
        handler.flush()
    assert path == tmp_path / "logs" / LOG_FILE
    assert path is not None
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].endswith("INFO    bot.test: hello world")


def test_no_log_dir_means_console_only(clean_root):
    assert setup_logging("INFO", None) is None


# --- interaction lines ---


def test_a_command_line_has_who_where_what_and_outcome(caplog):
    caplog.set_level(logging.INFO, "bot.interactions")
    i = fake_interaction()
    interaction_log.mark_received(i)
    interaction_log.log_completed(i)
    line = caplog.messages[0]
    assert line.startswith("/lobby open by ana (42) in #scrims (7) [lobby_id=3 note='xxx")
    assert "…'] -> ok " in line and line.rstrip().endswith("ms")


def test_a_refusal_shows_the_reason_in_the_completion_line(caplog):
    caplog.set_level(logging.INFO, "bot.interactions")
    i = fake_interaction()
    interaction_log.log_refusal(i, ValueError("Já estás registado."))
    assert caplog.messages == []  # a slash command reports once, when it completes
    interaction_log.log_completed(i)
    assert "-> refused" in caplog.messages[0] and "(Já estás registado.)" in caplog.messages[0]


def test_a_button_is_logged_on_arrival_and_its_refusal_at_once(caplog):
    caplog.set_level(logging.INFO, "bot.interactions")
    i = fake_interaction(
        kind=discord.InteractionType.component, command=None, message=SimpleNamespace(id=99)
    )
    interaction_log.log_received(i)
    interaction_log.log_refusal(i, ValueError("Lobby cheia."))
    assert caplog.messages == [
        "button lobby:join by ana (42) in #scrims (7) on message 99",
        "button lobby:join by ana (42) refused: Lobby cheia.",
    ]


def test_autocomplete_is_not_logged(caplog):
    caplog.set_level(logging.INFO, "bot.interactions")
    interaction_log.log_received(fake_interaction(kind=discord.InteractionType.autocomplete))
    assert caplog.messages == []


async def test_a_denied_command_is_a_warning_and_a_crash_an_error_with_traceback(caplog):
    caplog.set_level(logging.INFO, "bot.interactions")
    bot = KDBot(load_settings(ENV))
    await bot.on_tree_error(fake_interaction(), app_commands.CheckFailure())
    cause = RuntimeError("boom")
    await bot.on_tree_error(fake_interaction(), app_commands.AppCommandError(cause))
    denied, crashed = caplog.records
    assert (denied.levelno, "-> denied" in denied.message) == (logging.WARNING, True)
    assert crashed.levelno == logging.ERROR and "-> error" in crashed.message
    assert crashed.exc_info is not None


# --- database summary ---


async def test_the_database_summary_has_the_schema_version_and_table_sizes(db: Database):
    text = await db.describe()
    assert text.startswith(":memory: schema v")
    assert "names=0" in text and "picks=0" in text and "players=0" in text
