"""Rating business rules: seeds, rank signals, adjustments, replay-backed caches, inspection."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from bot.features.ratings.engine import RatingEngine
from bot.features.ratings.ladder import DEFAULT_LADDER, LadderConfig, Standing, step_progress
from bot.features.ratings.matchmaking import (
    Estimate,
    FormGame,
    default_seed,
    effective_estimate,
    form_adjustment,
    seed_from_rank,
)
from bot.features.ratings.params import MatchmakingParams, algo_version
from bot.features.ratings.rank import (
    APEX_POINTS,
    RankResolution,
    ResolvedRank,
    is_stale,
    rank_age_days,
    rank_points,
    resolve_rank,
)
from bot.features.ratings.replay import MatchIn, replay
from bot.features.ratings.repository import RatingRepository, StoredRating

log = logging.getLogger(__name__)


class RatingError(ValueError):
    """A user-facing problem (message is shown to the user)."""


async def _no_matches() -> list[MatchIn]:
    return []


async def _no_form(player_id: int, limit: int) -> list[FormGame]:
    return []


async def _no_main(player_id: int) -> str | None:
    return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Inspection:
    stored: StoredRating
    standing: Standing
    resolution: RankResolution
    estimate: Estimate
    rank_age_days: float | None
    rank_stale: bool


class RatingService:
    def __init__(
        self,
        repo: RatingRepository,
        params: MatchmakingParams | None = None,
        engine: RatingEngine | None = None,
        ladder: LadderConfig = DEFAULT_LADDER,
        clock: Callable[[], datetime] = _utcnow,
        main_puuid_of: Callable[[int], Awaitable[str | None]] = _no_main,
    ) -> None:
        self.repo = repo
        self.params = params or MatchmakingParams()
        self.engine = engine or RatingEngine()
        self.ladder = ladder
        self.clock = clock
        self.main_puuid_of = main_puuid_of  # M1b injects the designated main account's puuid
        self.matches_source: Callable[[], Awaitable[list[MatchIn]]] = _no_matches  # set by M3
        self.form_source: Callable[[int, int], Awaitable[list[FormGame]]] = _no_form  # ... and this
        self.algo_version = algo_version(self.engine.params)
        self._rebuild_lock = asyncio.Lock()

    # --- caches ----------------------------------------------------------------------------

    async def rebuild(self) -> None:
        """Replay every authoritative input and rewrite the caches."""
        async with self._rebuild_lock:
            events = await self.repo.list_events()
            result = replay(events, await self.matches_source(), self.engine, self.ladder)
            await self.repo.replace_caches(
                result, self.algo_version, self.ladder.version, self.clock()
            )

    async def rebuild_if_stale(self) -> bool:
        """Startup check: replay when the algorithm/ladder version changed or a cache is missing."""
        if await self.repo.count_events() == 0:
            return False
        current = {(self.algo_version, self.ladder.version)}
        if (
            await self.repo.cache_versions() == current
            and await self.repo.players_without_rating_but_with_events() == 0
        ):
            return False
        await self.rebuild()
        return True

    # --- seeds and adjustments -------------------------------------------------------------

    async def ensure_default_seed(self, player_id: int, created_by: int | None = None) -> None:
        if await self.repo.has_events(player_id):
            return
        mu, sigma = default_seed(self.params)
        await self.repo.add_event(
            player_id, "seed", mu, sigma, "default", "no rank information", self.clock(), created_by
        )
        await self.rebuild()

    async def adjust(
        self,
        player_id: int,
        mu: float,
        sigma: float | None,
        admin_id: int,
        note: str | None = None,
    ) -> StoredRating:
        stored = await self.repo.get_stored(player_id)
        if stored is None:
            raise RatingError("Este jogador ainda não tem rating.")
        new_sigma = stored.sigma if sigma is None else sigma
        if not 0 < mu < 100:
            raise RatingError("mu tem de estar entre 0 e 100.")
        if not 0.5 <= new_sigma <= 25:
            raise RatingError("sigma tem de estar entre 0,5 e 25.")
        await self.repo.add_event(
            player_id, "adjust", mu, new_sigma, "admin", note, self.clock(), admin_id
        )
        await self.rebuild()
        updated = await self.repo.get_stored(player_id)
        assert updated is not None
        return updated

    # --- rank signals ---------------------------------------------------------------------

    async def resolve(self, player_id: int) -> RankResolution:
        signals = await self.repo.list_signals(player_id)
        return resolve_rank(signals, await self.main_puuid_of(player_id))

    async def record_rank_signal(
        self,
        player_id: int,
        source: str,
        status: str,
        tier: str | None,
        division: str | None,
        created_by: int | None,
        account_key: str | None = None,
        detail: str | None = None,
        unchanged_for: timedelta | None = None,
    ) -> int:
        """Append a rank signal (the only way R changes) and re-seed H when it is still unproven.

        A *failed* Riot request must not call this at all; only successful lookups (ranked or
        confirmed unranked) do, so a failure can never supersede a good signal.

        `unchanged_for` keeps automatic refreshes from filling the history with copies: when the
        account's latest signal says the same thing and is younger than this, nothing is stored
        and that signal's id is returned. (A copy is still stored once the old one is older, so
        the rank's age, the stale flag and the replay order stay meaningful.)
        """
        previous = None
        if account_key is not None:
            same_account = [
                s
                for s in await self.repo.list_signals(player_id)
                if s.source == source and s.account_key == account_key
            ]
            previous = same_account[-1] if same_account else None
        if (
            unchanged_for is not None
            and previous is not None
            and (previous.status, previous.tier, previous.division) == (status, tier, division)
            and self.clock() - previous.created_at < unchanged_for
        ):
            log.debug("Rank signal for player %s unchanged: not stored again", player_id)
            return previous.id
        before = (await self.resolve(player_id)).rank
        signal_id = await self.repo.add_signal(
            player_id,
            source,
            status,
            tier,
            division,
            self.clock(),
            created_by,
            account_key=account_key,
            queue_type="RANKED_SOLO_5x5" if source == "riot" else None,
            detail=detail,
        )
        after = (await self.resolve(player_id)).rank
        await self._maybe_reseed(player_id, before, after, created_by)
        log.info(
            "Rank signal for player %s: %s %s %s %s",
            player_id,
            source,
            status,
            tier or "-",
            division or "",
        )
        if previous is not None and (previous.status, previous.tier, previous.division) != (
            status,
            tier,
            division,
        ):
            log.info(
                "Rank changed for player %s (%s): %s -> %s",
                player_id,
                source,
                _signal_text(previous.status, previous.tier, previous.division),
                _signal_text(status, tier, division),
            )
        return signal_id

    async def _maybe_reseed(
        self,
        player_id: int,
        before: ResolvedRank | None,
        after: ResolvedRank | None,
        created_by: int | None,
    ) -> None:
        if after is None:
            return  # R vanished (e.g. confirmed unranked): H is left as it is
        if before is not None and (before.tier, before.division) == (after.tier, after.division):
            return
        stored = await self.repo.get_stored(player_id)
        if stored is not None and stored.games >= self.params.reseed_below_games:
            return  # enough in-house evidence: only the anchor (E) moves
        mu, sigma = seed_from_rank(after.mu(self.params), self.params)
        await self.repo.add_event(
            player_id,
            "seed",
            mu,
            sigma,
            after.source,
            f"rank {after.label()} ({after.source})",  # deliberately no Riot ID
            self.clock(),
            created_by,
        )
        await self.rebuild()

    async def set_self_reported_rank(
        self, player_id: int, tier: str, division: str | None, created_by: int | None
    ) -> ResolvedRank | None:
        try:
            rank_points(tier, division)
        except ValueError as exc:
            raise RatingError("Rank inválido: escolhe o tier e a divisão.") from exc
        tier, division = _normalise(tier, division)
        await self.record_rank_signal(
            player_id, "self_reported", "ranked", tier, division, created_by
        )
        return (await self.resolve(player_id)).rank

    async def set_admin_rank(
        self, player_id: int, tier: str | None, division: str | None, admin_id: int
    ) -> ResolvedRank | None:
        """Admin override of R; `tier=None` clears the override."""
        if tier is None:
            await self.record_rank_signal(player_id, "admin", "cleared", None, None, admin_id)
        else:
            try:
                rank_points(tier, division)
            except ValueError as exc:
                raise RatingError("Rank inválido: escolhe o tier e a divisão.") from exc
            tier, division = _normalise(tier, division)
            await self.record_rank_signal(player_id, "admin", "ranked", tier, division, admin_id)
        return (await self.resolve(player_id)).rank

    # --- read side -------------------------------------------------------------------------

    def standing_of(self, stored: StoredRating) -> Standing:
        progress = (
            step_progress(self.ladder, stored.tier_key, stored.division, stored.mu)
            if stored.placed
            else None
        )
        return Standing(stored.games, stored.placed, stored.tier_key, stored.division, progress)

    async def inspect(self, player_id: int) -> Inspection:
        stored = await self.repo.get_stored(player_id)
        if stored is None:
            raise RatingError("Este jogador ainda não tem rating.")
        resolution = await self.resolve(player_id)
        rank_mu = resolution.rank.mu(self.params) if resolution.rank else None
        estimate = effective_estimate(stored.mu, stored.sigma, rank_mu, self.params)
        if self.params.form_weight:
            recent = await self.form_source(player_id, self.params.form_games)
            form = form_adjustment(recent, self.params)
            estimate = replace(estimate, mu=estimate.mu + form, form=form)
        now = self.clock()
        age = rank_age_days(resolution.rank, now) if resolution.rank else None
        stale = is_stale(resolution.rank, now, self.params) if resolution.rank else False
        return Inspection(stored, self.standing_of(stored), resolution, estimate, age, stale)


def _signal_text(status: str, tier: str | None, division: str | None) -> str:
    return f"{tier} {division or ''}".strip() if tier else status


def _normalise(tier: str, division: str | None) -> tuple[str, str | None]:
    tier = tier.strip().upper()
    if tier in APEX_POINTS:
        return tier, None
    return tier, (division or "").strip().upper()
