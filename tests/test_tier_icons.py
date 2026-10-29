"""Rank (tier) icons: bundled PNGs, upload/adopt/skip logic, and the leaderboard's use of them."""

import struct
from datetime import UTC, datetime

import pytest
from test_icons import ENV, Uploads, live  # noqa: F401  (shared fakes)

from bot.features.icons.tiers import TIER_DIR, TierEmojiRepository, TierIconService, emoji_name
from bot.features.ratings.rank import ALL_TIERS
from bot.features.riot.leaderboard import TIER_DOTS, MainRank, build_embed, build_rows, row_text

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def service(db, **kwargs) -> TierIconService:
    return TierIconService(TierEmojiRepository(db), **kwargs)


def one_row(tier: str | None, division: str | None = None, lp: int | None = 10):
    return build_rows([MainRank(1, "Ana", "Ana#EUW", tier, division, lp)], {})[0]


# --- the bundled files ----


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_every_tier_has_a_128px_transparent_png(tier):
    data = (TIER_DIR / f"{tier}.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _depth, colour_type = struct.unpack(">IIBB", data[16:26])
    assert (width, height) == (128, 128)
    assert colour_type == 6  # RGBA: a transparent background
    assert len(data) < 256 * 1024  # Discord's emoji size limit


def test_there_is_a_bundled_file_and_a_fallback_dot_for_every_tier_and_nothing_else():
    assert {p.stem for p in TIER_DIR.glob("*.png")} == set(ALL_TIERS)
    assert set(TIER_DOTS) == set(ALL_TIERS)


def test_emoji_names_are_valid_for_discord():
    names = {emoji_name(t) for t in ALL_TIERS}
    assert len(names) == len(ALL_TIERS) == 10
    assert all(2 <= len(n) <= 32 and n.replace("_", "").isalnum() for n in names)


# --- upload / adopt / skip ----


async def test_sync_uploads_each_tier_once_from_the_bundled_files(db):
    svc, uploads = service(db), Uploads()
    assert await svc.sync({}, uploads) == 10
    assert sorted(uploads.names) == sorted(emoji_name(t) for t in ALL_TIERS)
    assert all(image[:4] == b"\x89PNG" for image in uploads.images)
    assert set(svc.markup()) == set(ALL_TIERS)
    assert svc.markup()["GOLD"].startswith("<:tier_gold:")
    assert await svc.sync(live(uploads), uploads) == 0  # nothing left to do
    assert len(uploads.names) == 10


async def test_markup_survives_a_restart(db):
    uploads = Uploads()
    await service(db).sync({}, uploads)
    fresh = service(db)
    assert fresh.markup() == {}  # not loaded yet
    await fresh.load()
    assert set(fresh.markup()) == set(ALL_TIERS)


async def test_sync_adopts_emojis_that_already_exist(db):
    svc, uploads = service(db), Uploads()
    assert await svc.sync({"tier_gold": 77}, uploads) == 9
    assert "tier_gold" not in uploads.names
    assert svc.markup()["GOLD"] == "<:tier_gold:77>"


async def test_icons_saved_for_another_application_are_uploaded_again(db):
    first = Uploads()
    await service(db).sync({}, first)
    second, svc = Uploads(), service(db)
    assert await svc.sync({}, second) == 10  # none of the saved ids exist in the new application


async def test_a_failing_tier_is_skipped_and_the_next_run_finishes(db):
    svc = service(db)
    broken = Uploads(fail={"tier_gold"})
    assert await svc.sync({}, broken) == 9
    assert "GOLD" not in svc.markup()
    fixed = Uploads()
    assert await svc.sync(live(broken), fixed) == 1
    assert fixed.names == ["tier_gold"]


async def test_sync_gives_up_after_three_failures_in_a_row(db):
    uploads = Uploads(fail={emoji_name(t) for t in ALL_TIERS})
    assert await service(db).sync({}, uploads) == 0
    assert uploads.names == []


async def test_with_icons_off_nothing_is_uploaded_or_shown(db):
    svc, uploads = service(db, enabled=False), Uploads()
    assert await svc.sync({}, uploads) == 0
    assert uploads.names == [] and svc.markup() == {}


# --- the leaderboard ----


def test_a_row_shows_the_emblem_when_there_is_one_else_the_colour_dot():
    row = one_row("GOLD", "II", 40)
    assert row_text(row, {"GOLD": "<:tier_gold:5>"}).startswith(
        "🥇 **Ana** · <:tier_gold:5> GOLD II"
    )
    assert row_text(row, {}).startswith("🥇 **Ana** · 🟡 GOLD II")
    assert row_text(row, None) == row_text(row, {})
    # Another tier's icon is not borrowed.
    assert "<:tier_gold:5>" not in row_text(one_row("SILVER", "I", 5), {"GOLD": "<:tier_gold:5>"})


def test_unranked_rows_and_unknown_tiers_never_get_an_icon():
    icons = {"GOLD": "<:tier_gold:5>"}
    assert "Sem rank" in row_text(one_row(None, None, None), icons)
    assert "<:" not in row_text(one_row("WOOD", "I", 5), icons)


def test_the_embed_uses_the_icons_it_is_given():
    rows = build_rows([MainRank(1, "Ana", "Ana#EUW", "MASTER", None, 689)], {})
    embed = build_embed(rows, 1, NOW, NOW, {"MASTER": "<:tier_master:9>"})
    assert "<:tier_master:9> MASTER · 689 LP" in (embed.description or "")
    assert "🟣" not in (embed.description or "")
