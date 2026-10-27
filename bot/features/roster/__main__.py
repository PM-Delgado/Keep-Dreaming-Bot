"""Set up the group's database from the roster file, and play test games with the real players.

    python -m bot.features.roster check                  # read data/roster.json, change nothing
    python -m bot.features.roster seed                   # register everyone, link Riot accounts
    python -m bot.features.roster games --games 22       # test games with the real players
    python -m bot.features.roster matches                # every game: who faced whom, how fair
    python -m bot.features.roster reset                  # show what would be wiped
    python -m bot.features.roster reset --yes            # wipe ALL league data (backup first)
    python -m bot.features.roster reset --keep-roster --yes   # wipe only games, keep the roster

The database is `DB_PATH` (default data/kdbot.sqlite3); change it with --db. Stop the bot first.
Every command that writes makes a backup copy next to the database (`*.sqlite3.bak-...`).
"""

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.config import ConfigError, Settings, load_settings
from bot.db import Database
from bot.features.players.repository import PlayerRepository
from bot.features.riot.client import RiotClient
from bot.features.roster.backup import backup_database
from bot.features.roster.games import Clock, Playground, PlaygroundError
from bot.features.roster.loader import RosterEntry, RosterError, load_roster, strength_of
from bot.features.roster.report import format_game, load_games, summarize
from bot.features.roster.repository import RosterRepository
from bot.features.roster.reset import plan_reset, reset_league
from bot.features.roster.seed import seed_roster
from bot.features.roster.stack import Stack

DEFAULT_ROSTER = Path("data/roster.json")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    parser.add_argument("--db", type=Path, help="default: DB_PATH from the environment / .env")
    parser.add_argument("--admin-id", type=int, help="default: the lowest ADMIN_USER_IDS")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate the roster file and show the players")
    seed = sub.add_parser("seed", help="register the roster (and link its Riot accounts)")
    seed.add_argument("--no-riot", action="store_true", help="do not link Riot accounts")
    seed.add_argument(
        "--wait-history",
        action="store_true",
        help="also download the ranked history now (slow: ~0.5 request/s; the bot does it later)",
    )
    games = sub.add_parser("games", help="play test games with the real players")
    games.add_argument("--games", type=int, default=22, help="finished games (plus 3 unfinished)")
    games.add_argument("--newcomers", type=int, default=2, help="players that stay in placement")
    games.add_argument("--seed", type=int, default=7)
    games.add_argument("--guild-id", type=int, help="default: GUILD_ID")
    matches = sub.add_parser("matches", help="list the games lane by lane (read-only)")
    matches.add_argument("--match", type=int, help="only this game number")
    matches.add_argument("--finished", action="store_true", help="only confirmed games")
    reset = sub.add_parser(
        "reset", help="remove all league data (players, ranks, Riot accounts, games, ratings)"
    )
    reset.add_argument(
        "--keep-roster",
        action="store_true",
        help="only remove games, lobbies and mock players; keep players, ranks and Riot accounts",
    )
    reset.add_argument("--yes", action="store_true", help="really do it (default: only show)")
    return parser.parse_args(argv)


def settings_or_none() -> Settings | None:
    try:
        return load_settings()
    except ConfigError:
        return None


def print_roster(entries: list[RosterEntry]) -> None:
    print(f"{len(entries)} players")
    for e in entries:
        roles = ", ".join(f"{r} {n}" for r, n in e.roles.items()) or "-"
        riot = ", ".join(e.riot) or "-"
        print(
            f"  {e.nickname:<24} {e.rank_label:<18} strength {strength_of(e):5.1f}"
            f"  roles: {roles}  riot: {riot}"
        )


async def run_seed(args: argparse.Namespace, entries: list[RosterEntry], db_path: Path) -> int:
    settings = settings_or_none()
    admin_id = args.admin_id or (min(settings.admin_ids) if settings and settings.admin_ids else 0)
    if not admin_id:
        print("No admin: set ADMIN_USER_IDS or pass --admin-id.")
        return 2
    backup = backup_database(db_path, "pre-roster")
    if backup:
        print(f"Backup: {backup}")
    key = settings.riot_api_key if settings else None
    client = RiotClient(key) if key and not args.no_riot else None
    db = Database(str(db_path))
    await db.connect()
    try:
        stack = Stack(
            db,
            frozenset({admin_id}),
            riot_client=client,
            default_platform=settings.riot_default_platform if settings else "euw1",
            sync_history=args.wait_history,
        )
        report = await seed_roster(
            stack,
            entries,
            admin_id,
            link_riot=not args.no_riot,
            progress=lambda line: print(line, flush=True),
        )
    finally:
        if client is not None:
            await client.close()
        await db.close()
    print(f"\nRegistered {len(report.created)}, already there {len(report.existing)}.")
    if not args.wait_history and report.linked:
        print("Ranked history is not downloaded yet: the bot continues it when it starts.")
    for line in report.ranks:
        print(f"  rank   {line}")
    for line in report.linked:
        print(f"  linked {line}")
    for line in report.problems:
        print(f"  CHECK  {line}")
    return 1 if report.problems else 0


async def run_games(args: argparse.Namespace, entries: list[RosterEntry], db_path: Path) -> int:
    settings = settings_or_none()
    admin_id = args.admin_id or (min(settings.admin_ids) if settings and settings.admin_ids else 0)
    guild_id = args.guild_id or (settings.guild_id if settings else 0)
    if not admin_id or not guild_id:
        print("Set GUILD_ID and ADMIN_USER_IDS (or pass --guild-id / --admin-id).")
        return 2
    real_now = datetime.now(UTC)
    clock = Clock(real_now - timedelta(hours=13 * args.games + 24))
    print(f"Backup: {backup_database(db_path, 'pre-games')}")
    db = Database(str(db_path))
    await db.connect()
    try:
        repo = RosterRepository(db)
        if (await repo.count_games()).matches:
            print("The database already has games: run `reset --keep-roster --yes` first.")
            return 2
        # The games are in the past, so the roster's own rows must be dated before them.
        await repo.date_roster_before(clock.now - timedelta(hours=1))
        stack = Stack(db, frozenset({admin_id}), clock=clock, seed=args.seed)
        playground = Playground(stack, guild_id, entries, admin_id, clock, args.seed, real_now)
        await playground.prepare()
        played = await playground.play_history(args.games, args.newcomers)
    finally:
        await db.close()
    print(
        f"Played {played.finished} finished games and {played.unfinished} unfinished ones"
        f" (with {played.mock_players} mock players to complete them)."
    )
    print("The reported game is auto-confirmed by the bot about 20 minutes from now.")
    print("Run `reset --keep-roster --yes` to remove the games again.")
    return 0


async def run_reset(args: argparse.Namespace, db_path: Path) -> int:
    db = Database(str(db_path))
    await db.connect()
    try:
        stack = Stack(db, frozenset())
        repo = RosterRepository(db)
        players = PlayerRepository(db)
        plan = await plan_reset(repo, players, args.keep_roster)
        games, roster = plan.games, plan.roster
        print(
            f"Games: {games.matches}, lobbies: {games.lobbies}, rating adjustments:"
            f" {games.adjustments}, mock players: {plan.mock_players}."
        )
        if args.keep_roster:
            print(f"Kept: {roster.players} players with their ranks, roles and Riot accounts.")
        else:
            print(
                f"Also removed: {roster.players} players, {roster.riot_accounts} Riot accounts"
                f" ({roster.riot_games} stored ranked games), {roster.rank_signals} rank signals."
            )
        print("Always kept: /puta names, phrases and picks, the champion list and icon ids.")
        if not args.yes:
            print("Nothing changed. Add --yes to do it (a backup is made first).")
            return 0
        print(f"Backup: {backup_database(db_path, 'pre-reset')}")
        await reset_league(repo, players, stack.ratings, args.keep_roster)
    finally:
        await db.close()
    if args.keep_roster:
        print("Done. The roster is untouched.")
    else:
        print("Done. The league data is empty: run `seed` to put the roster back.")
    return 0


def run_matches(args: argparse.Namespace, db_path: Path) -> int:
    if not db_path.exists():
        print(f"{db_path} does not exist.")
        return 2
    games = load_games(db_path, args.match)
    if args.finished:
        games = [g for g in games if g.status == "confirmed"]
    if not games:
        print("No games.")
        return 0
    for game in games:
        print(format_game(game))
        print()
    print(summarize(games))
    return 0


def _where_from(args: argparse.Namespace) -> str:
    if args.db:
        return "from --db"
    if "DB_PATH" in os.environ:
        return "from the DB_PATH environment variable of this terminal"
    return "from .env or the default"


def main(argv: list[str] | None = None) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = settings_or_none()
    db_path: Path = args.db or (settings.db_path if settings else Path("data/kdbot.sqlite3"))
    print(f"Database: {db_path}  ({_where_from(args)})")
    if args.command == "matches":
        return run_matches(args, db_path)
    if args.command in ("games", "reset") and not db_path.exists():
        print(
            f"{db_path} does not exist yet"
            + (": run `seed` first." if args.command == "games" else ".")
        )
        return 2
    if args.command == "reset":
        return asyncio.run(run_reset(args, db_path))
    try:
        entries = load_roster(args.roster)
    except RosterError as exc:
        print(f"{args.roster} has problems:")
        for problem in exc.problems:
            print(f"  - {problem}")
        return 2
    if args.command == "check":
        print_roster(entries)
        return 0
    try:
        if args.command == "seed":
            return asyncio.run(run_seed(args, entries, db_path))
        return asyncio.run(run_games(args, entries, db_path))
    except PlaygroundError as exc:
        print(exc)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing is lost: run the same command again to continue.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
