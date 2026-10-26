from types import SimpleNamespace

from bot.config import load_settings
from bot.permissions import _admin_predicate, is_admin

ENV = {"DISCORD_TOKEN": "t", "GUILD_ID": "1", "ASSETS_URL": "https://x.invalid/a"}


def interaction(settings, user_id):
    return SimpleNamespace(
        client=SimpleNamespace(settings=settings), user=SimpleNamespace(id=user_id)
    )


def test_only_allowlisted_ids_are_admins():
    settings = load_settings({**ENV, "ADMIN_USER_IDS": "10,20"})
    assert is_admin(settings, 10)
    assert not is_admin(settings, 30)


def test_empty_allowlist_denies_everyone():
    settings = load_settings(ENV)
    assert not is_admin(settings, 10)


async def test_check_predicate_uses_the_bot_settings():
    settings = load_settings({**ENV, "ADMIN_USER_IDS": "10"})
    assert await _admin_predicate(interaction(settings, 10)) is True  # type: ignore[arg-type]
    assert await _admin_predicate(interaction(settings, 11)) is False  # type: ignore[arg-type]
    assert await _admin_predicate(interaction(load_settings(ENV), 10)) is False  # type: ignore[arg-type]
