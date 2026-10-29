"""The post-game card: one embed for one finished ranked Solo/Duo game. Pure: everything it shows
(names, icons, the rank after the game, the LP change) is passed in.

    ┃ [avatar] Nickname (Name#TAG)                                  [champion]
    ┃ Ganhou 27 LP (Solo/Duo)
    ┃ [role] **Yone**
    ┃ [tier] MASTER · 790 LP
    ┃ 🔥 Quadra kill
    ┃ **KDA** 8/3/11 (6.3)   **Duração** 30:28   **Participação** 76%
    ┃ **CS** 212 (7.0/min)   **Dano** 24.1k (791/min)   **Visão** 38 (1.25/min)
    ┃ **Ouro** 14.2k         **Pings** 9
    ┃ **Stats @ 15 (vs Zed)**
    ┃ **CS** -1 · **Ouro** +146 · **XP** +380 · **Primeiro nível 2:** Não
    ┃ Solo/Duo · 8028021803

The side bar colour is the result. A stat Riot did not send is left out, never shown as 0.
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

LANE_ROLES = ("TOP", "MID", "ADC")  # where a CS difference means something
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


def title_text(game: RankedGame, lp_delta: int | None) -> str:
    """`Ganhou 27 LP (Solo/Duo)`; the number is left out when the LP change is unknown, and a
    remake never has one."""
    if is_remake(game):
        return "Remake (Solo/Duo)"
    if lp_delta:  # not None and not 0
        return f"{'Ganhou' if lp_delta > 0 else 'Perdeu'} {abs(lp_delta)} LP (Solo/Duo)"
    return f"{'Ganhou' if game.win else 'Perdeu'} (Solo/Duo)"


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


def laning_text(laning: Laning, role: str | None) -> str:
    parts = []
    if role in LANE_ROLES:
        parts.append(f"**CS** {signed(laning.cs_diff)}")
    parts += [f"**Ouro** {signed(laning.gold_diff)}", f"**XP** {signed(laning.xp_diff)}"]
    if laning.first_lvl2 is not None:
        parts.append(f"**Primeiro nível 2:** {'Sim' if laning.first_lvl2 else 'Não'}")
    return " · ".join(parts)


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
        fields.append(("Ouro", format_count(game.gold)))
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
        label = rank_label(card.rank.tier, card.rank.division, card.rank.lp)
        lines.append(f"{icon} {label}".strip())
    if (kill := best_multikill(game)) is not None:
        lines.append(kill)
    embed = discord.Embed(
        title=title_text(game, card.lp_delta),
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
    if card.laning is not None:
        name = f"Stats @ 15 (vs {card.opponent_name})" if card.opponent_name else "Stats @ 15"
        embed.add_field(name=name, value=laning_text(card.laning, role), inline=False)
    # The numeric id only: everybody here plays on the same region.
    embed.set_footer(text=f"Solo/Duo · {game.match_id.rsplit('_', 1)[-1]}")
    return embed
