"""Embeds for /history, /stats and /leaderboard. Pure: icons and names are passed in.

Every icon falls back to its name, so a missing upload never leaves a bare line. No raw rating is
shown anywhere; the ladder board shows the visible tier and division with a progress bar toward
the next division, which is also what orders players inside the same division.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime

import discord

from bot.features.icons.roles import role_label
from bot.features.matches.repository import ROLE_ORDER
from bot.features.matches.service import kda_text
from bot.features.players.repository import Player
from bot.features.players.text import progress_bar
from bot.features.ratings.ladder import LadderConfig
from bot.features.riot.history import format_ago
from bot.features.stats.service import (
    KDA,
    MIN_BOARD_GAMES,
    RANK,
    WIN_RATE,
    BoardEntry,
    Breakdown,
    History,
    Kda,
    PlayerStats,
    Tally,
)

MAX_BOARD_ROWS = 30  # keeps the description far below Discord's 4096 characters
NO_GAMES = "Ainda não há jogos confirmados."
BOARD_TITLES = {
    RANK: "🏆 Rank Scrims",
    WIN_RATE: "📈 Win Rate Scrims",
    KDA: "⚔️ KDA Scrims",
}
BOARD_RULES = {
    RANK: "só quem terminou a colocação",
    WIN_RATE: f"mín. {MIN_BOARD_GAMES} jogos",
    KDA: f"mín. {MIN_BOARD_GAMES} jogos com stats validadas",
}
BOARD_COLORS = {
    RANK: discord.Color.gold(),
    WIN_RATE: discord.Color.green(),
    KDA: discord.Color.red(),
}
BOARD_EMPTY = {
    RANK: "Ainda ninguém terminou a colocação.",
    WIN_RATE: f"Ainda ninguém tem {MIN_BOARD_GAMES} jogos.",
    KDA: f"Ainda ninguém tem {MIN_BOARD_GAMES} jogos com stats validadas.",
}
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def escape(text: str) -> str:
    return discord.utils.escape_markdown(text)


def champion_label(champion: str, icons: Mapping[str, str], names: Mapping[str, str]) -> str:
    """The champion's icon, else its name in bold (the Data Dragon id if the list lacks it)."""
    return icons.get(champion) or f"**{escape(names.get(champion, champion))}**"


def games_text(count: int) -> str:
    return f"{count} jogo" if count == 1 else f"{count} jogos"


def tally_line(tally: Tally) -> str:
    """`Jogos: 10V 8D (56%) · 18 jogos`."""
    rate = tally.win_rate
    percent = "—" if rate is None else f"{rate * 100:.0f}%"
    return f"Jogos: {tally.wins}V {tally.losses}D ({percent}) · {games_text(tally.games)}"


def kda_average(kda: Kda | None) -> str:
    """`4.84 (6.4/3.7/11.5)` over every game with validated stats, or a dash when there is none."""
    if kda is None:
        return "—"
    kills, deaths, assists = kda.average
    return f"{kda.ratio:.2f} ({kills:.1f}/{deaths:.1f}/{assists:.1f})"


def breakdown_line(prefix: str, item: Breakdown) -> str:
    """`<prefix> 2V 0D · KDA 6.50`; the KDA is left out when no game has validated stats."""
    text = f"{prefix} {item.tally.wins}V {item.tally.losses}D"
    return text if item.kda is None else f"{text} · KDA {item.kda.ratio:.2f}"


def champion_role_line(
    item: Breakdown,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> str:
    """`<role> <champion> 2V 0D · KDA 6.50`; the key is (champion, role)."""
    champion, role = item.key
    prefix = f"{role_label(role, roles)} {champion_label(champion, champion_icons, champion_names)}"
    return breakdown_line(prefix, item)


def _pending_note(pending: int) -> str:
    plural = "s" if pending != 1 else ""
    return f"⏳ {pending} jogo{plural} por validar (ainda não contam)."


def _heading(title: str, player: Player) -> str:
    return f"# {title} · {escape(player.nickname)}"


def _card(color: discord.Color | None, avatar_url: str | None) -> discord.Embed:
    embed = discord.Embed(color=color or discord.Color.blurple())
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


def build_history_embed(
    history: History,
    now: datetime,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
    avatar_url: str | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Large heading with the player's avatar, the overall record, then one line per game."""
    embed = _card(color, avatar_url)
    heading = _heading("Histórico", history.player)
    if not history.lines:
        embed.description = f"{heading}\n\n{NO_GAMES}"
        return embed
    lines = []
    for game in history.lines:
        result = "✅" if game.won else "❌"
        parts = [f"{result} {role_label(game.role, roles)}"]
        if game.champion is not None:
            champion = champion_label(game.champion, champion_icons, champion_names)
            parts.append(f"{champion} {kda_text(game.kills, game.deaths, game.assists)}")
        else:
            parts.append("stats por validar ⏳" if game.pending else "sem stats")
        parts += [format_ago(game.confirmed_at, now), f"#{game.match_id}"]
        lines.append(" · ".join(parts))
    pending = sum(g.pending for g in history.lines)
    cut = (
        [f"Últimos {len(history.lines)} jogos:"]
        if len(history.lines) < history.overall.games
        else []
    )
    embed.description = "\n".join(
        [heading, "", tally_line(history.overall), "", *cut, *lines]
        + ([_pending_note(pending)] if pending else [])
    )
    return embed


def build_stats_embed(
    stats: PlayerStats,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
    avatar_url: str | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    """The profile's scrim numbers in full: all five roles, champions, champion x role."""
    embed = _card(color, avatar_url)
    heading = _heading("Estatísticas", stats.player)
    if stats.overall.games == 0:
        embed.description = f"{heading}\n\n{NO_GAMES}"
        return embed
    lines = [heading, "", tally_line(stats.overall), f"KDA: {kda_average(stats.kda)}"]
    if stats.pending:
        lines.append(_pending_note(stats.pending))
    embed.description = "\n".join(lines)
    played = {item.key[0]: item for item in stats.by_role}
    embed.add_field(
        name="Por role",
        value="\n".join(
            breakdown_line(
                role_label(role, roles), played.get(role, Breakdown((role,), Tally(), None))
            )
            for role in ROLE_ORDER
        ),
        inline=False,
    )
    if stats.by_champion:
        embed.add_field(
            name="Campeões Scrims",
            value="\n".join(
                breakdown_line(champion_label(item.key[0], champion_icons, champion_names), item)
                for item in stats.by_champion
            ),
            inline=False,
        )
        embed.add_field(
            name="Campeões Scrims por role",
            value="\n".join(
                champion_role_line(item, roles, champion_icons, champion_names)
                for item in stats.by_champion_role
            ),
            inline=False,
        )
    return embed


def board_line(board: str, entry: BoardEntry, ladder: LadderConfig) -> str:
    """`🥇 **Name** · <what the board ranks> · <context>`; ties share the medal."""
    place = MEDALS.get(entry.position, f"{entry.position}.")
    name = f"**{escape(entry.player.nickname)}**"
    if board == RANK:
        label = ladder.label(entry.tier_key or "", entry.division)
        bar = progress_bar(entry.progress)
        return f"{place} {name} · {label} {bar} · {entry.tally.wins}V {entry.tally.losses}D"
    if board == WIN_RATE:
        rate = entry.tally.win_rate
        percent = "—" if rate is None else f"{rate * 100:.0f}%"
        record = f"{entry.tally.wins}V {entry.tally.losses}D"
        return f"{place} {name} · {percent} · {record} · {games_text(entry.tally.games)}"
    games = entry.kda.games if entry.kda else 0
    return f"{place} {name} · {kda_average(entry.kda)} · {games_text(games)}"


def build_board_embed(
    board: str,
    entries: Sequence[BoardEntry],
    ladder: LadderConfig,
    leader_avatar_url: str | None = None,
) -> discord.Embed:
    """Large heading, a one-line rule, medals on the podium; the leader's avatar is the picture."""
    embed = discord.Embed(color=BOARD_COLORS[board])
    heading = f"# {BOARD_TITLES[board]}"
    if not entries:
        embed.description = f"{heading}\n\n{BOARD_EMPTY[board]}"
        return embed
    if leader_avatar_url:
        embed.set_thumbnail(url=leader_avatar_url)
    lines = [board_line(board, entry, ladder) for entry in entries[:MAX_BOARD_ROWS]]
    if len(entries) > MAX_BOARD_ROWS:
        lines.append(f"… e mais {len(entries) - MAX_BOARD_ROWS}.")
    rule = f"-# {len(entries)} jogadores · {BOARD_RULES[board]}"
    embed.description = "\n".join([heading, rule, "", *lines])
    return embed
