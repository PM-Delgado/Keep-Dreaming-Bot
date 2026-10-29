"""The daily Solo/Duo leaderboard of the players' main accounts: rows, trend, pages, embed.

Pure: the repository supplies `MainRank`s and the previous day's snapshots; nothing here touches
Discord, the database or Riot. It shows Riot's own tier, division and LP, never an in-house rating.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from bot.features.players.text import TIER_COLORS
from bot.features.ratings.rank import rank_points
from bot.features.riot.lp import ladder_points, rank_label

LISBON = ZoneInfo("Europe/Lisbon")
PAGE_SIZE = 10
TITLE = "🏆 Classificação Solo/Duo"
UNRANKED = "Sem rank"
NO_PLAYERS = "Ainda não há jogadores com conta Riot ligada."
FOOTER_PAGE = re.compile(r"^Página (\d+)/(\d+)")
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
# Generic dots in the colour of each tier (not Riot's emblems, which the policy leaves out).
TIER_DOTS = {
    "IRON": "⚫",
    "BRONZE": "🟤",
    "SILVER": "⚪",
    "GOLD": "🟡",
    "PLATINUM": "🔵",
    "EMERALD": "🟢",
    "DIAMOND": "🔷",
    "MASTER": "🟣",
    "GRANDMASTER": "🔴",
    "CHALLENGER": "👑",
}


@dataclass(frozen=True)
class MainRank:
    """A player's main account and the last Solo/Duo rank saved for it (tier None = unranked or
    not looked up yet)."""

    account_id: int
    nickname: str
    riot_id: str
    tier: str | None
    division: str | None
    lp: int | None

    @property
    def points(self) -> int | None:
        if self.tier is None or self.lp is None:
            return None
        try:
            return ladder_points(self.tier, self.division, self.lp)
        except ValueError:  # a tier we don't map: listed as unranked rather than failing the post
            return None


@dataclass(frozen=True)
class Row:
    position: int
    main: MainRank
    trend: int | None  # LP since the previous snapshot; None = no snapshot to compare with


def _sort_key(main: MainRank) -> tuple[int, int, int, str]:
    points = main.points
    if points is None or main.tier is None:
        return (1, 0, 0, main.nickname.casefold())
    # Most LP first; on equal LP the higher tier (a Master and a Grandmaster at 0 LP).
    return (0, -points, -rank_points(main.tier, main.division), main.nickname.casefold())


def build_rows(mains: Sequence[MainRank], previous: Mapping[int, int]) -> list[Row]:
    """Ranked players by LP (highest first), then the unranked ones by name.

    `previous` maps an account id to its ladder points in the last earlier snapshot.
    """
    rows: list[Row] = []
    for position, main in enumerate(sorted(mains, key=_sort_key), start=1):
        points, before = main.points, previous.get(main.account_id)
        trend = None if points is None or before is None else points - before
        rows.append(Row(position, main, trend))
    return rows


def page_count(rows: Sequence[Row]) -> int:
    return max(1, -(-len(rows) // PAGE_SIZE))


def trend_text(trend: int | None) -> str:
    """`📈 +27` / `📉 -20`; nothing when unchanged or unknown, so a quiet day has no clutter."""
    if not trend:
        return ""
    return f"📈 +{trend}" if trend > 0 else f"📉 {trend}"


def row_text(row: Row, tier_icons: Mapping[str, str] | None = None) -> str:
    """Two lines per player, always: the first never grows with the trend, the second is small
    text, so no row can wrap into a third line.

        🥇 **Brunão** · 🟣 MASTER · 689 LP
        -# thisisvaldo#EUW · 📈 +27

    The tier icon is the uploaded emblem when there is one, else a generic colour dot.
    """
    main = row.main
    place = MEDALS.get(row.position, f"**{row.position}**")
    name = discord.utils.escape_markdown(main.nickname)
    if main.points is None or main.tier is None or main.lp is None:
        first = f"{place} **{name}** · {UNRANKED}"
    else:
        tier = main.tier.upper()
        dot = (tier_icons or {}).get(tier) or TIER_DOTS.get(tier, "")
        rank = rank_label(main.tier, main.division, main.lp)
        first = f"{place} **{name}** · {f'{dot} ' if dot else ''}{rank}"
    second = " · ".join(
        part
        for part in (discord.utils.escape_markdown(main.riot_id), trend_text(row.trend))
        if part
    )
    return f"{first}\n-# {second}"


def highlights(rows: Sequence[Row]) -> str:
    """The day's biggest climb and fall, when somebody moved."""
    moved = [r for r in rows if r.trend]
    if not moved:
        return ""
    best = max(moved, key=lambda r: r.trend or 0)
    worst = min(moved, key=lambda r: r.trend or 0)
    parts = []
    if (best.trend or 0) > 0:
        name = discord.utils.escape_markdown(best.main.nickname)
        parts.append(f"🔥 Maior subida: **{name}** +{best.trend}")
    if (worst.trend or 0) < 0:
        name = discord.utils.escape_markdown(worst.main.nickname)
        parts.append(f"🧊 Maior queda: **{name}** {worst.trend}")
    return " · ".join(parts)


def updated_text(moment: datetime, now: datetime) -> str:
    local, today = moment.astimezone(LISBON), now.astimezone(LISBON)
    if local.date() == today.date():
        return f"hoje às {local:%H:%M}"
    return f"{local:%d/%m} às {local:%H:%M}"


def build_embed(
    rows: Sequence[Row],
    page: int,
    updated: datetime,
    now: datetime,
    tier_icons: Mapping[str, str] | None = None,
) -> discord.Embed:
    """One page (1-based; an out-of-range page is clamped) of the leaderboard. The side bar has
    the colour of the leader's tier."""
    pages = page_count(rows)
    page = min(max(page, 1), pages)
    chunk = rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    lines = [row_text(r, tier_icons) for r in chunk]
    top = highlights(rows) if page == 1 else ""
    description = "\n".join([*([top, ""] if top else []), *lines]) if lines else NO_PLAYERS
    leader = rows[0].main.tier if rows else None
    embed = discord.Embed(
        title=TITLE,
        description=description,
        color=discord.Color(TIER_COLORS[leader.upper()])
        if leader and leader.upper() in TIER_COLORS
        else discord.Color.gold(),
    )
    legend = " · 📈📉 = LP desde ontem" if any(r.trend for r in rows) else ""
    when = updated_text(updated, now)
    embed.set_footer(text=f"Página {page}/{pages} · Atualizado {when}{legend}")
    return embed


def page_of_footer(text: str | None) -> int:
    """The page a leaderboard message currently shows (1 when the footer can't be read)."""
    match = FOOTER_PAGE.match(text or "")
    return int(match.group(1)) if match else 1


def next_page(current: int, pages: int, step: int) -> int:
    """Wraps around: ▶ on the last page goes back to the first, ◀ on the first to the last."""
    return (current - 1 + step) % pages + 1
