import pytest

from bot.features.ratings.ladder import (
    DEFAULT_LADDER,
    LadderConfig,
    Standing,
    TierConfig,
    derive_standing,
)

L = DEFAULT_LADDER


def standing_at(mu, games=10, previous=None, ladder=L):
    return derive_standing(mu, games, previous, ladder)


def key(s: Standing):
    return (s.tier_key, s.division)


def test_default_player_is_in_the_middle_tier():
    assert key(standing_at(25.0)) == ("duro_rijo", 2)
    assert L.label("duro_rijo", 2) == "Duro que nem um rijo II"


@pytest.mark.parametrize(
    ("mu", "expected"),
    [
        (17.99, ("bot_maximo", 3)),  # clamps below the first floor
        (0.0, ("bot_maximo", 3)),
        (18.0, ("bot_maximo", 3)),
        (19.0, ("bot_maximo", 2)),
        (20.0, ("bot_maximo", 1)),
        (20.99, ("bot_maximo", 1)),
        (21.0, ("rush_caixa", 3)),
        (23.99, ("rush_caixa", 1)),
        (24.0, ("duro_rijo", 3)),
        (26.99, ("duro_rijo", 1)),
        (27.0, ("no_box", 3)),
        (29.99, ("no_box", 1)),
        (30.0, ("kd_esports", None)),
        (80.0, ("kd_esports", None)),
    ],
)
def test_tier_and_division_boundaries(mu, expected):
    assert key(standing_at(mu)) == expected


def test_top_tier_has_no_division_or_progress():
    s = standing_at(33.0)
    assert (s.division, s.progress) == (None, None)
    assert L.label("kd_esports", None) == "KD esports"


def test_progress_inside_a_division():
    assert standing_at(24.0).progress == pytest.approx(0.0)
    assert standing_at(24.5).progress == pytest.approx(0.5)
    assert standing_at(26.99).progress == pytest.approx(0.99)


def test_unplaced_until_placement_games_are_played():
    for games in range(L.placement_games):
        s = derive_standing(25.0, games, None, L)
        assert not s.placed
        assert s.tier_key is None
    assert derive_standing(25.0, L.placement_games, None, L).placed


def test_promotion_happens_at_the_floor():
    below = standing_at(24.99, previous=standing_at(24.5))
    assert key(below) == ("duro_rijo", 3)
    promoted = standing_at(25.0, previous=below)
    assert key(promoted) == ("duro_rijo", 2)


def test_demotion_waits_for_the_buffer():
    start = standing_at(25.5)  # Duro II, floor 25.0
    assert key(start) == ("duro_rijo", 2)
    still = standing_at(25.0 - 0.33 + 0.01, previous=start)
    assert key(still) == ("duro_rijo", 2)
    demoted = standing_at(25.0 - 0.33 - 0.01, previous=start)
    assert key(demoted) == ("duro_rijo", 3)


def test_no_flapping_on_oscillating_rating():
    current = standing_at(25.4)
    seen = set()
    for mu in (24.95, 25.05, 24.9, 25.1, 24.85, 25.0, 24.8):
        current = standing_at(mu, previous=current)
        seen.add(key(current))
    assert seen == {("duro_rijo", 2)}


def test_multi_step_drop_lands_on_the_raw_step():
    start = standing_at(28.5)
    assert key(start) == ("no_box", 2)
    assert key(standing_at(22.0, previous=start)) == ("rush_caixa", 2)


def test_previous_without_hysteresis_matches_fresh_placement():
    assert key(standing_at(25.9, previous=None)) == key(standing_at(25.9))


def test_unknown_previous_tier_is_treated_as_fresh():
    renamed = Standing(games=10, placed=True, tier_key="removed_tier", division=1)
    assert key(standing_at(25.0, previous=renamed)) == ("duro_rijo", 2)


def test_renaming_a_tier_keeps_stored_keys_valid():
    custom = LadderConfig(
        version=2,
        tiers=(
            TierConfig("bot_maximo", {"pt": "Bot Supremo"}, 18.0, 3),
            *L.tiers[1:],
        ),
    )
    s = standing_at(19.0, ladder=custom)
    assert s.tier_key == "bot_maximo"  # stored key unchanged
    assert custom.label(s.tier_key, s.division) == "Bot Supremo II"
    assert L.label(s.tier_key, s.division) == "Bot Máximo II"
    assert L.display_name("gone") == "gone"


def test_retuned_thresholds_change_standing_without_touching_the_rating():
    shifted = LadderConfig(
        version=2,
        tiers=tuple(TierConfig(t.key, t.names, t.floor + 3.0, t.divisions) for t in L.tiers),
    )
    assert key(standing_at(25.0)) == ("duro_rijo", 2)
    assert key(standing_at(25.0, ladder=shifted)) == ("rush_caixa", 2)


def test_overlapping_ladder_is_rejected():
    with pytest.raises(ValueError, match="ascending"):
        LadderConfig(
            version=1,
            tiers=(TierConfig("a", {"pt": "A"}, 10.0, 3), TierConfig("b", {"pt": "B"}, 11.0, 3)),
        )
