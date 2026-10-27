"""Put the roster into a database through the real services (register, rank, roles, Riot link)."""

from collections.abc import Callable
from dataclasses import dataclass, field

from bot.features.players.service import PlayerError
from bot.features.roster.loader import RosterEntry
from bot.features.roster.stack import Stack


@dataclass
class SeedReport:
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    ranks: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # things that need a look, not crashes


async def seed_roster(
    stack: Stack,
    entries: list[RosterEntry],
    admin_id: int,
    link_riot: bool = True,
    progress: Callable[[str], None] | None = None,
) -> SeedReport:
    """Safe to run again: existing players are kept, the rank and roles of the file are applied,
    and Riot accounts already linked are skipped."""
    report = SeedReport()
    for number, entry in enumerate(entries, start=1):
        if progress:
            progress(f"[{number}/{len(entries)}] {entry.nickname}")
        player = await stack.players.repo.get_by_discord(entry.discord_id)
        if player is None:
            player = await stack.players.register(entry.discord_id, entry.nickname, admin_id)
            report.created.append(entry.nickname)
        else:
            report.existing.append(entry.nickname)
            if player.nickname.casefold() != entry.nickname.casefold():
                report.problems.append(
                    f"{entry.nickname}: this Discord id is already registered as"
                    f" {player.nickname!r} (the nickname was not changed)"
                )

        if entry.tier is not None and await _rank_changed(stack, player.id, entry):
            await stack.ratings.set_self_reported_rank(
                player.id, entry.tier, entry.division, admin_id
            )
            report.ranks.append(f"{entry.nickname}: {entry.rank_label}")
        for role, level in entry.roles.items():
            await stack.players.set_declared(entry.discord_id, role, level)

        if entry.riot:
            await _link(stack, entry, admin_id, link_riot, report)
    await stack.riot.wait_for_background()
    return report


async def _rank_changed(stack: Stack, player_id: int, entry: RosterEntry) -> bool:
    """False when the latest self-reported rank already is the file's (so reruns add nothing)."""
    signals = await stack.ratings.repo.list_signals(player_id)
    own = [s for s in signals if s.source == "self_reported"]
    return not own or (own[-1].tier, own[-1].division) != (entry.tier, entry.division)


async def _link(
    stack: Stack, entry: RosterEntry, admin_id: int, link_riot: bool, report: SeedReport
) -> None:
    if not link_riot or not stack.riot.enabled:
        why = "--no-riot" if not link_riot else "RIOT_API_KEY is not set"
        report.problems.append(f"{entry.nickname}: Riot accounts not linked ({why})")
        return
    player = await stack.players.require(entry.discord_id)
    linked = {a.riot_id.casefold() for a in await stack.riot_repo.list_accounts(player.id)}
    for riot_id in entry.riot:
        if riot_id.casefold() in linked:
            continue
        try:
            await stack.riot.link(entry.discord_id, riot_id, entry.platform, admin_id)
        except PlayerError as exc:
            report.problems.append(f"{entry.nickname}: {riot_id}: {exc}")
        else:
            report.linked.append(f"{entry.nickname}: {riot_id}")
