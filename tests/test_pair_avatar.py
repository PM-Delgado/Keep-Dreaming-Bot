"""The two-player picture of `/riot duo` and `/rivalry`: half of each avatar in one image."""

import asyncio
import io
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from PIL import Image

from bot.features.icons.pair_avatar import ATTACHMENT_URL, SIZE, PairAvatars, split_avatar

RED, BLUE = (220, 30, 30, 255), (30, 30, 220, 255)
GREEN, YELLOW = (30, 200, 30, 255), (230, 220, 20, 255)


def two_tone(left: tuple, right: tuple, size: tuple[int, int] = (64, 64)) -> bytes:
    """A PNG whose left half is one colour and whose right half is another."""
    image = Image.new("RGBA", size, left)
    image.paste(Image.new("RGBA", (size[0] - size[0] // 2, size[1]), right), (size[0] // 2, 0))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def decode(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGBA")


def test_it_takes_the_left_half_of_the_first_and_the_right_half_of_the_second():
    result = decode(split_avatar(two_tone(RED, BLUE), two_tone(GREEN, YELLOW)))
    assert result.size == (SIZE, SIZE)
    mid = SIZE // 2
    for y in (2, SIZE // 2, SIZE - 3):
        assert result.getpixel((5, y)) == RED  # the first one's left half
        assert result.getpixel((mid - 20, y)) == RED
        assert result.getpixel((mid + 20, y)) == YELLOW  # the second one's right half
        assert result.getpixel((SIZE - 5, y)) == YELLOW


def test_the_output_is_a_png_of_the_requested_size():
    png = split_avatar(two_tone(RED, BLUE), two_tone(GREEN, YELLOW), size=128)
    assert png.startswith(b"\x89PNG")
    assert decode(png).size == (128, 128)


def test_a_picture_that_is_not_square_is_cropped_not_squashed():
    wide = two_tone(RED, BLUE, size=(200, 100))
    tall = two_tone(GREEN, YELLOW, size=(100, 200))
    result = decode(split_avatar(wide, tall))
    assert result.size == (SIZE, SIZE)
    assert result.getpixel((5, 5)) == RED and result.getpixel((SIZE - 5, SIZE - 5)) == YELLOW


def test_transparency_is_kept():
    clear = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    out = io.BytesIO()
    clear.save(out, "PNG")
    result = decode(split_avatar(out.getvalue(), two_tone(GREEN, YELLOW)))
    assert result.getchannel("A").getpixel((5, 5)) == 0  # still transparent on the first's side
    assert result.getpixel((SIZE - 5, 5)) == YELLOW


def test_bytes_that_are_not_an_image_raise_an_os_error():
    with pytest.raises(OSError):
        split_avatar(b"not an image", two_tone(RED, BLUE))
    with pytest.raises(OSError):
        split_avatar(two_tone(RED, BLUE), b"")


# --- PairAvatars: fetching, caching and falling back -------------------------------------------


class FakeAsset:
    """A member's avatar: `replace` gives the same asset back, `read` gives its bytes."""

    def __init__(self, url: str, data: bytes, reads: list[str], delay: float = 0.0) -> None:
        self.url, self.data, self.reads, self.delay = url, data, reads, delay

    def replace(self, **_kwargs) -> "FakeAsset":
        return self

    async def read(self) -> bytes:
        self.reads.append(self.url)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.data


def avatars_of(reads: list[str], **extra) -> dict[int, FakeAsset]:
    """Discord id -> avatar: 1 is red | blue, 2 is green | yellow."""
    return {
        1: FakeAsset("https://cdn.invalid/1.png", two_tone(RED, BLUE), reads, **extra),
        2: FakeAsset("https://cdn.invalid/2.png", two_tone(GREEN, YELLOW), reads, **extra),
    }


def fake_guild(avatars: dict[int, FakeAsset]) -> SimpleNamespace:
    """A server that knows only these members (the rest are 'unknown members')."""

    async def fetch_member(_id: int):
        response = SimpleNamespace(status=404, reason="Not Found")
        raise discord.NotFound(response, "Unknown Member")  # type: ignore[arg-type]

    return SimpleNamespace(
        get_member=lambda i: SimpleNamespace(display_avatar=avatars[i]) if i in avatars else None,
        fetch_member=fetch_member,
    )


def members(avatars: dict[int, FakeAsset]) -> tuple[Any, Any]:
    return (SimpleNamespace(display_avatar=avatars[1]), SimpleNamespace(display_avatar=avatars[2]))


async def test_the_thumbnail_is_half_of_each_avatar_attached_as_a_file():
    url, file = await PairAvatars().thumbnail(*members(avatars_of([])))
    assert url == ATTACHMENT_URL == "attachment://pair.png"
    assert file is not None and file.filename == "pair.png"
    picture = decode(file.fp.read())
    assert picture.size == (256, 256)
    assert picture.getpixel((10, 128)) == RED  # the first one's left half
    assert picture.getpixel((246, 128)) == YELLOW  # the second one's right half


async def test_the_picture_is_made_once_and_every_message_gets_its_own_file():
    reads: list[str] = []
    pair, first = PairAvatars(), members(avatars_of(reads))
    _, one = await pair.thumbnail(*first)
    _, two = await pair.thumbnail(*first)
    assert sorted(reads) == ["https://cdn.invalid/1.png", "https://cdn.invalid/2.png"]
    assert one is not two  # a sent file is used up: never hand the same one out twice
    assert one is not None and two is not None and one.fp.read() == two.fp.read()


async def test_a_new_profile_picture_makes_a_new_thumbnail():
    reads: list[str] = []
    avatars = avatars_of(reads)
    pair = PairAvatars()
    await pair.thumbnail(*members(avatars))
    avatars[2] = FakeAsset("https://cdn.invalid/2-new.png", two_tone(RED, BLUE), reads)
    _, file = await pair.thumbnail(*members(avatars))
    assert "https://cdn.invalid/2-new.png" in reads
    assert file is not None and decode(file.fp.read()).getpixel((246, 128)) == BLUE


async def test_the_cache_does_not_grow_without_limit():
    pair = PairAvatars(cache_size=2)
    first, second = members(avatars_of([]))
    for i in range(5):
        second.display_avatar.url = f"https://cdn.invalid/{i}.png"
        await pair.thumbnail(first, second)
    assert len(pair._cache) == 2


async def test_bytes_that_are_not_an_image_fall_back_to_one_avatar_and_no_file():
    avatars = avatars_of([])
    avatars[2].data = b"<html>not a picture</html>"
    url, file = await PairAvatars().thumbnail(*members(avatars))
    assert url == "https://cdn.invalid/1.png" and file is None


async def test_a_slow_cdn_does_not_make_a_command_miss_discords_deadline():
    url, file = await PairAvatars(wait_s=0.01).thumbnail(*members(avatars_of([], delay=5)))
    assert url == "https://cdn.invalid/1.png" and file is None


async def test_a_member_out_of_reach_falls_back_to_whoever_can_be_found():
    avatars = avatars_of([])
    first, second = members(avatars)
    assert await PairAvatars().thumbnail(first, None) == ("https://cdn.invalid/1.png", None)
    assert await PairAvatars().thumbnail(None, second) == (None, None)
    assert await PairAvatars().thumbnail(None, None) == (None, None)
