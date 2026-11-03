"""The picture of two players: the left half of one avatar next to the right half of the other, as
one square PNG (`/riot duo` and `/rivalry`). `split_avatar` is bytes in, bytes out (the only
Pillow import of the bot); `PairAvatars` fetches the two avatars and keeps what it made in memory.
"""

import asyncio
import io
import logging

import discord
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

SIZE = 256  # pixels; the embed thumbnail is shown much smaller, this keeps it sharp
FILENAME = "pair.png"
ATTACHMENT_URL = f"attachment://{FILENAME}"
WAIT_S = 2.0  # Discord wants an answer in 3 s: the picture gets at most this long
CACHE_SIZE = 32  # pictures kept in memory (about 100 KB each at most)


def split_avatar(left: bytes, right: bytes, size: int = SIZE) -> bytes:
    """A `size` x `size` PNG: the left half of `left` and the right half of `right`.

    Each picture is first fitted to a square (centred crop), so a non-square avatar does not
    distort. Raises `OSError` (Pillow's `UnidentifiedImageError` is one) for bytes that are not an
    image."""
    half = size // 2
    with Image.open(io.BytesIO(left)) as a, Image.open(io.BytesIO(right)) as b:
        first = ImageOps.fit(a.convert("RGBA"), (size, size))
        second = ImageOps.fit(b.convert("RGBA"), (size, size))
    canvas = Image.new("RGBA", (size, size))
    canvas.paste(first.crop((0, 0, half, size)), (0, 0))
    canvas.paste(second.crop((half, 0, size, size)), (half, 0))
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


class PairAvatars:
    """Makes the two-player picture for an embed thumbnail.

    The finished pictures are cached by the two avatars' URLs (which change when someone changes
    their picture), so flipping a tab does not download anything again."""

    def __init__(self, wait_s: float = WAIT_S, cache_size: int = CACHE_SIZE) -> None:
        self.wait_s = wait_s
        self.cache_size = cache_size
        self._cache: dict[tuple[str, str], bytes] = {}

    async def thumbnail(
        self, first: discord.Member | None, second: discord.Member | None
    ) -> tuple[str | None, discord.File | None]:
        """(the thumbnail URL for the embed, the file to attach with the message).

        When the picture cannot be made (a member out of reach, Discord's CDN slow or failing,
        bytes that are not an image) it is the first player's avatar link alone and no file, or
        nothing at all: a command never fails over a picture. Attach a fresh file to every
        message, since sending consumes it."""
        if first is None or second is None:
            return (first.display_avatar.url if first else None), None
        try:
            png = await asyncio.wait_for(self._make(first, second), self.wait_s)
        except (TimeoutError, discord.HTTPException, OSError, ValueError) as exc:
            log.info("Pair picture not made (%s): showing one avatar", type(exc).__name__)
            return first.display_avatar.url, None
        return ATTACHMENT_URL, discord.File(io.BytesIO(png), filename=FILENAME)

    async def _make(self, first: discord.Member, second: discord.Member) -> bytes:
        a = first.display_avatar.replace(size=SIZE, format="png")
        b = second.display_avatar.replace(size=SIZE, format="png")
        key = (a.url, b.url)
        if (cached := self._cache.get(key)) is not None:
            return cached
        left, right = await asyncio.gather(a.read(), b.read())
        png = await asyncio.to_thread(split_avatar, left, right)
        self._cache[key] = png
        while len(self._cache) > self.cache_size:
            del self._cache[next(iter(self._cache))]  # the oldest
        return png
