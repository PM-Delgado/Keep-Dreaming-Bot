"""A consistent copy of the database before anything is changed."""

import sqlite3
from datetime import datetime
from pathlib import Path


def backup_database(path: Path, reason: str, now: datetime | None = None) -> Path | None:
    """Copy `path` next to itself as `<name>.bak-<time>-<reason>` (git-ignored by `*.sqlite3.bak*`).

    Uses SQLite's backup API, so it is consistent even with a WAL file next to the database.
    Returns None when there is nothing to back up yet.
    """
    if not path.exists():
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}-{reason}")
    source = sqlite3.connect(path)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    return target
