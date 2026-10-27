"""The /profile view embed. Pure: icons, names and the avatar are passed in.

Order: the nickname (large) with the Discord avatar, then the scrim numbers (rank with its bar,
record, KDA), the scrim record in all five roles, the scrim champions (role + champion), the roles
the player is comfortable on, and last the League reference (rank, linked accounts, Solo/Duo
champion pool). The side bar has the colour of the player's League Solo/Duo tier. No footer: the
group knows this is not Riot's.
"""

from collections.abc import Mapping

import discord

from bot.features.icons.roles import role_label
from bot.features.matches.repository import ROLE_ORDER
from bot.features.players.service import Profile
from bot.features.players.text import progress_bar, tier_color
from bot.features.ratings.ladder import LadderConfig
from bot.features.stats.render import (
    champion_label,
    champion_role_line,
    escape,
    kda_average,
    tally_line,
)
from bot.features.stats.service import PlayerStats, Tally

GROUP_TITLE = "Scrims"
NOT_REGISTERED = "KD Scrims: Jogador não registado. Usa /register primeiro."


def in_house_rank_text(profile: Profile, ladder: LadderConfig) -> str:
    standing = profile.standing
    if not standing.placed:
        return f"Sem rank (colocação {standing.games}/{ladder.placement_games})"
    label = ladder.label(standing.tier_key or "", standing.division)
    # The top step has nothing to climb to: it shows a full bar instead of none.
    bar = progress_bar(1.0 if standing.progress is None else standing.progress)
    return f"{label} {bar}".strip()


def league_rank_text(profile: Profile) -> str:
    rank = profile.resolution.rank
    if rank is None:
        return "A aguardar atualização" if profile.resolution.riot_pending else "—"
    return rank.label()


def roles_text(profile: Profile, roles: Mapping[str, str]) -> str:
    """`<icon> 5/5 · <icon> 3/5 ...`: the icon alone (the role's name only if it has no icon)."""
    parts = []
    for line in profile.roles:
        value = line.proficiency.value
        parts.append(f"{role_label(line.role, roles)} {'—' if value is None else f'{value}/5'}")
    return " · ".join(parts)


def build_profile_embed(
    profile: Profile,
    ladder: LadderConfig,
    roles: Mapping[str, str] | None = None,
    stats: PlayerStats | None = None,
    champion_icons: Mapping[str, str] | None = None,
    champion_names: Mapping[str, str] | None = None,
    avatar_url: str | None = None,
) -> discord.Embed:
    roles = roles or {}
    icons = champion_icons or {}
    names = champion_names or {}
    embed = discord.Embed(
        description=f"# {escape(profile.player.nickname)}",
        color=tier_color(profile.resolution.rank),
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    house = [f"Rank: {in_house_rank_text(profile, ladder)}"]
    if stats is not None and stats.overall.games:
        house.append(tally_line(stats.overall))
        house.append(f"KDA: {kda_average(stats.kda)}")
    embed.add_field(name=GROUP_TITLE, value="\n".join(house), inline=False)
    if stats is not None:
        record = {item.key[0]: item.tally for item in stats.by_role}
        embed.add_field(
            name="Win Rate Scrims",
            value=" · ".join(
                f"{role_label(role, roles)} {record.get(role, Tally()).wins}V/"
                f"{record.get(role, Tally()).losses}D"
                for role in ROLE_ORDER
            ),
            inline=False,
        )
    if stats is not None and stats.by_champion_role:
        embed.add_field(
            name="Campeões Scrims",
            value="\n".join(
                champion_role_line(item, roles, icons, names) for item in stats.by_champion_role
            ),
            inline=False,
        )

    embed.add_field(name="Roles", value=roles_text(profile, roles), inline=False)

    embed.add_field(name="Rank League", value=league_rank_text(profile), inline=True)
    if profile.accounts:
        embed.add_field(name="Contas Riot", value="\n".join(profile.accounts), inline=True)
    if profile.champion_pool:
        embed.add_field(
            name="Campeões Solo/Duo",
            value=" ".join(
                champion_label(champion, icons, names) for champion, _ in profile.champion_pool
            ),
            inline=True,
        )
    return embed
