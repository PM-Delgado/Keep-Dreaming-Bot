"""The roster file: who the group is, read and checked as a whole. Pure: no database, no network.

`data/roster.json` (git-ignored; start from `tools/roster.example.json`) holds one entry per
player. Every problem in the file is collected and reported together, so it can be fixed in one go.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from bot.features.players.proficiency import ROLES
from bot.features.players.service import NICKNAME_MAX, NICKNAME_MIN
from bot.features.ratings.matchmaking import default_seed
from bot.features.ratings.params import MatchmakingParams
from bot.features.ratings.rank import rank_mu
from bot.features.riot.platforms import normalise_platform

TIERS = ("IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND")
APEX = ("MASTER", "GRANDMASTER", "CHALLENGER")
DIVISIONS = ("IV", "III", "II", "I")


class RosterError(Exception):
    """The file cannot be used; `problems` lists everything that is wrong with it."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class RosterEntry:
    discord_id: int
    nickname: str
    tier: str | None  # None: no rank given
    division: str | None
    roles: Mapping[str, int]  # role -> declared proficiency 1..5
    riot: tuple[str, ...]  # Riot IDs (Name#TAG); the first becomes the main account
    platform: str
    strength: float | None  # simulations only; None derives it from the rank

    @property
    def rank_label(self) -> str:
        if self.tier is None:
            return "sem rank"
        return self.tier if self.division is None else f"{self.tier} {self.division}"


def strength_of(entry: RosterEntry, params: MatchmakingParams | None = None) -> float:
    """The hidden "true" strength a simulation uses: the file's value, else the rank's rating."""
    params = params or MatchmakingParams()
    if entry.strength is not None:
        return entry.strength
    if entry.tier is None:
        return default_seed(params)[0]
    return rank_mu(entry.tier, entry.division, params)


def load_roster(path: Path) -> list[RosterEntry]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RosterError([f"cannot read {path}: {exc.strerror or exc}"]) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RosterError([f"{path} is not valid JSON: {exc}"]) from exc
    return parse_roster(document)


def parse_roster(document: object) -> list[RosterEntry]:
    players = document.get("players") if isinstance(document, dict) else None
    if not isinstance(players, list) or not players:
        raise RosterError(['the file needs a non-empty "players" list'])
    problems: list[str] = []
    entries: list[RosterEntry] = []
    for index, raw in enumerate(players, start=1):
        label = f"entry {index}"
        if not isinstance(raw, dict):
            problems.append(f"{label}: must be an object")
            continue
        entry = _entry(raw, label, problems)
        if entry is not None:
            entries.append(entry)
    _check_unique(entries, problems)
    if problems:
        raise RosterError(problems)
    return entries


def _entry(raw: dict[str, object], label: str, problems: list[str]) -> RosterEntry | None:
    found = len(problems)
    raw_nickname = raw.get("nickname")
    nickname = ""
    if isinstance(raw_nickname, str):
        nickname = " ".join(raw_nickname.split())
        label = f"{label} ({nickname})"
        if not NICKNAME_MIN <= len(nickname) <= NICKNAME_MAX or not nickname.isprintable():
            problems.append(f"{label}: nickname must have {NICKNAME_MIN}-{NICKNAME_MAX} characters")
    else:
        problems.append(f"{label}: nickname is missing")

    raw_id = raw.get("discord_id")
    discord_id = 0
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        problems.append(f"{label}: discord_id must be the member's Discord user id (still 0?)")
    else:
        discord_id = raw_id

    tier, division = _rank(raw.get("rank"), label, problems)
    roles = _roles(raw.get("roles"), label, problems)
    riot = _riot(raw.get("riot"), label, problems)

    platform = "euw1"
    raw_platform = raw.get("platform", "euw1")
    try:
        platform = normalise_platform(str(raw_platform))
    except ValueError:
        problems.append(f"{label}: unknown platform {raw_platform!r} (try euw1, eun1, ...)")

    raw_strength = raw.get("strength")
    strength: float | None = None
    if raw_strength is not None:
        if (
            isinstance(raw_strength, bool)
            or not isinstance(raw_strength, int | float)
            or not 0 < raw_strength < 100
        ):
            problems.append(f"{label}: strength must be null or a number between 0 and 100")
        else:
            strength = float(raw_strength)

    if len(problems) > found:
        return None
    return RosterEntry(discord_id, nickname, tier, division, roles, riot, platform, strength)


def _rank(raw: object, label: str, problems: list[str]) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        problems.append(f'{label}: rank must be null or {{"tier": ..., "division": ...}}')
        return None, None
    tier = str(raw.get("tier") or "").strip().upper()
    division = raw.get("division")
    division = None if division is None else str(division).strip().upper()
    if tier in APEX:
        return tier, None
    if tier not in TIERS:
        problems.append(f"{label}: unknown tier {raw.get('tier')!r}")
    elif division not in DIVISIONS:
        problems.append(f"{label}: {tier} needs a division (IV, III, II or I)")
    return tier, division


def _roles(raw: object, label: str, problems: list[str]) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append(f'{label}: roles must be an object such as {{"MID": 5}}')
        return {}
    roles: dict[str, int] = {}
    for role, level in raw.items():
        name = str(role).strip().upper()
        if name not in ROLES:
            problems.append(f"{label}: unknown role {role!r} (use {', '.join(ROLES)})")
        elif isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5:
            problems.append(f"{label}: proficiency of {name} must be a whole number from 1 to 5")
        else:
            roles[name] = level
    return roles


def _riot(raw: object, label: str, problems: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append(f"{label}: riot must be a list of Name#TAG")
        return ()
    ids: list[str] = []
    for item in raw:
        text = item.strip() if isinstance(item, str) else ""
        name, sep, tag = text.rpartition("#")
        if not sep or not name.strip() or not tag.strip().isalnum():
            problems.append(f"{label}: {item!r} is not a Riot ID (use Name#TAG)")
        else:
            ids.append(f"{name.strip()}#{tag.strip()}")
    return tuple(ids)


def _check_unique(entries: list[RosterEntry], problems: list[str]) -> None:
    for what, keys in (
        ("discord_id", [str(e.discord_id) for e in entries]),
        ("nickname", [e.nickname.casefold() for e in entries]),
        ("Riot ID", [r.casefold() for e in entries for r in e.riot]),
    ):
        seen: set[str] = set()
        for key in keys:
            if key in seen:
                problems.append(f"duplicate {what}: {key}")
            seen.add(key)
