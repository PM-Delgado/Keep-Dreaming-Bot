"""Small text and colour pieces shared by the embeds that show ranks (profile, history, stats)."""

import discord

from bot.features.ratings.rank import ResolvedRank

DEFAULT_COLOR = discord.Color.blurple()
# League's own tier colours (the client's rank emblems), keyed by the stored tier.
TIER_COLORS = {
    "IRON": 0x51484A,
    "BRONZE": 0x8C513A,
    "SILVER": 0x80989D,
    "GOLD": 0xCD8837,
    "PLATINUM": 0x4E9996,
    "EMERALD": 0x2D9A5B,
    "DIAMOND": 0x576BCE,
    "MASTER": 0x9D5CC9,
    "GRANDMASTER": 0xCD4545,
    "CHALLENGER": 0xF4C874,
}


def tier_color(rank: ResolvedRank | None) -> discord.Color:
    """The side-bar colour of a League Solo/Duo tier; blurple without a rank."""
    value = TIER_COLORS.get(rank.tier.upper()) if rank else None
    return DEFAULT_COLOR if value is None else discord.Color(value)


def progress_bar(progress: float | None, width: int = 10) -> str:
    if progress is None:
        return ""
    filled = round(progress * width)
    return "▰" * filled + "▱" * (width - filled)
