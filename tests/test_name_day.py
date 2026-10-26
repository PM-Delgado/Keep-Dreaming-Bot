import asyncio
import json
from pathlib import Path
from datetime import datetime, timedelta

import discord
import pytest
import pytz
from discord.ext import commands

from bot.utils import db_manager
from bot.utils.db_manager import DBManager

LISBON = pytz.timezone("Europe/Lisbon")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db_manager, "DATA_DIR", str(tmp_path))
    (tmp_path / "names.json").write_text(json.dumps(["Ana"]), encoding="utf-8")
    (tmp_path / "phrases.json").write_text(json.dumps(["Hoje: {name}"]), encoding="utf-8")
    (tmp_path / "name_stats.json").write_text(json.dumps({"Ana": 2, "Bia": 5}), encoding="utf-8")
    return tmp_path


def write_current(data_dir, when: datetime):
    (data_dir / "name_of_the_day.json").write_text(
        json.dumps({"name": "Ana", "last_updated": when.isoformat(), "phrase": "p"}), encoding="utf-8"
    )


def test_update_selects_name_formats_phrase_and_counts(data_dir):
    assert DBManager.update_name_of_the_day() == "Ana"
    assert json.loads((data_dir / "name_stats.json").read_text())["Ana"] == 3
    assert DBManager.get_current_name() == "Ana"
    assert DBManager.get_current_phrase() == "Hoje: Ana"


def test_current_name_valid_today_and_expired_yesterday(data_dir):
    now = datetime.now(LISBON)
    write_current(data_dir, now)
    assert DBManager.get_current_name() == "Ana"
    write_current(data_dir, now - timedelta(days=1))
    assert DBManager.get_current_name() is None
    assert DBManager.get_current_phrase() is None


def test_current_name_missing_file(data_dir):
    assert DBManager.get_current_name() is None


def test_leaderboard_sorted_top_three(data_dir):
    (data_dir / "name_stats.json").write_text(json.dumps({"a": 1, "b": 4, "c": 3, "d": 2}))
    assert DBManager.get_leaderboard() == [("b", 4), ("c", 3), ("d", 2)]


def test_update_without_names_returns_none(data_dir):
    (data_dir / "names.json").write_text("[]")
    assert DBManager.update_name_of_the_day() is None


def test_bot_registers_only_retained_commands():
    from bot import bot as bot_module

    async def run():
        b = commands.Bot(command_prefix="/", intents=discord.Intents.default())
        async with b:
            await b.load_extension("bot.cogs.name_day")
            names = {c.name for c in b.tree.get_commands(guild=discord.Object(id=123456789012345678))}
            cog_names = set(b.cogs)
            b.cogs["NameDay"]._daily_name_check.cancel()
            return names, cog_names

    names, cog_names = asyncio.run(run())
    assert names == {"puta", "putaindor"}
    assert cog_names == {"NameDay"}
    cogs_dir = Path(bot_module.__file__).parent / "cogs"
    assert [p.name for p in cogs_dir.glob("*.py") if not p.name.startswith("__")] == ["name_day.py"]


def test_no_music_dependencies_imported():
    import sys
    import bot.bot  # noqa: F401
    assert "pomice" not in sys.modules
