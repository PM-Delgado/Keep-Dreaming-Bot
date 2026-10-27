"""Champion list: Data Dragon client, cached list, lookup and refresh. No network."""

import json
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from bot.config import load_settings
from bot.features.champions.client import (
    BASE_URL,
    VERSIONS_URL,
    Champion,
    DataDragonError,
    fetch_champions,
    icon_url,
)
from bot.features.champions.cog import Champions
from bot.features.champions.repository import SEED_FILE, ChampionRepository
from bot.features.champions.service import (
    ChampionService,
    emoji_name,
    normalise,
)
from bot.features.icons.upload import IconUploadError


def dragon(patch: str = "99.1.1", extra: dict[str, str] | None = None):
    """A fake `get_json` serving a tiny Data Dragon for `patch`."""
    champions = {"Ahri": "Ahri", "MonkeyKing": "Wukong", "Kaisa": "Kai'Sa", **(extra or {})}
    calls: list[str] = []

    async def get_json(url: str) -> Any:
        calls.append(url)
        if url == VERSIONS_URL:
            return [patch, "98.24.1"]
        assert url == f"{BASE_URL}/cdn/{patch}/data/en_US/champion.json"
        return {"data": {k: {"id": k, "name": v, "title": "x"} for k, v in champions.items()}}

    get_json.calls = calls  # type: ignore[attr-defined]
    return get_json


# --- client ----


async def test_fetch_reads_the_latest_patch_and_sorts_by_name():
    result = await fetch_champions(dragon("99.1.1"))
    assert result.patch == "99.1.1"
    assert [c.name for c in result.champions] == ["Ahri", "Kai'Sa", "Wukong"]
    assert result.champions[2] == Champion("MonkeyKing", "Wukong")


@pytest.mark.parametrize(
    "versions, champions",
    [
        ({}, {"data": {}}),  # versions is not a list
        ([], {"data": {}}),
        (["1.0.0"], []),  # champion payload is not an object
        (["1.0.0"], {"data": {}}),  # no champions
        (["1.0.0"], {"data": {"A": {"id": "A"}}}),  # no name
        (["1.0.0"], {"data": {"A": "nope"}}),
    ],
)
async def test_fetch_rejects_payloads_it_does_not_understand(versions, champions):
    async def get_json(url: str) -> Any:
        return versions if url == VERSIONS_URL else champions

    with pytest.raises(DataDragonError):
        await fetch_champions(get_json)


async def test_a_network_error_surfaces_as_a_data_dragon_error():
    async def get_json(url: str) -> Any:
        raise DataDragonError("down")

    with pytest.raises(DataDragonError):
        await fetch_champions(get_json)


# --- seed and cache ----


def test_the_bundled_seed_is_a_complete_champion_list():
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    ids = [c["id"] for c in data["champions"]]
    assert data["patch"]
    assert len(ids) == len(set(ids)) >= 150
    assert {"Ahri", "MonkeyKing", "Kaisa"} <= set(ids)


async def test_the_seed_fills_an_empty_table_once(db):
    repo = ChampionRepository(db)
    assert await repo.seed_if_empty() is True
    assert await repo.seed_if_empty() is False
    assert await repo.current_patch() == json.loads(SEED_FILE.read_text("utf-8"))["patch"]
    assert len(await repo.all()) >= 150


async def test_replace_all_swaps_the_whole_list(db):
    repo = ChampionRepository(db)
    await repo.seed_if_empty()
    await repo.replace_all(await fetch_champions(dragon("99.1.1")))
    assert [c.id for c in await repo.all()] == ["Ahri", "Kaisa", "MonkeyKing"]
    assert await repo.current_patch() == "99.1.1"


# --- service ----


async def test_refresh_replaces_the_list_only_when_the_patch_changes(db):
    service = ChampionService(ChampionRepository(db), dragon("99.1.1"))
    await service.start()
    assert await service.refresh() is True
    assert await service.refresh() is False  # same patch: nothing to do
    newer = ChampionService(ChampionRepository(db), dragon("99.2.1", {"Zed": "Zed"}))
    assert await newer.refresh() is True
    assert (await newer.find("zed")) == Champion("Zed", "Zed")


async def test_a_failed_refresh_keeps_serving_the_cached_list(db):
    async def broken(url: str) -> Any:
        raise DataDragonError("down")

    service = ChampionService(ChampionRepository(db), broken)
    await service.start()
    assert await service.try_refresh() is False
    with pytest.raises(DataDragonError):
        await service.refresh()
    assert await service.find("Ahri") == Champion("Ahri", "Ahri")


async def test_refresh_refreshes_the_lookup_cache(db):
    service = ChampionService(ChampionRepository(db), dragon("99.1.1"))
    await service.start()
    assert await service.find("Wukong") is not None  # fills the cache from the seed
    await service.refresh()
    assert len(await service.suggest("")) == 3  # not the seed's 170-odd


@pytest.mark.parametrize("typed", ["Kai'Sa", "kaisa", "KAI SA", "Kaisa", " kai-sa "])
async def test_find_ignores_case_spaces_and_punctuation(db, typed):
    service = ChampionService(ChampionRepository(db), dragon())
    await service.refresh()
    found = await service.find(typed)
    assert found is not None and found.id == "Kaisa"


async def test_find_accepts_the_display_name_or_the_data_dragon_id(db):
    service = ChampionService(ChampionRepository(db), dragon())
    await service.refresh()
    assert (await service.find("Wukong")) == (await service.find("MonkeyKing"))


async def test_find_returns_none_for_unknown_or_empty_text(db):
    service = ChampionService(ChampionRepository(db), dragon())
    await service.refresh()
    assert await service.find("Teemo's twin") is None
    assert await service.find("   ") is None
    assert await service.find("'") is None


async def test_suggest_puts_names_that_start_with_the_text_first(db):
    extra = {"Karma": "Karma", "Ekko": "Ekko", "Akali": "Akali"}
    service = ChampionService(ChampionRepository(db), dragon(extra=extra))
    await service.refresh()
    assert [c.name for c in await service.suggest("ka")] == ["Kai'Sa", "Karma", "Akali"]
    assert await service.suggest("zzz") == []
    assert len(await service.suggest("", limit=2)) == 2


async def test_names_maps_ids_to_display_names(db):
    service = ChampionService(ChampionRepository(db), dragon())
    await service.refresh()
    assert await service.names(["MonkeyKing", "Nope"]) == {"MonkeyKing": "Wukong"}
    assert await service.names([]) == {}


def test_normalise_folds_accents_and_symbols():
    assert normalise("Renata Glasc") == "renataglasc"
    assert normalise("Nunu & Willump") == "nunuwillump"
    assert normalise("Bel'Veth") == "belveth"
    assert normalise("Vel’Koz") == "velkoz"


# --- icons (application emojis) ----


class Uploads:
    """A fake emoji uploader: records what was uploaded and hands out ids."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.names: list[str] = []
        self.fail = fail or set()

    async def __call__(self, name: str, image: bytes) -> int:
        if name in self.fail:
            raise IconUploadError("limit reached")
        assert image == b"PNG"
        self.names.append(name)
        return 1000 + len(self.names)


async def png(url: str) -> bytes:
    assert url.startswith(f"{BASE_URL}/cdn/99.1.1/img/champion/") and url.endswith(".png")
    return b"PNG"


async def icon_service(db, **kwargs):
    service = ChampionService(ChampionRepository(db), dragon("99.1.1"), png, **kwargs)
    await service.refresh()
    return service


def test_icon_urls_and_emoji_names():
    assert icon_url("16.1.1", "MonkeyKing") == f"{BASE_URL}/cdn/16.1.1/img/champion/MonkeyKing.png"
    assert emoji_name("Kaisa") == "champ_Kaisa"
    assert 2 <= len(emoji_name("Kaisa")) <= 32


def live(uploads: Uploads) -> dict[str, int]:
    """The application's emojis after these uploads (what Discord would list)."""
    return {name: 1001 + i for i, name in enumerate(uploads.names)}


async def test_sync_uploads_every_missing_icon_once(db):
    service = await icon_service(db)
    uploads = Uploads()
    assert await service.sync_icons({}, uploads) == 3
    assert sorted(uploads.names) == ["champ_Ahri", "champ_Kaisa", "champ_MonkeyKing"]
    markup = await service.icon_markup(["Ahri", "Nope"])
    assert list(markup) == ["Ahri"] and markup["Ahri"].startswith("<:champ_Ahri:")
    assert await service.sync_icons(live(uploads), uploads) == 0  # nothing left to do
    assert len(uploads.names) == 3


async def test_icons_saved_for_another_application_are_replaced(db):
    service = await icon_service(db)
    for champion_id in ("Ahri", "Kaisa", "MonkeyKing"):  # a development bot's ids
        await service.repo.save_emoji(champion_id, 9_000, emoji_name(champion_id))
    uploads = Uploads()
    assert await service.sync_icons({"champ_Ahri": 555}, uploads) == 2
    assert "champ_Ahri" not in uploads.names  # adopted by name instead
    markup = await service.icon_markup(["Ahri", "Kaisa", "MonkeyKing"])
    assert markup["Ahri"] == "<:champ_Ahri:555>"
    assert all(":9000>" not in m for m in markup.values())


async def test_sync_adopts_emojis_that_already_exist(db):
    service = await icon_service(db)
    uploads = Uploads()
    assert await service.sync_icons({"champ_Ahri": 555, "unrelated": 1}, uploads) == 2
    assert "champ_Ahri" not in uploads.names
    assert (await service.icon_markup(["Ahri"]))["Ahri"] == "<:champ_Ahri:555>"


async def test_sync_skips_a_failing_icon_and_goes_on(db):
    service = await icon_service(db)
    uploads = Uploads(fail={"champ_Kaisa"})
    assert await service.sync_icons({}, uploads) == 2
    assert set(await service.icon_markup(["Ahri", "Kaisa", "MonkeyKing"])) == {"Ahri", "MonkeyKing"}
    assert await service.sync_icons(live(uploads), Uploads()) == 1  # the next run finishes the job


async def test_sync_gives_up_after_repeated_failures(db):
    extra = {f"Extra{i}": f"Extra{i}" for i in range(10)}
    service = ChampionService(ChampionRepository(db), dragon("99.1.1", extra), png)
    await service.refresh()
    uploads = Uploads(fail={emoji_name(c) for c in [*extra, "Ahri", "Kaisa", "MonkeyKing"]})
    assert await service.sync_icons({}, uploads) == 0
    assert uploads.names == []  # stopped early instead of trying all thirteen


async def test_a_download_failure_counts_as_a_failed_icon(db):
    async def broken(url: str) -> bytes:
        raise DataDragonError("down")

    service = ChampionService(ChampionRepository(db), dragon("99.1.1"), broken)
    await service.refresh()
    assert await service.sync_icons({}, Uploads()) == 0
    assert await service.icon_markup(["Ahri"]) == {}


async def test_with_icons_off_nothing_is_uploaded_or_shown(db):
    service = await icon_service(db)
    await service.sync_icons({}, Uploads())
    off = ChampionService(ChampionRepository(db), dragon("99.1.1"), png, icons=False)
    uploads = Uploads()
    assert await off.sync_icons({}, uploads) == 0 and uploads.names == []
    assert await off.icon_markup(["Ahri"]) == {}


async def test_icons_survive_a_new_patch(db):
    service = await icon_service(db)
    await service.sync_icons({}, Uploads())
    newer = ChampionService(ChampionRepository(db), dragon("99.2.1", {"Zed": "Zed"}), png)
    await newer.refresh()
    assert "Ahri" in await newer.icon_markup(["Ahri", "Zed"])  # kept
    assert "Zed" not in await newer.icon_markup(["Ahri", "Zed"])  # not uploaded yet


# --- the cog's Discord side ----


class FakeEmojiBot:
    """Just the application-emoji calls of discord.Client that the cog uses."""

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


ENV = {"DISCORD_TOKEN": "t", "GUILD_ID": "1"}


async def test_the_cog_uploads_missing_icons_and_adopts_existing_ones(db):
    service = await icon_service(db)
    bot = FakeEmojiBot(existing=[("champ_Ahri", 77)])
    await Champions(bot, service).sync_icons()  # type: ignore[arg-type]
    assert sorted(bot.created) == ["champ_Kaisa", "champ_MonkeyKing"]
    assert (await service.icon_markup(["Ahri"]))["Ahri"] == "<:champ_Ahri:77>"


async def test_the_cog_survives_discord_errors_and_retries_next_time(db):
    service = await icon_service(db)
    response: Any = SimpleNamespace(status=400, reason="Bad Request")
    refused = discord.HTTPException(response, "no room")
    bot = FakeEmojiBot(fail_with=refused)
    await Champions(bot, service).sync_icons()  # type: ignore[arg-type]
    assert await service.icon_markup(["Ahri", "Kaisa", "MonkeyKing"]) == {}
    bot.fail_with = None
    await Champions(bot, service).sync_icons()  # type: ignore[arg-type]
    assert len(bot.created) == 3


async def test_the_cog_never_raises_out_of_the_refresh_task(db):
    service = await icon_service(db)

    class Broken(FakeEmojiBot):
        async def fetch_application_emojis(self):
            raise RuntimeError("boom")

    await Champions(Broken(), service).sync_icons()  # type: ignore[arg-type]
    off = await icon_service(db, icons=False)
    bot = FakeEmojiBot()
    await Champions(bot, off).sync_icons()  # type: ignore[arg-type]
    assert bot.created == []
