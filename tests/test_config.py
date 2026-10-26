from pathlib import Path

import pytest

from bot.config import ConfigError, load_settings

VALID = {"DISCORD_TOKEN": "t", "GUILD_ID": "42", "ASSETS_URL": "https://x.invalid/a"}


def test_valid_settings():
    s = load_settings(VALID)
    assert (s.token, s.guild_id) == ("t", 42)
    assert s.assets_url == "https://x.invalid/a/"
    assert s.db_path == Path("data/kdbot.sqlite3")


@pytest.mark.parametrize("missing", ["DISCORD_TOKEN", "GUILD_ID", "ASSETS_URL"])
def test_missing_value_raises(missing):
    env = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_settings(env)


def test_non_numeric_guild_id():
    with pytest.raises(ConfigError, match="GUILD_ID"):
        load_settings({**VALID, "GUILD_ID": "abc"})
