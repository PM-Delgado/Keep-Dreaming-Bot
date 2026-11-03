"""The three `/riot duo` tabs as embeds. Pure: icons and names are passed in.

Same look as `/rivalry` (large heading, avatar, bold names, small `-#` notes). The wording says
"na mesma equipa": two players on one team is not proof that they queued together.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

import discord

from bot.features.icons.roles import role_label
from bot.features.riot.duo import (
    LISBON,
    MIN_CHAMPION_RECORD,
    MIN_PERCENT,
    MIN_ROLE_RECORD,
    TOP_PAIRS,
    BestGame,
    DuoGame,
    DuoRow,
    DuoStats,
    PairTally,
    PlayerKda,
    WithWithout,
)
from bot.features.riot.history import format_ago, format_duration
from bot.features.stats.tally import Tally

TAB_SUMMARY, TAB_GAMES, TAB_RECORDS = "summary", "games", "records"
TABS = (TAB_SUMMARY, TAB_GAMES, TAB_RECORDS)
TAB_LABELS = {TAB_SUMMARY: "Resumo", TAB_GAMES: "Jogos", TAB_RECORDS: "Recordes"}
TAB_EMOJIS = {TAB_SUMMARY: "📊", TAB_GAMES: "🎮", TAB_RECORDS: "🏆"}
TAB_COLORS = {
    TAB_SUMMARY: discord.Color.green(),
    TAB_GAMES: discord.Color.blurple(),
    TAB_RECORDS: discord.Color.gold(),
}
NOTHING_SHARED = "Ainda não há jogos ranked Solo/Duo guardados na mesma equipa."
SMALL_SAMPLE = f"-# Amostra pequena: percentagens a partir de {MIN_PERCENT} jogos."


def _escape(text: str) -> str:
    return discord.utils.escape_markdown(text)


def _games_text(count: int) -> str:
    return f"{count} jogo" if count == 1 else f"{count} jogos"


def _percent(tally: Tally) -> int:
    return round(tally.wins * 100 / tally.games)


def _record(tally: Tally) -> str:
    """`5V 1D (83%)` from the minimum sample on, else just `2V 1D`."""
    text = f"{tally.wins}V {tally.losses}D"
    return f"{text} ({_percent(tally)}%)" if tally.games >= MIN_PERCENT else text


def _date(moment: datetime) -> str:
    return moment.astimezone(LISBON).strftime("%d/%m/%Y")


def _role(role: str | None, icons: Mapping[str, str]) -> str:
    return role_label(role, icons) if role else "—"


def _champion(champion: str | None, icons: Mapping[str, str], names: Mapping[str, str]) -> str:
    """The champion's icon, else its name in bold; `?` when Riot did not send one."""
    if not champion:
        return "?"
    return icons.get(champion) or f"**{_escape(names.get(champion, champion))}**"


def champion_ids(stats: DuoStats) -> list[str]:
    """Every champion any tab may show, for the icon and name lookup."""
    ids = [c for game in stats.recent for c in (game.a.champion, game.b.champion)]
    for pair in (*stats.champion_pairs[:TOP_PAIRS], stats.best_champion_pair):
        if pair is not None:
            ids += list(pair.key)
    ids += [best.row.champion for best in (stats.best_a, stats.best_b) if best is not None]
    return [c for c in ids if c]


# --- summary ----------------------------------------------------------------------------------


def _streak_line(stats: DuoStats) -> str | None:
    """`Sequência atual: 🔥 **3 vitórias** · Maior: 6`, or `Maior sequência de vitórias: 6` when
    the current run is shorter than two games. Streaks of one game are not worth a line."""
    current = stats.current
    longest = stats.longest_win.length if stats.longest_win is not None else 0
    parts = []
    if current.length >= 2:
        parts.append(
            f"Sequência atual: 🔥 **{current.length} vitórias**"
            if current.won
            else f"Sequência atual: 💀 **{current.length} derrotas**"
        )
        if longest >= 2:
            parts.append(f"Maior: {longest}")
    elif longest >= 2:
        parts.append(f"Maior sequência de vitórias: {longest}")
    return " · ".join(parts) or None


def _pair_lines(pairs: Sequence[PairTally], label: Callable[[str], str]) -> list[str]:
    return [
        f"{label(p.key[0])} + {label(p.key[1])} · {_record(p.tally)}" for p in pairs[:TOP_PAIRS]
    ]


def _kda_line(name: str, kda: PlayerKda) -> str:
    kills, deaths, assists = kda.average()
    return f"**{name}** | {kills:.1f}/{deaths:.1f}/{assists:.1f} ({kda.ratio:.1f})"


def _compare_line(name: str, other: str, compare: WithWithout) -> str:
    return (
        f"**{name}**: {_percent(compare.with_)}% com {other} ({compare.with_.games})"
        f" · {_percent(compare.without)}% sem ({compare.without.games})"
    )


def _notes(stats: DuoStats) -> list[str]:
    """Only what is wrong with the data right now; nothing when everything is as expected."""
    notes: list[str] = []
    if stats.syncing:
        names = ", ".join(stats.syncing)
        what = "da conta" if len(stats.syncing) == 1 else "das contas"
        notes.append(f"-# O histórico {what} {names} ainda está a sincronizar; podem faltar jogos.")
    if stats.ambiguous:
        plural = "s" if stats.ambiguous != 1 else ""
        notes.append(
            f"-# {stats.ambiguous} jogo{plural} ignorado{plural}: "
            "duas contas do mesmo jogador no mesmo jogo."
        )
    return notes


def _summary_lines(
    a: str,
    b: str,
    stats: DuoStats,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> list[str]:
    lines = [f"Jogos: **{stats.games}** | {_record(stats.tally)}"]
    if (streak := _streak_line(stats)) is not None:
        lines.append(streak)
    if stats.games < MIN_PERCENT:
        lines.append(SMALL_SAMPLE)
    if stats.role_pairs:
        lines += ["", f"**Roles** ({a} + {b})"]
        lines += _pair_lines(stats.role_pairs, lambda r: _role(r, roles))
    if stats.champion_pairs:
        lines += ["", "**Campeões**"]
        lines += _pair_lines(
            stats.champion_pairs, lambda c: _champion(c, champion_icons, champion_names)
        )
    lines += [
        "",
        "**KDA juntos**",
        _kda_line(a, stats.kda_a),
        _kda_line(b, stats.kda_b),
    ]
    compares = [
        _compare_line(name, other, compare)
        for name, other, compare in ((a, b, stats.compare_a), (b, a, stats.compare_b))
        if compare is not None and compare.shown
    ]
    if compares and stats.coverage is not None:
        lines += ["", f"**Com e sem** (desde {_date(stats.coverage)})", *compares]
    return lines


# --- games ------------------------------------------------------------------------------------


def _side(
    row: DuoRow,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> str:
    champion = _champion(row.champion, champion_icons, champion_names)
    return f"{_role(row.role, roles)} {champion} {row.kills}/{row.deaths}/{row.assists}"


def _game_line(
    game: DuoGame,
    now: datetime,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> str:
    """Two rows, so a long line never wraps into a third:

    ✅ [role] [champion] 8/2/6 + [role] [champion] 1/4/15
    -# 31 min · há 2 d · Smurf#EUW
    """
    first = (
        f"{'✅' if game.won else '❌'} {_side(game.a, roles, champion_icons, champion_names)}"
        f" + {_side(game.b, roles, champion_icons, champion_names)}"
    )
    later = [format_duration(game.a.duration_s), format_ago(game.played_at, now)]
    later += [row.riot_id for row in (game.a, game.b) if not row.is_main]
    return f"{first}\n-# {' · '.join(later)}"


def _games_lines(
    stats: DuoStats,
    now: datetime,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> list[str]:
    shown = len(stats.recent)
    header = (
        f"Últimos {shown} de {stats.games} jogos:"
        if shown < stats.games
        else f"{_games_text(stats.games)}:"
    )
    return [
        header,
        *(_game_line(g, now, roles, champion_icons, champion_names) for g in stats.recent),
    ]


# --- records ----------------------------------------------------------------------------------


def _best_game_line(
    name: str,
    best: BestGame,
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> str:
    row = best.row
    champion = _champion(row.champion, champion_icons, champion_names)
    return (
        f"🎯 Melhor jogo de **{name}**: {champion} {row.kills}/{row.deaths}/{row.assists}"
        f" ({row.kda:.1f}) · {_date(best.game.played_at)}"
    )


def _best_pair_line(label: str, pair: PairTally, minimum: int, show: Callable[[str], str]) -> str:
    return (
        f"{label}: {show(pair.key[0])} + {show(pair.key[1])} · {_record(pair.tally)}"
        f" (mín. {minimum} jogos)"
    )


def _records_lines(
    a: str,
    b: str,
    stats: DuoStats,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
) -> list[str]:
    lines: list[str] = []
    for emoji, label, run in (
        ("🔥", "Maior sequência de vitórias", stats.longest_win),
        ("💀", "Maior sequência de derrotas", stats.longest_loss),
    ):
        if run is not None:
            first, last = _date(run.first_at), _date(run.last_at)
            span = first if first == last else f"{first} – {last}"
            lines.append(f"{emoji} {label}: **{run.length}** ({span})")
    if stats.best_role_pair is not None:
        lines.append(
            _best_pair_line(
                "⭐ Melhor combinação de roles",
                stats.best_role_pair,
                MIN_ROLE_RECORD,
                lambda r: _role(r, roles),
            )
        )
    if stats.best_champion_pair is not None:
        lines.append(
            _best_pair_line(
                "🃏 Melhor combinação de campeões",
                stats.best_champion_pair,
                MIN_CHAMPION_RECORD,
                lambda c: _champion(c, champion_icons, champion_names),
            )
        )
    for name, best in ((a, stats.best_a), (b, stats.best_b)):
        if best is not None:
            lines.append(_best_game_line(name, best, champion_icons, champion_names))
    if stats.busiest_day is not None:
        day, count = stats.busiest_day
        lines.append(f"📅 Mais jogos num dia: **{count}** ({_date_text(day)})")
    if stats.first_game is not None:
        lines.append(f"🗓️ Primeiro jogo guardado juntos: {_date(stats.first_game)}")
    return lines


def _date_text(iso_day: str) -> str:
    """`2026-06-02` -> `02/06/2026`."""
    year, month, day = iso_day.split("-")
    return f"{day}/{month}/{year}"


# --- the embed --------------------------------------------------------------------------------


def build_duo_embed(
    a_nickname: str,
    b_nickname: str,
    stats: DuoStats,
    tab: str,
    now: datetime,
    roles: Mapping[str, str],
    champion_icons: Mapping[str, str],
    champion_names: Mapping[str, str],
    avatar_url: str | None = None,
) -> discord.Embed:
    """One tab: large heading, the lines of that tab, then the notes."""
    a, b = _escape(a_nickname), _escape(b_nickname)
    if tab not in TABS:
        tab = TAB_SUMMARY
    heading = {
        TAB_SUMMARY: f"# 🤝 Duo · {a} + {b}",
        TAB_GAMES: f"# 🎮 {a} & {b} · Jogos",
        TAB_RECORDS: f"# 🏆 {a} & {b} · Recordes",
    }[tab]
    if stats.games == 0:
        body = [NOTHING_SHARED]
    elif tab == TAB_GAMES:
        body = _games_lines(stats, now, roles, champion_icons, champion_names)
    elif tab == TAB_RECORDS:
        body = _records_lines(a, b, stats, roles, champion_icons, champion_names)
    else:
        body = _summary_lines(a, b, stats, roles, champion_icons, champion_names)
    embed = discord.Embed(color=TAB_COLORS[tab])
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    notes = _notes(stats)
    embed.description = "\n".join([heading, "", *body, *(["", *notes] if notes else [])])
    return embed
