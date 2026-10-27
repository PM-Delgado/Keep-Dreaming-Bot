"""Embeds for /history, /stats and /leaderboard. Pure: icons and names are passed in.

Every icon falls back to its name, so a missing upload never leaves a bare line. No rating (raw
or as a progress bar) is shown anywhere; the ladder board shows the visible tier and division.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime

import discord

from bot.features.icons.roles import role_label
from bot.features.lobby.render import SIDE_EMOJI
from bot.features.matches.service import kda_text
from bot.features.players.cog import FOOTER
from bot.features.players.proficiency import ROLE_NAMES_PT
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
    RANK: "Ranking do grupo",
    WIN_RATE: f"Taxa de vitórias (mín. {MIN_BOARD_GAMES} jogos)",
    KDA: f"KDA (mín. {MIN_BOARD_GAMES} jogos com stats validadas)",
}
BOARD_EMPTY = {
    RANK: "Ainda ninguém terminou a colocação.",
    WIN_RATE: f"Ainda ninguém tem {MIN_BOARD_GAMES} jogos.",
    KDA: f"Ainda ninguém tem {MIN_BOARD_GAMES} jogos com stats validadas.",
}


def escape(text: str) -> str:
    return discord.utils.escape_markdown(text)


def champion_label(champion: str, icons: Mapping[str, str], names: Mapping[str, str]) -> str:
    """The champion's icon, else its name in bold (the Data Dragon id if the list lacks it)."""
    return icons.get(champion) or f"**{escape(names.get(champion, champion))}**"


def games_text(count: int) -> str:
    return f"{count} jogo" if count == 1 else f"{count} jogos"


def tally_text(tally: Tally) -> str:
    """`3V 1D (75% · 4 jogos)`: the sample size is always next to the percentage."""
    rate = tally.win_rate
    percent = "—" if rate is None else f"{rate * 100:.0f}%"
    return f"{tally.wins}V {tally.losses}D ({percent} · {games_text(tally.games)})"


def kda_summary(kda: Kda | None, games: int | None = None) -> str:
    """`KDA 4.20 (6.0/2.0/7.0)`; says how many games it covers when that differs from `games`."""
    if kda is None:
        return "sem KDA validado"
    kills, deaths, assists = kda.average
    text = f"KDA {kda.ratio:.2f} ({kills:.1f}/{deaths:.1f}/{assists:.1f})"
    return text if games is None or games == kda.games else f"{text} em {games_text(kda.games)}"


def _pending_note(pending: int) -> str:
    plural = "s" if pending != 1 else ""
    return f"⏳ {pending} jogo{plural} com stats por validar (ainda não contam)."


def build_history_embed(
    history: History,
    now: datetime,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Histórico · {escape(history.player.nickname)}", color=discord.Color.blurple()
    )
    if not history.lines:
        embed.description = NO_GAMES
    else:
        lines = []
        for game in history.lines:
            result = "✅" if game.won else "❌"
            parts = [f"{result} {SIDE_EMOJI[game.side]} {role_label(game.role, roles)}"]
            if game.champion is not None:
                champion = champion_label(game.champion, champion_icons, champion_names)
                parts.append(f"{champion} {kda_text(game.kills, game.deaths, game.assists)}")
            else:
                parts.append("stats por validar ⏳" if game.pending else "sem stats")
            parts += [format_ago(game.confirmed_at, now), f"#{game.match_id}"]
            lines.append(" · ".join(parts))
        pending = sum(g.pending for g in history.lines)
        head = f"Últimos {len(history.lines)} de {games_text(history.overall.games)} · "
        head += tally_text(history.overall)
        embed.description = "\n".join(
            [head, "", *lines] + ([_pending_note(pending)] if pending else [])
        )
    embed.set_footer(text=FOOTER)
    return embed


def _breakdown_line(prefix: str, item: Breakdown) -> str:
    return f"{prefix} · {tally_text(item.tally)} · {kda_summary(item.kda, item.tally.games)}"


def build_stats_embed(
    stats: PlayerStats,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Estatísticas · {escape(stats.player.nickname)}", color=discord.Color.blurple()
    )
    if stats.overall.games == 0:
        embed.description = NO_GAMES
        embed.set_footer(text=FOOTER)
        return embed
    lines = [tally_text(stats.overall), kda_summary(stats.kda, stats.overall.games)]
    if stats.pending:
        lines.append(_pending_note(stats.pending))
    embed.description = "\n".join(lines)
    embed.add_field(
        name="Por role",
        value="\n".join(
            _breakdown_line(roles.get(item.key[0]) or f"**{ROLE_NAMES_PT[item.key[0]]}**", item)
            for item in stats.by_role
        ),
        inline=False,
    )
    if stats.by_champion:
        embed.add_field(
            name="Campeões (stats validadas)",
            value="\n".join(
                _breakdown_line(champion_label(item.key[0], champion_icons, champion_names), item)
                for item in stats.by_champion
            ),
            inline=False,
        )
        embed.add_field(
            name="Campeão × role (stats validadas)",
            value="\n".join(
                _breakdown_line(
                    f"{champion_label(item.key[0], champion_icons, champion_names)} "
                    f"{role_label(item.key[1], roles)}",
                    item,
                )
                for item in stats.by_champion_role
            ),
            inline=False,
        )
    embed.set_footer(text=FOOTER)
    return embed


def board_line(board: str, entry: BoardEntry, ladder: LadderConfig) -> str:
    name = f"**{escape(entry.player.nickname)}**"
    if board == RANK:
        label = ladder.label(entry.tier_key or "", entry.division)
        return f"{entry.position}. {name} — {label} · {entry.tally.wins}V {entry.tally.losses}D"
    if board == WIN_RATE:
        return f"{entry.position}. {name} — {tally_text(entry.tally)}"
    games = entry.kda.games if entry.kda else 0
    return f"{entry.position}. {name} — {kda_summary(entry.kda)} · {games_text(games)}"


def build_board_embed(
    board: str, entries: Sequence[BoardEntry], ladder: LadderConfig
) -> discord.Embed:
    embed = discord.Embed(title=BOARD_TITLES[board], color=discord.Color.gold())
    lines = [board_line(board, entry, ladder) for entry in entries[:MAX_BOARD_ROWS]]
    if len(entries) > MAX_BOARD_ROWS:
        lines.append(f"… e mais {len(entries) - MAX_BOARD_ROWS}.")
    embed.description = "\n".join(lines) or BOARD_EMPTY[board]
    embed.set_footer(text=FOOTER)
    return embed
