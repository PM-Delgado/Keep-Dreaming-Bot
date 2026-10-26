from datetime import UTC, datetime, timedelta

import pytest

from bot.features.ratings.params import MatchmakingParams
from bot.features.ratings.rank import (
    ALL_TIERS,
    DIVISIONS,
    TIERS,
    RankSignal,
    is_stale,
    rank_mu,
    rank_points,
    resolve_rank,
)

P = MatchmakingParams()
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("tier", "division", "points", "mu"),
    [
        ("IRON", "IV", 0, 20.0),
        ("BRONZE", "IV", 4, 21.6),
        ("SILVER", "IV", 8, 23.2),
        ("GOLD", "IV", 12, 24.8),
        ("GOLD", "I", 15, 26.0),
        ("PLATINUM", "IV", 16, 26.4),
        ("EMERALD", "IV", 20, 28.0),
        ("DIAMOND", "IV", 24, 29.6),
        ("DIAMOND", "I", 27, 30.8),
        ("MASTER", None, 28, 31.2),
        ("GRANDMASTER", None, 32, 32.8),
        ("CHALLENGER", None, 36, 34.4),
    ],
)
def test_documented_rank_values(tier, division, points, mu):
    assert rank_points(tier, division) == points
    assert rank_mu(tier, division, P) == pytest.approx(mu)


def ordered_ladder():
    for tier in TIERS:
        for division in DIVISIONS:
            yield tier, division
    for tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        yield tier, None


def test_strictly_monotone_across_the_whole_ladder():
    values = [rank_mu(t, d, P) for t, d in ordered_ladder()]
    assert len(values) == 31
    assert all(b > a for a, b in zip(values, values[1:], strict=False))
    # The boundaries that are easy to get wrong.
    assert rank_mu("DIAMOND", "I", P) < rank_mu("MASTER", None, P)
    assert (
        rank_mu("MASTER", None, P)
        < rank_mu("GRANDMASTER", None, P)
        < rank_mu("CHALLENGER", None, P)
    )


def test_division_order_within_each_tier():
    for tier in TIERS:
        mus = [rank_mu(tier, d, P) for d in ("IV", "III", "II", "I")]
        assert mus == sorted(mus)
        assert len(set(mus)) == 4


def test_apex_tiers_ignore_division():
    assert rank_points("MASTER", "I") == rank_points("MASTER", None) == 28


def test_parsing_is_case_insensitive():
    assert rank_points(" gold ", " iv ") == rank_points("GOLD", "IV")


@pytest.mark.parametrize(
    ("tier", "division"),
    [("WOOD", "I"), ("GOLD", None), ("GOLD", "V"), ("", "I")],
)
def test_unknown_tier_or_division_raises(tier, division):
    with pytest.raises(ValueError, match="Unknown"):
        rank_points(tier, division)


def test_mapping_follows_the_params():
    custom = MatchmakingParams(rank_mu_base=10.0, rank_mu_step=1.0)
    assert rank_mu("GOLD", "IV", custom) == 22.0
    assert len(ALL_TIERS) == 10


def sig(id, source, status, minutes, tier=None, division=None, account_key=None):
    return RankSignal(
        id, source, status, T0 + timedelta(minutes=minutes), account_key, tier, division
    )


def test_no_signals_means_no_rank():
    res = resolve_rank([], None)
    assert res.rank is None
    assert not res.riot_pending
    assert res.riot_last_refreshed is None


def test_self_reported_is_used_when_nothing_better():
    res = resolve_rank(
        [
            sig(1, "self_reported", "ranked", 0, "SILVER", "II"),
            sig(2, "self_reported", "ranked", 5, "GOLD", "IV"),
        ],
        None,
    )
    assert res.rank is not None
    assert (res.rank.tier, res.rank.division, res.rank.source) == ("GOLD", "IV", "self_reported")


def test_riot_main_beats_self_reported():
    res = resolve_rank(
        [
            sig(1, "self_reported", "ranked", 10, "SILVER", "II"),
            sig(2, "riot", "ranked", 0, "PLATINUM", "IV", "puuid-main"),
        ],
        "puuid-main",
    )
    assert res.rank is not None
    assert res.rank.source == "riot"
    assert res.rank.label() == "Platinum IV"


def test_successful_unranked_refresh_supersedes_earlier_ranked_signal():
    signals = [
        sig(1, "riot", "ranked", 0, "GOLD", "II", "puuid-main"),
        sig(2, "riot", "unranked", 10, account_key="puuid-main"),
    ]
    res = resolve_rank(signals, "puuid-main")
    assert res.rank is None  # no fallback available
    assert res.riot_last_refreshed == T0 + timedelta(minutes=10)
    # ...and falls through to a self-reported rank when there is one.
    signals.append(sig(3, "self_reported", "ranked", 1, "SILVER", "I"))
    fallback = resolve_rank(signals, "puuid-main")
    assert fallback.rank is not None
    assert (fallback.rank.source, fallback.rank.tier) == ("self_reported", "SILVER")


def test_failed_refresh_leaves_no_signal_so_the_old_rank_stays():
    # A failed request appends nothing; resolving the unchanged list keeps the ranked signal.
    signals = [sig(1, "riot", "ranked", 0, "GOLD", "II", "puuid-main")]
    res = resolve_rank(signals, "puuid-main")
    assert res.rank is not None
    assert res.rank.tier == "GOLD"


def test_main_change_ignores_previous_accounts_rank():
    signals = [sig(1, "riot", "ranked", 0, "MASTER", None, "puuid-old")]
    res = resolve_rank(signals, "puuid-new")
    assert res.rank is None
    assert res.riot_pending  # new main has never been refreshed
    assert res.riot_last_refreshed is None
    # Once the new main is refreshed its rank is used, not the old one's.
    signals.append(sig(2, "riot", "ranked", 5, "SILVER", "III", "puuid-new"))
    after = resolve_rank(signals, "puuid-new")
    assert after.rank is not None
    assert after.rank.tier == "SILVER"
    assert not after.riot_pending


def test_pending_main_falls_back_to_self_reported():
    signals = [
        sig(1, "riot", "ranked", 0, "MASTER", None, "puuid-old"),
        sig(2, "self_reported", "ranked", 0, "GOLD", "III"),
    ]
    res = resolve_rank(signals, "puuid-new")
    assert res.rank is not None
    assert res.rank.source == "self_reported"
    assert res.riot_pending


def test_no_main_ignores_all_riot_signals():
    res = resolve_rank([sig(1, "riot", "ranked", 0, "MASTER", None, "puuid-x")], None)
    assert res.rank is None
    assert not res.riot_pending


def test_admin_override_beats_riot_and_clear_restores_it():
    base = [
        sig(1, "riot", "ranked", 0, "GOLD", "II", "puuid-main"),
        sig(2, "admin", "ranked", 5, "DIAMOND", "IV"),
    ]
    overridden = resolve_rank(base, "puuid-main")
    assert overridden.rank is not None
    assert (overridden.rank.source, overridden.rank.tier) == ("admin", "DIAMOND")
    cleared = resolve_rank([*base, sig(3, "admin", "cleared", 6)], "puuid-main")
    assert cleared.rank is not None
    assert (cleared.rank.source, cleared.rank.tier) == ("riot", "GOLD")


def test_equal_timestamps_are_broken_by_id():
    res = resolve_rank(
        [
            sig(2, "self_reported", "ranked", 0, "GOLD", "I"),
            sig(1, "self_reported", "ranked", 0, "IRON", "IV"),
        ],
        None,
    )
    assert res.rank is not None
    assert res.rank.tier == "GOLD"


def test_staleness_boundary_for_riot_ranks_only():
    signal = sig(1, "riot", "ranked", 0, "GOLD", "II", "puuid-main")
    rank = resolve_rank([signal], "puuid-main").rank
    assert rank is not None
    assert not is_stale(rank, T0 + timedelta(days=29), P)
    assert is_stale(rank, T0 + timedelta(days=30), P)
    assert is_stale(rank, T0 + timedelta(days=31), P)
    manual = resolve_rank([sig(2, "self_reported", "ranked", 0, "GOLD", "II")], None).rank
    assert manual is not None
    assert not is_stale(manual, T0 + timedelta(days=400), P)
