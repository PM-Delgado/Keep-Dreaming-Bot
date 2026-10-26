from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from bot.db import Database
from bot.features.players.repository import PlayerRepository
from bot.features.ratings.engine import RatingEngine
from bot.features.ratings.ladder import DEFAULT_LADDER, LadderConfig, TierConfig
from bot.features.ratings.params import EngineParams, MatchmakingParams
from bot.features.ratings.rank import rank_mu
from bot.features.ratings.replay import replay
from bot.features.ratings.repository import RatingRepository
from bot.features.ratings.service import RatingError, RatingService

P = MatchmakingParams()
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class Clock:
    """Advances one minute per read, so every write gets a distinct, ordered timestamp."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


class Mains:
    """Stands in for the Riot account module: which puuid is a player's designated main."""

    def __init__(self) -> None:
        self.by_player: dict[int, str | None] = {}

    async def __call__(self, player_id: int) -> str | None:
        return self.by_player.get(player_id)


@pytest_asyncio.fixture
async def player_id(db: Database) -> int:
    player = await PlayerRepository(db).create(1001, "Ana", T0)
    return player.id


@pytest.fixture
def mains() -> Mains:
    return Mains()


@pytest.fixture
def service(db: Database, mains: Mains) -> RatingService:
    return RatingService(RatingRepository(db), clock=Clock(), main_puuid_of=mains)


async def events(db: Database, player_id: int):
    async with db.conn.execute(
        "SELECT kind, mu, sigma, source, detail FROM rating_events WHERE player_id = ? ORDER BY id",
        (player_id,),
    ) as cur:
        return list(await cur.fetchall())


async def test_default_seed_creates_unplaced_rating(service, player_id):
    await service.ensure_default_seed(player_id)
    await service.ensure_default_seed(player_id)  # idempotent
    stored = await service.repo.get_stored(player_id)
    assert stored is not None
    assert (stored.mu, stored.sigma, stored.games, stored.placed) == (
        25.0,
        pytest.approx(25 / 3),
        0,
        False,
    )
    assert stored.algo_version == service.algo_version
    assert stored.ladder_version == DEFAULT_LADDER.version


async def test_self_reported_rank_seeds_h_and_e_equals_r(db, service, player_id):
    await service.ensure_default_seed(player_id)
    rank = await service.set_self_reported_rank(player_id, "gold", "iv", 1001)
    assert rank is not None
    assert rank.label() == "Gold IV"
    stored = await service.repo.get_stored(player_id)
    assert stored is not None
    assert (stored.mu, stored.sigma) == (pytest.approx(24.8), P.sigma_rank_seed)
    inspection = await service.inspect(player_id)
    assert inspection.estimate.mu == pytest.approx(24.8)
    assert inspection.estimate.deviation == pytest.approx(0.0)
    assert [e[3] for e in await events(db, player_id)] == ["default", "self_reported"]


async def test_changing_rank_before_enough_games_reseeds_but_same_rank_does_not(
    db, service, player_id
):
    await service.ensure_default_seed(player_id)
    await service.set_self_reported_rank(player_id, "GOLD", "IV", 1001)
    await service.set_self_reported_rank(player_id, "GOLD", "IV", 1001)  # unchanged
    assert len(await events(db, player_id)) == 2
    await service.set_self_reported_rank(player_id, "DIAMOND", "I", 1001)
    stored = await service.repo.get_stored(player_id)
    assert stored is not None
    assert stored.mu == pytest.approx(rank_mu("DIAMOND", "I", P))
    assert len(await events(db, player_id)) == 3


async def test_with_enough_games_a_rank_change_only_moves_the_anchor(db, mains):
    # reseed_below_games=0 behaves like "the player already has enough confirmed games".
    service = RatingService(
        RatingRepository(db),
        params=MatchmakingParams(reseed_below_games=0),
        clock=Clock(),
        main_puuid_of=mains,
    )
    pid = (await PlayerRepository(db).create(1, "Bia", T0)).id
    await service.ensure_default_seed(pid)
    await service.set_self_reported_rank(pid, "GOLD", "IV", 1)
    assert len(await events(db, pid)) == 1  # no re-seed
    inspection = await service.inspect(pid)
    assert inspection.stored.mu == 25.0  # H untouched
    assert inspection.estimate.rank_mu == pytest.approx(24.8)  # R moved


async def test_invalid_rank_is_rejected(service, player_id):
    await service.ensure_default_seed(player_id)
    with pytest.raises(RatingError):
        await service.set_self_reported_rank(player_id, "WOOD", "I", 1)
    with pytest.raises(RatingError):
        await service.set_self_reported_rank(player_id, "GOLD", None, 1)


async def test_apex_rank_ignores_division(service, player_id):
    await service.ensure_default_seed(player_id)
    rank = await service.set_self_reported_rank(player_id, "master", "I", 1)
    assert rank is not None
    assert (rank.tier, rank.division) == ("MASTER", None)


async def test_riot_signals_follow_the_designated_main(db, service, player_id, mains):
    mains.by_player[player_id] = "puuid-A"
    await service.ensure_default_seed(player_id)
    await service.record_rank_signal(
        player_id, "riot", "ranked", "MASTER", None, 1, account_key="puuid-A"
    )
    assert (await service.resolve(player_id)).rank is not None
    seeds = await events(db, player_id)
    assert seeds[-1][3] == "riot"
    assert "puuid" not in (seeds[-1][4] or "").lower()  # seed detail never carries identifiers
    assert seeds[-1][4] == "rank Master (riot)"

    # Main changes to an account that has not been refreshed: A's rank must not stay active.
    mains.by_player[player_id] = "puuid-B"
    resolution = await service.resolve(player_id)
    assert resolution.rank is None
    assert resolution.riot_pending

    # A failed refresh of B writes nothing, so nothing changes.
    assert (await service.resolve(player_id)).rank is None

    # A successful refresh of B takes over and re-seeds (still no in-house games).
    await service.record_rank_signal(
        player_id, "riot", "ranked", "SILVER", "II", 1, account_key="puuid-B"
    )
    rank = (await service.resolve(player_id)).rank
    assert rank is not None
    assert rank.label() == "Silver II"
    stored = await service.repo.get_stored(player_id)
    assert stored is not None
    assert stored.mu == pytest.approx(rank_mu("SILVER", "II", P))


async def test_confirmed_unranked_supersedes_and_leaves_h_alone(db, service, player_id, mains):
    mains.by_player[player_id] = "puuid-A"
    await service.ensure_default_seed(player_id)
    await service.record_rank_signal(
        player_id, "riot", "ranked", "GOLD", "II", 1, account_key="puuid-A"
    )
    before = await service.repo.get_stored(player_id)
    n_events = len(await events(db, player_id))
    await service.record_rank_signal(
        player_id, "riot", "unranked", None, None, 1, account_key="puuid-A"
    )
    assert (await service.resolve(player_id)).rank is None
    assert len(await events(db, player_id)) == n_events  # no new seed
    assert await service.repo.get_stored(player_id) == before
    estimate = (await service.inspect(player_id)).estimate
    assert estimate.rank_mu is None
    assert estimate.mu == before.mu  # E falls back to H


async def test_admin_override_and_clear(service, player_id, mains):
    mains.by_player[player_id] = "puuid-A"
    await service.ensure_default_seed(player_id)
    await service.record_rank_signal(
        player_id, "riot", "ranked", "GOLD", "II", 1, account_key="puuid-A"
    )
    overridden = await service.set_admin_rank(player_id, "DIAMOND", "IV", 99)
    assert overridden is not None
    assert (overridden.source, overridden.tier) == ("admin", "DIAMOND")
    restored = await service.set_admin_rank(player_id, None, None, 99)
    assert restored is not None
    assert (restored.source, restored.tier) == ("riot", "GOLD")


async def test_adjust_sets_h_and_validates(db, service, player_id):
    await service.ensure_default_seed(player_id)
    updated = await service.adjust(player_id, 30.0, None, 99, "calibration")
    assert (updated.mu, updated.sigma) == (30.0, pytest.approx(25 / 3))  # sigma kept
    assert (await events(db, player_id))[-1][0] == "adjust"
    with pytest.raises(RatingError):
        await service.adjust(player_id, 500.0, None, 99)
    with pytest.raises(RatingError):
        await service.adjust(player_id, 25.0, 0.0, 99)


async def test_unknown_player_has_no_rating(service):
    with pytest.raises(RatingError):
        await service.inspect(12345)
    with pytest.raises(RatingError):
        await service.adjust(12345, 25.0, None, 1)


async def test_cache_equals_a_direct_replay_of_the_events(db, service, player_id):
    repo = PlayerRepository(db)
    other = (await repo.create(1002, "Rui", T0)).id
    await service.ensure_default_seed(player_id)
    await service.ensure_default_seed(other)
    await service.set_self_reported_rank(other, "PLATINUM", "II", 1)
    await service.adjust(player_id, 27.5, 5.0, 99)
    result = replay(await service.repo.list_events(), [], RatingEngine(), DEFAULT_LADDER)
    for pid, state in result.players.items():
        stored = await service.repo.get_stored(pid)
        assert stored is not None
        assert (stored.mu, stored.sigma, stored.games) == (state.mu, state.sigma, state.games)
    async with db.conn.execute("SELECT COUNT(*) FROM rating_history") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == len(result.history)


async def test_rebuild_is_idempotent(db, service, player_id):
    await service.ensure_default_seed(player_id)
    await service.set_self_reported_rank(player_id, "GOLD", "II", 1)
    first = await service.repo.get_stored(player_id)
    await service.rebuild()
    await service.rebuild()
    assert await service.repo.get_stored(player_id) == first


async def test_version_change_triggers_a_rebuild_only_when_needed(db, service, player_id, mains):
    await service.ensure_default_seed(player_id)
    assert await service.rebuild_if_stale() is False

    other_engine = RatingService(
        RatingRepository(db),
        engine=RatingEngine(EngineParams(beta=2.0)),
        clock=Clock(),
        main_puuid_of=mains,
    )
    assert other_engine.algo_version != service.algo_version
    assert await other_engine.rebuild_if_stale() is True
    stored = await other_engine.repo.get_stored(player_id)
    assert stored is not None
    assert stored.algo_version == other_engine.algo_version
    assert await other_engine.rebuild_if_stale() is False

    retuned = LadderConfig(
        version=2,
        tiers=tuple(
            TierConfig(t.key, t.names, t.floor + 1, t.divisions) for t in DEFAULT_LADDER.tiers
        ),
    )
    new_ladder = RatingService(
        RatingRepository(db),
        engine=RatingEngine(EngineParams(beta=2.0)),
        ladder=retuned,
        clock=Clock(),
        main_puuid_of=mains,
    )
    assert await new_ladder.rebuild_if_stale() is True
    stored = await new_ladder.repo.get_stored(player_id)
    assert stored is not None
    assert stored.ladder_version == 2


async def test_rebuild_if_stale_with_no_events_does_nothing(service):
    assert await service.rebuild_if_stale() is False


async def snapshot(db: Database):
    out = {}
    for table, cols in (
        ("rating_events", "*"),
        ("rating_history", "*"),
        ("rank_signals", "*"),
        ("ratings", "player_id, mu, sigma, games, placed, tier_key, division"),
    ):
        async with db.conn.execute(f"SELECT {cols} FROM {table} ORDER BY 1, 2") as cur:
            out[table] = list(await cur.fetchall())
    return out


async def test_matchmaking_params_never_rewrite_stored_history(db, service, player_id, mains):
    await service.ensure_default_seed(player_id)
    await service.set_self_reported_rank(player_id, "MASTER", None, 1)
    await service.adjust(player_id, 24.0, 4.0, 99)
    before = await snapshot(db)
    default_estimate = (await service.inspect(player_id)).estimate

    tight = RatingService(
        RatingRepository(db),
        params=MatchmakingParams(d_down=1.0, d_up=1.0, rank_mu_step=0.5),
        clock=Clock(),
        main_puuid_of=mains,
    )
    tight_estimate = (await tight.inspect(player_id)).estimate
    assert await tight.rebuild_if_stale() is False  # params are not part of the cache versions
    assert await snapshot(db) == before  # byte-identical authoritative inputs and caches
    assert tight_estimate.mu != default_estimate.mu  # only the computed estimate changed


async def test_foreign_keys_hold_after_scenarios(db, service, player_id):
    await service.ensure_default_seed(player_id)
    await service.set_self_reported_rank(player_id, "GOLD", "I", 1)
    await service.set_admin_rank(player_id, "EMERALD", "IV", 99)
    async with db.conn.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall() == []
