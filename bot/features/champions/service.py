"""Champion lookup for the stats commands, and keeping the cached list current."""

import logging
import re
import unicodedata
from collections.abc import Mapping

from bot.features.champions.client import (
    Champion,
    DataDragonError,
    GetBytes,
    GetJson,
    aiohttp_get_bytes,
    aiohttp_get_json,
    fetch_champions,
    icon_url,
)
from bot.features.champions.repository import ChampionRepository
from bot.features.icons.upload import IconUploadError, UploadIcon

log = logging.getLogger(__name__)

EMOJI_PREFIX = "champ_"  # application emoji names: letters, digits and underscores only
MAX_ICON_FAILURES = 3  # consecutive failed uploads before a sync gives up until the next run


def emoji_name(champion_id: str) -> str:
    return EMOJI_PREFIX + re.sub(r"[^A-Za-z0-9_]", "_", champion_id)


def normalise(text: str) -> str:
    """Lower-case letters and digits only: "Kai'Sa", "kaisa" and "KAI SA" are the same champion."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", folded.lower())


class ChampionService:
    def __init__(
        self,
        repo: ChampionRepository,
        get_json: GetJson = aiohttp_get_json,
        get_bytes: GetBytes = aiohttp_get_bytes,
        icons: bool = True,
    ) -> None:
        self.repo = repo
        self.get_json = get_json
        self.get_bytes = get_bytes
        self.icons = icons  # show (and upload) champion icons at all
        # (normalised name, normalised id, champion), rebuilt after every change to the table
        self._cache: list[tuple[str, str, Champion]] | None = None

    async def _champions(self) -> list[tuple[str, str, Champion]]:
        if self._cache is None:
            self._cache = [(normalise(c.name), normalise(c.id), c) for c in await self.repo.all()]
        return self._cache

    async def start(self) -> None:
        """Make sure there is a list to work with, even before the first successful refresh."""
        if await self.repo.seed_if_empty():
            self._cache = None

    async def refresh(self) -> bool:
        """Fetch the latest patch's list; True if the cache changed. Raises DataDragonError."""
        latest = await fetch_champions(self.get_json)
        if latest.patch == await self.repo.current_patch():
            return False
        await self.repo.replace_all(latest)
        self._cache = None
        log.info(
            "Champion list updated to patch %s (%d champions)", latest.patch, len(latest.champions)
        )
        return True

    async def try_refresh(self) -> bool:
        """`refresh`, but a failure is only logged: the cached list keeps serving."""
        try:
            return await self.refresh()
        except DataDragonError as exc:
            log.warning("Could not refresh the champion list: %s", exc)
            return False

    async def find(self, text: str) -> Champion | None:
        """The champion a player typed: by id or name, ignoring case, spaces and punctuation."""
        wanted = normalise(text)
        if not wanted:
            return None
        for name, champion_id, champion in await self._champions():
            if wanted in (name, champion_id):
                return champion
        return None

    async def suggest(self, text: str, limit: int = 25) -> list[Champion]:
        """Autocomplete: names starting with the text first, then names containing it."""
        wanted = normalise(text)
        champions = await self._champions()
        if not wanted:
            return [c for _, _, c in champions[:limit]]
        starts, contains = [], []
        for name, champion_id, champion in champions:
            if name.startswith(wanted) or champion_id.startswith(wanted):
                starts.append(champion)
            elif wanted in name or wanted in champion_id:
                contains.append(champion)
        return (starts + contains)[:limit]

    async def names(self, ids: list[str]) -> dict[str, str]:
        return await self.repo.names(ids)

    # --- icons (application emojis) ----

    async def icon_markup(self, ids: list[str]) -> dict[str, str]:
        """Emoji markup for the champions whose icon is uploaded (empty when icons are off)."""
        return await self.repo.emojis(ids) if self.icons else {}

    async def sync_icons(self, existing: Mapping[str, int], upload: UploadIcon) -> int:
        """Upload the icons that are missing; returns how many were uploaded.

        `existing` is the application's emojis by name, so icons uploaded before (or by a run that
        was cut short) are adopted instead of duplicated. A failure skips that champion; after
        `MAX_ICON_FAILURES` in a row the run stops (probably a limit or an outage) and the next
        run picks up where this one left.
        """
        patch = await self.repo.current_patch()
        if not self.icons or patch is None:
            return 0
        done = await self.repo.uploaded()
        uploaded = failures = 0
        for champion in await self.repo.all():
            if champion.id in done:
                continue
            name = emoji_name(champion.id)
            if name in existing:
                await self.repo.save_emoji(champion.id, existing[name], name)
                continue
            try:
                image = await self.get_bytes(icon_url(patch, champion.id))
                emoji_id = await upload(name, image)
            except (DataDragonError, IconUploadError) as exc:
                failures += 1
                log.warning("Could not upload the icon of %s: %s", champion.id, exc)
                if failures >= MAX_ICON_FAILURES:
                    log.warning("Stopping the icon upload; it resumes at the next refresh.")
                    break
                continue
            failures = 0
            await self.repo.save_emoji(champion.id, emoji_id, name)
            uploaded += 1
        if uploaded:
            log.info("Uploaded %d champion icons", uploaded)
        return uploaded
