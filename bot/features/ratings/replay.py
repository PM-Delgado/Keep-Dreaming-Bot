"""Deterministic replay of the authoritative inputs into rating state and history rows.

Inputs: seed/adjust events and confirmed, non-voided matches. Output is a pure function of them,
the engine parameters and the ladder, so every cache can be rebuilt at any time. Rank signals are
deliberately *not* inputs: they only affect E and the creation of seed events.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from bot.features.ratings.engine import Rating, RatingEngine
from bot.features.ratings.ladder import DEFAULT_LADDER, LadderConfig, Standing, derive_standing


class ReplayError(ValueError):
    """The authoritative data cannot be replayed (a bug or manual DB edit, never silently fixed)."""


@dataclass(frozen=True)
class RatingEventIn:
    id: int
    player_id: int
    kind: str  # seed | adjust
    mu: float
    sigma: float
    created_at: datetime


@dataclass(frozen=True)
class MatchIn:
    id: int
    confirmed_at: datetime
    winner: str  # blue | red
    blue: tuple[int, ...]
    red: tuple[int, ...]


@dataclass(frozen=True)
class PlayerState:
    mu: float
    sigma: float
    games: int
    standing: Standing


@dataclass(frozen=True)
class HistoryRow:
    player_id: int
    event_id: int | None
    match_id: int | None
    mu_before: float | None
    sigma_before: float | None
    mu_after: float
    sigma_after: float
    standing: Standing


State = dict[int, PlayerState]


def apply_event(state: State, event: RatingEventIn, ladder: LadderConfig) -> HistoryRow:
    """Seeds and adjustments set H outright; the visible standing restarts without hysteresis."""
    if event.kind not in ("seed", "adjust"):
        raise ReplayError(f"Unknown rating event kind {event.kind!r} (event {event.id}).")
    before = state.get(event.player_id)
    games = before.games if before else 0
    standing = derive_standing(event.mu, games, None, ladder)
    state[event.player_id] = PlayerState(event.mu, event.sigma, games, standing)
    return HistoryRow(
        event.player_id,
        event.id,
        None,
        before.mu if before else None,
        before.sigma if before else None,
        event.mu,
        event.sigma,
        standing,
    )


def apply_match(
    state: State, match: MatchIn, engine: RatingEngine, ladder: LadderConfig
) -> list[HistoryRow]:
    if match.winner not in ("blue", "red"):
        raise ReplayError(f"Match {match.id} has invalid winner {match.winner!r}.")
    if len(match.blue) != len(match.red) or not match.blue:
        raise ReplayError(f"Match {match.id} has unbalanced or empty teams.")
    everyone = (*match.blue, *match.red)
    if len(set(everyone)) != len(everyone):
        raise ReplayError(f"Match {match.id} lists a player more than once.")
    missing = [p for p in everyone if p not in state]
    if missing:
        raise ReplayError(f"Match {match.id} involves players without a rating: {missing}.")

    winners, losers = (match.blue, match.red) if match.winner == "blue" else (match.red, match.blue)
    new_w, new_l = engine.update(
        [Rating(state[p].mu, state[p].sigma) for p in winners],
        [Rating(state[p].mu, state[p].sigma) for p in losers],
    )
    rows: list[HistoryRow] = []
    for player_id, new in (*zip(winners, new_w, strict=True), *zip(losers, new_l, strict=True)):
        old = state[player_id]
        games = old.games + 1
        standing = derive_standing(new.mu, games, old.standing, ladder)
        state[player_id] = PlayerState(new.mu, new.sigma, games, standing)
        rows.append(
            HistoryRow(player_id, None, match.id, old.mu, old.sigma, new.mu, new.sigma, standing)
        )
    return rows


@dataclass(frozen=True)
class ReplayResult:
    players: State
    history: list[HistoryRow]


def replay(
    events: Iterable[RatingEventIn],
    matches: Iterable[MatchIn],
    engine: RatingEngine | None = None,
    ladder: LadderConfig = DEFAULT_LADDER,
) -> ReplayResult:
    """Walk one timeline ordered by (timestamp, events before matches, id)."""
    engine = engine or RatingEngine()
    timeline: list[tuple[datetime, int, int, RatingEventIn | MatchIn]] = []
    timeline.extend((e.created_at, 0, e.id, e) for e in events)
    timeline.extend((m.confirmed_at, 1, m.id, m) for m in matches)
    timeline.sort(key=lambda item: item[:3])

    state: State = {}
    history: list[HistoryRow] = []
    for _, _, _, item in timeline:
        if isinstance(item, RatingEventIn):
            history.append(apply_event(state, item, ladder))
        else:
            history.extend(apply_match(state, item, engine, ladder))
    return ReplayResult(state, history)
