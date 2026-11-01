"""Backtest the rating and matchmaking settings on the real games.

    python -m bot.features.backtest --db data/snapshot.sqlite3
    python -m bot.features.backtest --db snapshot.sqlite3 --variant "aware2: loss=2, aware"
    python -m bot.features.backtest --db snapshot.sqlite3 --detail "loss x2"

Work on a *snapshot* of the database (see docs/deploy.md), never the live file. A dev tool: the
bot does not load it, and it changes nothing.
"""

import argparse
import sys
from pathlib import Path

from bot.features.backtest.loader import load
from bot.features.backtest.model import Variant
from bot.features.backtest.report import detail, summary
from bot.features.backtest.runner import BASELINE, run_variant

PRESETS = (
    Variant("loss x1.5", loss_scale=1.5),
    Variant("loss x2", loss_scale=2.0),
    Variant("loss x3", loss_scale=3.0),
    Variant("role-aware", role_aware=True),
    Variant("role-aware, loss x2", loss_scale=2.0, role_aware=True),
    Variant("sigma floor 2.5", sigma_floor=2.5),
    Variant("sigma floor 3", sigma_floor=3.0),
)


def parse_variant(text: str) -> Variant:
    """`"name: loss=2, aware, floor=3, d_up=4, d_down=2"`; every key is optional."""
    name, _, options = text.partition(":")
    values: dict = {}
    for part in (o.strip() for o in options.split(",") if o.strip()):
        key, _, value = part.partition("=")
        key = key.strip()
        if key == "loss":
            values["loss_scale"] = float(value)
        elif key == "aware":
            values["role_aware"] = True
        elif key == "floor":
            values["sigma_floor"] = float(value)
        elif key in ("d_up", "d_down"):
            values[key] = float(value)
        else:
            raise ValueError(f"Unknown variant option {key!r} (loss, aware, floor, d_up, d_down).")
    return Variant(name.strip() or "custom", **values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.features.backtest", description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="a snapshot of the SQLite database")
    parser.add_argument(
        "--variant", action="append", default=[], help='"name: loss=2, aware, floor=3" (repeatable)'
    )
    parser.add_argument("--detail", help="show every player's rating under this variant's name")
    args = parser.parse_args(argv)

    if not args.db.is_file():
        print(f"No such file: {args.db}", file=sys.stderr)
        return 2
    try:
        variants = [parse_variant(v) for v in args.variant] or list(PRESETS)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    data = load(args.db)
    if not data.games:
        print("No confirmed games in this database yet.")
        return 0
    results = [run_variant(data, v) for v in (BASELINE, *variants)]
    print(summary(data, results))
    if args.detail:
        chosen = next((r for r in results if r.variant.name == args.detail), None)
        if chosen is None:
            print(f"\nNo variant is named {args.detail!r}.", file=sys.stderr)
            return 2
        print("\n" + detail(data, results[0], chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
