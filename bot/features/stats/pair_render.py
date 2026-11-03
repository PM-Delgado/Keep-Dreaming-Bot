"""The three `/rivalry` tabs as embeds. Pure: icons and names are passed in.

Same look as `/history` and `/stats` (large heading, avatar, no footer). The two players are told
apart by their bold names, never by a blue/red dot: blue and red are the sides of a game here.
"""

from collections.abc import Mapping
from datetime import datetime

import discord

from bot.features.icons.roles import role_label
from bot.features.matches.service import kda_text
from bot.features.players.repository import Player
from bot.features.riot.history import format_ago
from bot.features.stats.pairs import (
    MIN_PAIR_PERCENT,
    TAB_GAMES,
    TAB_H2H,
    TAB_SYNERGY,
    A,
    PairStats,
    SharedGame,
)
from bot.features.stats.render import (
    _card,
    _pending_note,
    champion_label,
    escape,
    games_text,
    tally_line,
)
from bot.features.stats.repository import GameRow
from bot.features.stats.tally import Tally

TAB_LABELS = {TAB_H2H: "Confrontos", TAB_SYNERGY: "Sinergia", TAB_GAMES: "Jogos"}
TAB_EMOJIS = {TAB_H2H: "⚔️", TAB_SYNERGY: "🤝", TAB_GAMES: "📜"}
TAB_COLORS = {
    TAB_H2H: discord.Color.red(),
    TAB_SYNERGY: discord.Color.green(),
    TAB_GAMES: discord.Color.blurple(),
}
SMALL_SAMPLE = f"-# Amostra pequena: percentagens a partir de {MIN_PAIR_PERCENT} jogos."
NOTHING_SHARED = "Ainda não jogaram nenhuma Scrim confirmada juntos."
NO_CONFRONTATIONS = "Ainda não se defrontaram."
NO_TEAMWORK = "Ainda não jogaram na mesma equipa."


def _name(player: Player) -> str:
    return escape(player.nickname)


def champion_ids(pair: PairStats) -> list[str]:
    """The champions on the games tab (validated stats only), for the icon lookup."""
    rows = [row for game in pair.recent for row in (game.a, game.b)]
    return [row.champion for row in rows if row.has_stats and row.champion]


def percent(wins: int, games: int) -> int:
    return round(wins * 100 / games)


def _record(tally: Tally) -> str:
    """`5V 1D (83%)` from the minimum sample on, else just `2V 1D`."""
    text = f"{tally.wins}V {tally.losses}D"
    if tally.games >= MIN_PAIR_PERCENT:
        return f"{text} ({percent(tally.wins, tally.games)}%)"
    return text


def _h2h_lines(pair: PairStats) -> list[str]:
    h2h, a, b = pair.h2h, _name(pair.a), _name(pair.b)
    if h2h.games == 0:
        return [NO_CONFRONTATIONS]
    lines = [f"**{a}** {h2h.a_wins}V · {h2h.b_wins}V **{b}** · {games_text(h2h.games)}"]
    if h2h.games >= MIN_PAIR_PERCENT:
        pa = percent(h2h.a_wins, h2h.games)
        lines.append(f"{a} {pa}% · {b} {100 - pa}%")
    else:
        lines.append(SMALL_SAMPLE)
    if h2h.current.length >= 2:
        holder = a if h2h.current.holder == A else b
        lines.append(f"🔥 {holder} ganhou os últimos {h2h.current.length}.")
    if max(h2h.longest_a, h2h.longest_b) >= 2:
        lines.append(f"Maior série: {a} {h2h.longest_a} · {b} {h2h.longest_b}")
    shown = len(h2h.last)
    label = "Último" if shown == 1 else f"Últimos {shown}"
    lines.append(f"{label} ({a}): " + " ".join("✅" if won else "❌" for won in h2h.last))
    if h2h.same_lane.games:
        lane = h2h.same_lane
        lines.append(f"Na mesma lane: {games_text(lane.games)} · {a} {lane.wins}V {lane.losses}D")
    return lines


def _synergy_lines(pair: PairStats, roles: Mapping[str, str]) -> list[str]:
    together = pair.synergy.tally
    if together.games == 0:
        return [NO_TEAMWORK]
    if together.games >= MIN_PAIR_PERCENT:
        lines = [tally_line(together)]
    else:
        lines = [f"Jogos: {together.wins}V {together.losses}D · {games_text(together.games)}"]
    lines.append(f"**Combinações** ({_name(pair.a)} + {_name(pair.b)})")
    for combo in pair.synergy.combos:
        first, second = (role_label(role, roles) for role in combo.roles)
        lines.append(f"{first} + {second} {_record(combo.tally)}")
    if together.games < MIN_PAIR_PERCENT:
        lines.append(SMALL_SAMPLE)
    return lines


def _game_side(
    row: GameRow,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> str:
    role = role_label(row.role, roles)
    if row.has_stats and row.champion is not None:
        champion = champion_label(row.champion, champion_icons, champion_names)
        return f"{role} {champion} {kda_text(row.kills, row.deaths, row.assists)}"
    return f"{role} " + ("stats por validar ⏳" if row.stats_status == "pending" else "sem stats")


def _game_line(
    game: SharedGame,
    now: datetime,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> str:
    mine = _game_side(game.a, roles, champion_icons, champion_names)
    theirs = _game_side(game.b, roles, champion_icons, champion_names)
    kind = "🤝" if game.together else "⚔️"
    link = "+" if game.together else "vs"
    result = "✅" if game.a.won else "❌"
    return (
        f"{kind} {result} {mine} {link} {theirs}"
        f" · {format_ago(game.confirmed_at, now)} · #{game.match_id}"
    )


def _games_lines(
    pair: PairStats,
    now: datetime,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> list[str]:
    shown = len(pair.recent)
    header = (
        f"Últimos {shown} de {pair.total} jogos:"
        if shown < pair.total
        else (f"{games_text(pair.total)}:")
    )
    lines = [header]
    lines += [_game_line(g, now, roles, champion_icons, champion_names) for g in pair.recent]
    pending = sum(row.stats_status == "pending" for game in pair.recent for row in (game.a, game.b))
    if pending:
        lines.append(_pending_note(pending))
    return lines


def build_pair_embed(
    pair: PairStats,
    tab: str,
    now: datetime,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
    avatar_url: str | None = None,
) -> discord.Embed:
    """One tab: large heading, the two names, then the lines of that tab."""
    a, b = _name(pair.a), _name(pair.b)
    if tab == TAB_SYNERGY:
        heading, body = f"# 🤝 Sinergia · {a} + {b}", _synergy_lines(pair, roles)
    elif tab == TAB_GAMES:
        heading = f"# 📜 {a} & {b} · Jogos"
        body = _games_lines(pair, now, roles, champion_icons, champion_names)
    else:
        tab = TAB_H2H
        heading, body = f"# ⚔️ Rivalidade · {a} vs {b}", _h2h_lines(pair)
    if pair.total == 0:
        body = [NOTHING_SHARED]
    embed = _card(TAB_COLORS[tab], avatar_url)
    embed.description = "\n".join([heading, "", *body])
    return embed
