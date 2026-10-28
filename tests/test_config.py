from pathlib import Path

import pytest

from bot.config import ConfigError, load_settings

VALID = {"DISCORD_TOKEN": "t", "GUILD_ID": "42"}


def test_valid_settings():
    s = load_settings(VALID)
    assert (s.token, s.guild_id) == ("t", 42)
    assert s.db_path == Path("data/kdbot.sqlite3")


@pytest.mark.parametrize("missing", ["DISCORD_TOKEN", "GUILD_ID"])
def test_missing_value_raises(missing):
    env = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_settings(env)


def test_non_numeric_guild_id():
    with pytest.raises(ConfigError, match="GUILD_ID"):
        load_settings({**VALID, "GUILD_ID": "abc"})


def test_admin_ids_default_to_nobody():
    assert load_settings(VALID).admin_ids == frozenset()
    assert load_settings({**VALID, "ADMIN_USER_IDS": " , "}).admin_ids == frozenset()


def test_admin_ids_are_parsed():
    s = load_settings({**VALID, "ADMIN_USER_IDS": "111, 222 ,111"})
    assert s.admin_ids == frozenset({111, 222})


def test_invalid_admin_id_raises():
    with pytest.raises(ConfigError, match="ADMIN_USER_IDS"):
        load_settings({**VALID, "ADMIN_USER_IDS": "111,abc"})


def test_riot_settings_are_optional_with_a_default_platform():
    s = load_settings(VALID)
    assert s.riot_api_key is None
    assert s.riot_default_platform == "euw1"
    assert load_settings({**VALID, "RIOT_API_KEY": "   "}).riot_api_key is None


def test_riot_settings_are_read_and_the_key_is_not_in_the_repr():
    s = load_settings({**VALID, "RIOT_API_KEY": "RGAPI-secret", "RIOT_DEFAULT_PLATFORM": " NA1 "})
    assert s.riot_api_key == "RGAPI-secret"
    assert s.riot_default_platform == "na1"
    assert "RGAPI-secret" not in repr(s)


def test_invalid_riot_platform_raises():
    with pytest.raises(ConfigError, match="RIOT_DEFAULT_PLATFORM"):
        load_settings({**VALID, "RIOT_DEFAULT_PLATFORM": "mars1"})


def test_champion_icons_are_on_unless_turned_off():
    assert load_settings(VALID).champion_icons is True
    assert load_settings({**VALID, "CHAMPION_ICONS": "true"}).champion_icons is True
    for off in ("false", "0", "no", "off", ""):
        assert load_settings({**VALID, "CHAMPION_ICONS": off}).champion_icons is False, off


def test_scrim_channel_and_role_are_optional_numeric_ids():
    unset = load_settings(VALID)
    assert unset.scrim_channel_id is None and unset.scrim_role_id is None
    blank = load_settings({**VALID, "SCRIM_CHANNEL_ID": " ", "SCRIM_ROLE_ID": ""})
    assert blank.scrim_channel_id is None and blank.scrim_role_id is None
    s = load_settings({**VALID, "SCRIM_CHANNEL_ID": "1234567890123456", "SCRIM_ROLE_ID": " 99 "})
    assert (s.scrim_channel_id, s.scrim_role_id) == (1234567890123456, 99)


@pytest.mark.parametrize("name", ["SCRIM_CHANNEL_ID", "SCRIM_ROLE_ID"])
def test_a_scrim_id_that_is_not_a_number_is_refused(name):
    with pytest.raises(ConfigError, match=name):
        load_settings({**VALID, name: "#scrim"})
