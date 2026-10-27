"""Match-history view model and text formatting. Pure: no Discord, no database.

This is the foundation for `/riot history`: `to_entry` turns a stored game into a display-ready
entry, `summarize` aggregates a list, and `format_line` renders one game. Presentation choices
(emoji, field order, which stats appear) live in this module and in `build_history_embed`, so
restyling the output never touches the Riot or storage code.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from bot.features.players.proficiency import ROLE_NAMES_PT
from bot.features.riot.repository import StoredGame
from bot.features.riot.roles import RoleHistoryParams, position_role

RESULT_EMOJI = {"win": "✅", "loss": "❌", "remake": "🔁"}
RESULT_TEXT = {"win": "Vitória", "loss": "Derrota", "remake": "Remake"}


@dataclass(frozen=True)
class HistoryEntry:
    result: str  # win | loss | remake
    champion: str
    role: str | None  # our role key (TOP, JUNGLE, MID, ADC, SUPPORT) or None when unknown
    kills: int
    deaths: int
    assists: int
    duration_s: int
    played_at: datetime
    riot_id: str
    cs: int | None
    damage: int | None
    gold: int | None
    vision_score: int | None

    @property
    def kda(self) -> float:
        """(kills + assists) / deaths, with deaths floored at 1 so a deathless game is finite."""
        return (self.kills + self.assists) / max(self.deaths, 1)

    @property
    def cs_per_min(self) -> float | None:
        if self.cs is None or self.duration_s <= 0:
            return None
        return self.cs / (self.duration_s / 60)


@dataclass(frozen=True)
class HistorySummary:
    games: int
    wins: int
    losses: int
    remakes: int
    kda: float  # total (K + A) / total D over the counted games


def to_entry(game: StoredGame) -> HistoryEntry:
    remake = game.early_surrender or game.duration_s < RoleHistoryParams().min_duration_s
    result = "remake" if remake else ("win" if game.win else "loss")
    return HistoryEntry(
        result=result,
        champion=game.champion or "?",
        role=position_role(game.team_position),
        kills=game.kills,
        deaths=game.deaths,
        assists=game.assists,
        duration_s=game.duration_s,
        played_at=game.played_at,
        riot_id=game.riot_id,
        cs=game.cs,
        damage=game.damage,
        gold=game.gold,
        vision_score=game.vision_score,
    )


def summarize(entries: Sequence[HistoryEntry]) -> HistorySummary:
    counted = [e for e in entries if e.result != "remake"]
    deaths = sum(e.deaths for e in counted)
    kda = sum(e.kills + e.assists for e in counted) / max(deaths, 1)
    return HistorySummary(
        games=len(entries),
        wins=sum(e.result == "win" for e in entries),
        losses=sum(e.result == "loss" for e in entries),
        remakes=sum(e.result == "remake" for e in entries),
        kda=kda,
    )


def format_duration(seconds: int) -> str:
    return f"{max(seconds, 0) // 60} min"


def format_ago(moment: datetime, now: datetime) -> str:
    seconds = max((now - moment).total_seconds(), 0)
    if seconds < 3600:
        return f"há {int(seconds // 60)} min"
    if seconds < 86400:
        return f"há {int(seconds // 3600)} h"
    return f"há {int(seconds // 86400)} d"


def format_count(value: int) -> str:
    """12345 -> 12.3k (compact numbers for damage and gold)."""
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def format_line(
    entry: HistoryEntry,
    now: datetime,
    show_account: bool = False,
    details: bool = False,
    champion_icon: str = "",
    role_icon: str = "",
    champion_name: str | None = None,
) -> str:
    """One game: result, champion, role, KDA, CS, duration, age (+ optional extra stats).

    The champion and the role are shown as icons when given, else as text (`champion_name` is the
    display name; the entry only has Riot's id).
    """
    result = RESULT_EMOJI[entry.result]
    kda = f"{entry.kills}/{entry.deaths}/{entry.assists} ({entry.kda:.1f} KDA)"
    if champion_icon and role_icon:
        parts = [f"{result} {champion_icon} {role_icon}", kda]
    else:
        champion = champion_icon or f"**{champion_name or entry.champion}**"
        role = role_icon or ROLE_NAMES_PT.get(entry.role or "", "—")
        parts = [f"{result} {champion}", role, kda]
    if entry.cs is not None:
        per_min = entry.cs_per_min
        parts.append(f"{entry.cs} CS" + (f" ({per_min:.1f}/min)" if per_min is not None else ""))
    parts += [format_duration(entry.duration_s), format_ago(entry.played_at, now)]
    line = " · ".join(parts)
    if show_account:
        line += f" · *{entry.riot_id}*"
    if details:
        extra = []
        if entry.damage is not None:
            extra.append(f"{format_count(entry.damage)} dano")
        if entry.gold is not None:
            extra.append(f"{format_count(entry.gold)} ouro")
        if entry.vision_score is not None:
            extra.append(f"visão {entry.vision_score}")
        if extra:
            line += "\n　↳ " + " · ".join(extra)
    return line


def format_summary(summary: HistorySummary) -> str:
    played = summary.wins + summary.losses
    percent = f"{summary.wins / played * 100:.0f}%" if played else "—"
    remakes = f" · {summary.remakes} remake(s)" if summary.remakes else ""
    return f"Jogos: {summary.wins}V {summary.losses}D ({percent}){remakes} · KDA: {summary.kda:.2f}"
