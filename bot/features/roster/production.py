"""Get a database ready to go live: the group's roster and the /puta names, nothing else.

Everything the tests and the test games left behind is removed (games, lobbies, mock players,
rating adjustments and the daily `picks`), the roster is seeded again through the real services,
and the result is checked table by table. The names and phrases of /puta are kept as they are.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bot.db import Database
from bot.features.nameday.repository import SEED_DIR, NameDayRepository
from bot.features.roster.loader import RosterEntry
from bot.features.roster.repository import DataSnapshot, RosterRepository
from bot.features.roster.seed import SeedReport, seed_roster
from bot.features.roster.stack import Stack


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ProductionPlan:
    before: DataSnapshot
    roster_players: int


@dataclass(frozen=True)
class ProductionResult:
    seeded: SeedReport
    after: DataSnapshot
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def _seed_list(name: str, seed_dir: Path) -> set[str]:
    return set(json.loads((seed_dir / name).read_text(encoding="utf-8")))


def check_production(
    snap: DataSnapshot,
    entries: list[RosterEntry],
    *,
    link_riot: bool,
    missing_names: set[str],
    missing_phrases: set[str],
) -> list[Check]:
    """Pure: does the snapshot look like a clean go-live database for this roster?"""
    expected_accounts = sum(len(e.riot) for e in entries)
    ranked = sum(1 for e in entries if e.tier is not None)
    checks = [
        Check("integrity", snap.integrity == "ok", snap.integrity),
        Check(
            "foreign keys", snap.foreign_key_problems == 0, f"{snap.foreign_key_problems} broken"
        ),
        Check(
            "/puta names",
            snap.names > 0 and not missing_names,
            f"{snap.names} kept"
            + (f", missing from the seed: {sorted(missing_names)}" if missing_names else ""),
        ),
        Check(
            "/puta phrases",
            snap.phrases > 0 and not missing_phrases,
            f"{snap.phrases} kept"
            + (f", missing from the seed: {len(missing_phrases)}" if missing_phrases else ""),
        ),
        Check("/puta picks", snap.picks == 0, f"{snap.picks} (must be 0)"),
        Check(
            "players", snap.players == len(entries), f"{snap.players} of {len(entries)} in the file"
        ),
        Check("mock players", snap.mock_players == 0, f"{snap.mock_players} (must be 0)"),
        Check(
            "ratings", snap.ratings == snap.players, f"{snap.ratings} for {snap.players} players"
        ),
        Check(
            "rank signals", snap.rank_signals >= ranked, f"{snap.rank_signals} for {ranked} ranked"
        ),
        Check(
            "games and lobbies",
            snap.matches == snap.lobbies == snap.adjustments == 0,
            f"{snap.matches} games, {snap.lobbies} lobbies, {snap.adjustments} adjustments",
        ),
    ]
    if link_riot:
        checks.append(
            Check(
                "Riot accounts",
                snap.riot_accounts == expected_accounts,
                f"{snap.riot_accounts} of {expected_accounts} in the file",
            )
        )
    return checks


async def plan_production(repo: RosterRepository, entries: list[RosterEntry]) -> ProductionPlan:
    return ProductionPlan(await repo.snapshot(), len(entries))


async def prepare_production(
    db: Database,
    stack: Stack,
    entries: list[RosterEntry],
    admin_id: int,
    *,
    link_riot: bool = True,
    progress: Callable[[str], None] | None = None,
    seed_dir: Path = SEED_DIR,
) -> ProductionResult:
    """Wipe the league data and the picks, seed the roster again, then check the result.

    Callers make the backup first. `/puta` names and phrases are only ever added to (when the
    tables are empty, from the seed files), never removed.
    """
    repo = RosterRepository(db)
    nameday = NameDayRepository(db)
    await nameday.seed_if_empty(seed_dir)
    await repo.wipe_league()
    await repo.wipe_picks()
    seeded = await seed_roster(stack, entries, admin_id, link_riot=link_riot, progress=progress)
    after = await repo.snapshot()
    names = {name for _, name in await nameday.list_names()}
    phrases = set(await nameday.list_phrases())
    checks = check_production(
        after,
        entries,
        link_riot=link_riot,
        missing_names=_seed_list("names.json", seed_dir) - names,
        missing_phrases=_seed_list("phrases.json", seed_dir) - phrases,
    )
    return ProductionResult(seeded, after, checks)
