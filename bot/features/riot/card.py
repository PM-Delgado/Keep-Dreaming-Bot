"""The post-game card: one embed for one finished ranked Solo/Duo game. Pure: everything it shows
(names, icons, the rank after the game, the LP change) is passed in.

    ┃ [avatar] Nickname (Name#TAG)                                  [champion]
    ┃ Ganhou (Solo/Duo)
    ┃ [role] **Yone**
    ┃ [tier] MASTER · 790 LP (📈 **+27**)
    ┃ 🔥 Quadra kill
    ┃ **KDA** 8/3/11 (6.3)   **Duração** 30:28   **Participação** 76%
    ┃ **CS** 212 (7.0/min)   **Dano** 24.1k (791/min)   **Visão** 38 (1.25/min)
    ┃ **Gold** 14.2k         **Pings** 9              (blank: keeps the three columns)
    ┃ ──────────────────────────────────────
    ┃ **Stats @ 15 (vs Zed)**
    ┃ **CS** -1 | **Gold** +146 | **XP** +380 | **Nível 2 primeiro** ❌
    ┃ Solo/Duo

The side bar colour is the result. A stat Riot did not send is left out, never shown as 0.

Discord has no divider element and no fixed card width: an embed is as wide as its widest line.
The divider is a line of box-drawing characters in the name of the last field; being that wide it
also sets the card's width, so every card with a laning block has the same width. The stat fields
are padded with blank ones to a multiple of three, or the last row would be two wide columns that
do not line up with the three above.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import discord

from bot.features.icons.roles import role_label
from bot.features.riot.client import Laning, RankedGame, SoloRank
from bot.features.riot.history import format_count
from bot.features.riot.leaderboard import TIER_DOTS
from bot.features.riot.lp import rank_label
from bot.features.riot.roles import RoleHistoryParams, position_role

BLANK = "​"  # a zero-width space: Discord refuses an empty field name or value
DIVIDER = "─" * 38  # the width in characters is a guess to tune on a real client
COLUMNS = 3  # inline fields per row
# Highest first: the card celebrates the best multikill only.
MULTIKILLS = (
    ("penta_kills", "Penta kill"),
    ("quadra_kills", "Quadra kill"),
    ("triple_kills", "Triple kill"),
    ("double_kills", "Double kill"),
)


@dataclass(frozen=True)
class CardData:
    nickname: str
    riot_id: str
    game: RankedGame
    champion_name: str
    laning: Laning | None = None
    opponent_name: str | None = None  # the lane opponent's champion, for "(vs Zed)"
    opponent_icon: str = ""  # its emoji markup, shown before the name; empty = name only
    rank: SoloRank | None = None  # the rank after the game
    lp_delta: int | None = None  # None = unknown (never guessed)
    champion_icon_url: str | None = None
    avatar_url: str | None = None


def is_remake(game: RankedGame) -> bool:
    return game.early_surrender or game.duration_s < RoleHistoryParams().min_duration_s


def result_color(game: RankedGame) -> discord.Color:
    if is_remake(game):
        return discord.Color.light_grey()
    return discord.Color.green() if game.win else discord.Color.red()


def title_text(game: RankedGame) -> str:
    """`Ganhou (Solo/Duo)`, `Perdeu (Solo/Duo)` or `Remake (Solo/Duo)`: the result only, the LP
    change is next to the rank."""
    if is_remake(game):
        return "Remake (Solo/Duo)"
    return f"{'Ganhou' if game.win else 'Perdeu'} (Solo/Duo)"


def rank_text(rank: SoloRank, lp_delta: int | None, icon: str = "") -> str:
    """`[icon] GOLD I · 29 LP (📉 **-21**)`: the LP the game changed in brackets, with an arrow
    and in bold so it stands out. Left out when unknown (None) or zero, never guessed."""
    text = f"{icon} {rank_label(rank.tier, rank.division, rank.lp)}".strip()
    if lp_delta:
        arrow = "📈" if lp_delta > 0 else "📉"
        text += f" ({arrow} **{lp_delta:+d}**)"
    return text


def duration_text(seconds: int) -> str:
    return f"{max(seconds, 0) // 60}:{max(seconds, 0) % 60:02d}"


def best_multikill(game: RankedGame) -> str | None:
    for attribute, label in MULTIKILLS:
        count = getattr(game, attribute) or 0
        if count > 0:
            return f"🔥 {label}" if count == 1 else f"🔥 {count}× {label}"
    return None


def signed(value: int) -> str:
    return f"{value:+d}"


LANING_SEPARATOR = " | "


def laning_text(laning: Laning) -> str:
    """`**CS** -1 | **Gold** +146 | **XP** +380 | **Nível 2 primeiro** ❌`: every stat is a bold
    label and its value, with no colon on any of them; the yes/no one is a ✅ or a ❌."""
    parts = [
        f"**CS** {signed(laning.cs_diff)}",
        f"**Gold** {signed(laning.gold_diff)}",
        f"**XP** {signed(laning.xp_diff)}",
    ]
    if laning.first_lvl2 is not None:
        parts.append(f"**Nível 2 primeiro** {'✅' if laning.first_lvl2 else '❌'}")
    return LANING_SEPARATOR.join(parts)


def stat_fields(game: RankedGame) -> list[tuple[str, str]]:
    """The (name, value) pairs in display order; a stat Riot did not send is skipped."""
    minutes = game.duration_s / 60 if game.duration_s > 0 else None
    kda = (game.kills + game.assists) / max(game.deaths, 1)
    fields = [
        ("KDA", f"{game.kills}/{game.deaths}/{game.assists} ({kda:.1f})"),
        ("Duração", duration_text(game.duration_s)),
    ]
    if game.team_kills:
        fields.append(("Participação", f"{(game.kills + game.assists) / game.team_kills:.0%}"))
    if game.cs is not None:
        per_min = f" ({game.cs / minutes:.1f}/min)" if minutes else ""
        fields.append(("CS", f"{game.cs}{per_min}"))
    if game.damage is not None:
        value = format_count(game.damage)
        if minutes:
            value += f" ({game.damage / minutes:.0f}/min)"
        if game.team_damage:
            value += f"\n{game.damage / game.team_damage:.0%} da equipa"
        fields.append(("Dano", value))
    if game.vision_score is not None:
        per_min = f" ({game.vision_score / minutes:.2f}/min)" if minutes else ""
        fields.append(("Visão", f"{game.vision_score}{per_min}"))
    if game.gold is not None:
        fields.append(("Gold", format_count(game.gold)))
    if game.pings is not None:
        fields.append(("Pings", str(game.pings)))
    return fields


def build_card_embed(
    card: CardData,
    role_icons: Mapping[str, str] | None = None,
    tier_icons: Mapping[str, str] | None = None,
) -> discord.Embed:
    game = card.game
    role = position_role(game.team_position)
    lines = []
    head = f"**{discord.utils.escape_markdown(card.champion_name)}**"
    if role:
        head = f"{role_label(role, role_icons)} {head}"
    lines.append(head)
    if card.rank is not None:
        tier = card.rank.tier.upper()
        icon = (tier_icons or {}).get(tier) or TIER_DOTS.get(tier, "")
        lines.append(rank_text(card.rank, None if is_remake(game) else card.lp_delta, icon))
    if (kill := best_multikill(game)) is not None:
        lines.append(kill)
    embed = discord.Embed(
        title=title_text(game),
        description="\n".join(lines),
        color=result_color(game),
        timestamp=game.played_at,
    )
    embed.set_author(
        name=f"{card.nickname} ({card.riot_id})",
        icon_url=card.avatar_url,
    )
    if card.champion_icon_url:
        embed.set_thumbnail(url=card.champion_icon_url)
    for name, value in stat_fields(game):
        embed.add_field(name=name, value=value, inline=True)
    while len(embed.fields) % COLUMNS:
        embed.add_field(name=BLANK, value=BLANK, inline=True)
    if card.laning is not None:
        name = discord.utils.escape_markdown(card.opponent_name or "")
        opponent = f"{card.opponent_icon} {name}".strip()
        title = f"Stats @ 15 (vs {opponent})" if opponent else "Stats @ 15"
        embed.add_field(
            name=DIVIDER, value=f"**{title}**\n{laning_text(card.laning)}", inline=False
        )
    embed.set_footer(text="Solo/Duo")
    return embed
