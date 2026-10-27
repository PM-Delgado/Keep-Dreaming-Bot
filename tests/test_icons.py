"""Role icons: bundled PNGs, upload/adopt/skip logic, text fallback, and the cog's Discord side."""

import struct
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from bot.config import load_settings
from bot.features.icons.cog import Icons
from bot.features.icons.roles import (
    ROLE_DIR,
    RoleEmojiRepository,
    RoleIconService,
    emoji_name,
    role_label,
    role_prefix,
)
from bot.features.icons.upload import IconUploadError
from bot.features.players.proficiency import ROLES

ENV = {"DISCORD_TOKEN": "t", "GUILD_ID": "1", "ASSETS_URL": "https://x.invalid/a/"}


class Uploads:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.names: list[str] = []
        self.images: list[bytes] = []
        self.fail = fail or set()

    async def __call__(self, name: str, image: bytes) -> int:
        if name in self.fail:
            raise IconUploadError("limit reached")
        self.names.append(name)
        self.images.append(image)
        return 500 + len(self.names)


def service(db, **kwargs) -> RoleIconService:
    return RoleIconService(RoleEmojiRepository(db), **kwargs)


# --- the bundled files ----


@pytest.mark.parametrize("role", ROLES)
def test_every_role_has_a_128px_transparent_png(role):
    data = (ROLE_DIR / f"{role}.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _depth, colour_type = struct.unpack(">IIBB", data[16:26])
    assert (width, height) == (128, 128)
    assert colour_type == 6  # RGBA: a transparent background
    assert len(data) < 256 * 1024  # Discord's emoji size limit


def test_emoji_names_are_valid_for_discord():
    names = {emoji_name(r) for r in ROLES}
    assert len(names) == 5
    assert all(2 <= len(n) <= 32 and n.replace("_", "").isalnum() for n in names)


# --- text fallback ----


def test_role_prefix_is_the_icon_or_the_role_name():
    icons = {"TOP": "<:role_top:1>"}
    assert role_prefix("TOP", icons) == "<:role_top:1> "
    assert role_prefix("SUPPORT", icons) == "**Suporte** — "
    assert role_prefix("MID", None) == "**Mid** — "
    assert role_label("TOP", icons) == "<:role_top:1>"
    assert role_label("ADC", icons) == "ADC"


# --- sync ----


async def test_sync_uploads_each_role_once_from_the_bundled_files(db):
    icons = service(db)
    uploads = Uploads()
    assert await icons.sync({}, uploads) == 5
    assert sorted(uploads.names) == sorted(emoji_name(r) for r in ROLES)
    assert all(image[:4] == b"\x89PNG" for image in uploads.images)
    markup = icons.markup()
    assert set(markup) == set(ROLES) and markup["TOP"] == "<:role_top:501>"
    assert await icons.sync({}, uploads) == 0  # nothing left to do
    assert len(uploads.names) == 5


async def test_markup_survives_a_restart(db):
    await service(db).sync({}, Uploads())
    fresh = service(db)
    assert fresh.markup() == {}  # nothing loaded yet
    await fresh.load()
    assert set(fresh.markup()) == set(ROLES)


async def test_sync_adopts_emojis_that_already_exist(db):
    icons = service(db)
    uploads = Uploads()
    assert await icons.sync({"role_top": 77, "unrelated": 1}, uploads) == 4
    assert "role_top" not in uploads.names
    assert icons.markup()["TOP"] == "<:role_top:77>"


async def test_sync_skips_a_failing_role_and_the_next_run_finishes(db):
    icons = service(db)
    assert await icons.sync({}, Uploads(fail={"role_mid"})) == 4
    assert "MID" not in icons.markup()
    assert await icons.sync({}, Uploads()) == 1
    assert set(icons.markup()) == set(ROLES)


async def test_sync_gives_up_after_three_failures_in_a_row(db):
    icons = service(db)
    uploads = Uploads(fail={emoji_name(r) for r in ROLES})
    assert await icons.sync({}, uploads) == 0
    assert icons.markup() == {}


async def test_a_missing_bundled_file_counts_as_a_failed_icon(db, tmp_path):
    icons = service(db, role_dir=tmp_path)  # empty directory
    assert await icons.sync({}, Uploads()) == 0


async def test_with_icons_off_nothing_is_uploaded_or_shown(db):
    await service(db).sync({}, Uploads())
    off = service(db, enabled=False)
    await off.load()
    uploads = Uploads()
    assert await off.sync({}, uploads) == 0 and uploads.names == []
    assert off.markup() == {}


async def test_the_database_only_accepts_the_five_roles(db):
    with pytest.raises(Exception, match="CHECK"):
        await RoleEmojiRepository(db).save("CARRY", 1, "role_carry")


# --- the cog's Discord side ----


class FakeEmojiBot:
    def __init__(self, existing=(), fail_with: Exception | None = None) -> None:
        self.settings = load_settings(ENV)
        self.existing = [SimpleNamespace(name=n, id=i) for n, i in existing]
        self.created: list[str] = []
        self.fail_with = fail_with

    async def fetch_application_emojis(self):
        return self.existing

    async def create_application_emoji(self, *, name: str, image: bytes):
        if self.fail_with:
            raise self.fail_with
        self.created.append(name)
        return SimpleNamespace(id=9000 + len(self.created))


async def test_the_cog_uploads_missing_icons_and_adopts_existing_ones(db):
    icons = service(db)
    bot = FakeEmojiBot(existing=[("role_adc", 31)])
    await Icons(bot, icons).sync()  # type: ignore[arg-type]
    assert sorted(bot.created) == sorted(emoji_name(r) for r in ROLES if r != "ADC")
    assert icons.markup()["ADC"] == "<:role_adc:31>"


async def test_the_cog_survives_discord_errors_and_retries_next_time(db):
    icons = service(db)
    response: Any = SimpleNamespace(status=400, reason="Bad Request")
    bot = FakeEmojiBot(fail_with=discord.HTTPException(response, "no room"))
    await Icons(bot, icons).sync()  # type: ignore[arg-type]
    assert icons.markup() == {}
    bot.fail_with = None
    await Icons(bot, icons).sync()  # type: ignore[arg-type]
    assert len(bot.created) == 5


async def test_the_cog_never_raises_and_does_nothing_when_off(db):
    class Broken(FakeEmojiBot):
        async def fetch_application_emojis(self):
            raise RuntimeError("boom")

    await Icons(Broken(), service(db)).sync()  # type: ignore[arg-type]
    bot = FakeEmojiBot()
    await Icons(bot, service(db, enabled=False)).sync()  # type: ignore[arg-type]
    assert bot.created == []
