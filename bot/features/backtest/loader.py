"""Read a database snapshot (read-only) into a `Dataset`. A dev tool, so plain `sqlite3`."""

import json
import sqlite3
from pathlib import Path

from bot.features.backtest.model import Dataset, Game, GamePlayer
from bot.features.ratings.replay import RatingEventIn
from bot.features.ratings.repository import from_db


def load(path: Path | str) -> Dataset:
    """Everything a replay needs. The file is opened read-only and never changed."""
    uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        events = [
            RatingEventIn(r[0], r[1], r[2], r[3], r[4], from_db(r[5]))
            for r in conn.execute(
                "SELECT id, player_id, kind, mu, sigma, created_at FROM rating_events ORDER BY id"
            )
        ]
        names = {r[0]: r[1] for r in conn.execute("SELECT id, nickname FROM players")}
        stored = {r[0]: r[1] for r in conn.execute("SELECT player_id, mu FROM ratings")}
        games = []
        matches = conn.execute(
            "SELECT id, confirmed_at, winner FROM matches WHERE status = 'confirmed'"
            " ORDER BY confirmed_at, id"
        ).fetchall()
        for match_id, confirmed_at, winner in matches:
            rows = conn.execute(
                "SELECT player_id, side, role, rank_mu, proficiency_json FROM match_participants"
                " WHERE match_id = ?",
                (match_id,),
            ).fetchall()
            players = tuple(
                GamePlayer(pid, side, role, rank_mu, json.loads(prof) if prof else None)
                for pid, side, role, rank_mu, prof in rows
            )
            games.append(Game(match_id, from_db(confirmed_at), winner, players))
    finally:
        conn.close()
    return Dataset(events, games, names, stored)
