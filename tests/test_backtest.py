"""The backtest: real games played through the real services, replayed under other settings."""

import math

import pytest
import pytest_asyncio
from lobby_fakes import LobbyStack, sides_of, start_game

from bot.db import Database
from bot.features.backtest.__main__ import PRESETS, main, parse_variant
from bot.features.backtest.loader import load
from bot.features.backtest.model import Dataset, GameScore, Variant, VariantResult
from bot.features.backtest.report import describe, detail, summary
from bot.features.backtest.runner import (
    BASELINE,
    brier,
    check_against_stored,
    compare,
    log_loss,
    run_variant,
)

GAMES = 6


@pytest_asyncio.fixture
async def snapshot(tmp_path):
    """A database file with GAMES confirmed games between ten players with varied role profiles."""
    path = tmp_path / "snapshot.sqlite3"
    db = Database(path)
    await db.connect()
    stack = LobbyStack(db)
    await stack.register(10)
    # Different seeds and role knowledge, so the role effects are not all the same.
    for i in range(1, 11):
        await stack.ratings.adjust(i, 22.0 + i * 0.9, 2.0, admin_id=99)
        for j, role in enumerate(("TOP", "JUNGLE", "MID", "ADC", "SUPPORT")):
            await stack.players.set_declared(i, role, 1 + (i + j) % 5)
    for n in range(GAMES):
        lobby_id, match = await start_game(stack)
        sides = await sides_of(stack, match.id)
        winner = "blue" if n % 3 else "red"
        await stack.lobbies.report_result(lobby_id, sides[winner][0], winner)
        loser = "red" if winner == "blue" else "blue"
        await stack.lobbies.confirm_result(lobby_id, sides[loser][0])
        stack.clock.advance(hours=2)
    await db.close()
    return path


def test_the_loader_reads_games_events_and_the_recorded_profiles(snapshot):
    data = load(snapshot)
    assert len(data.games) == GAMES
    assert len(data.names) >= 10 and len(data.stored_mu) == 10
    game = data.games[0]
    assert len(game.players) == 10
    assert all(p.proficiency is not None for p in game.players)
    assert {p.side for p in game.players} == {"blue", "red"}


def test_the_baseline_replay_reproduces_the_bots_stored_ratings(snapshot):
    data = load(snapshot)
    baseline = run_variant(data, BASELINE)
    diff = check_against_stored(data, baseline)
    assert diff is not None and diff < 1e-9
    assert len(baseline.scores) == GAMES
    assert all(0 < s.p_blue < 1 for s in baseline.scores)
    assert baseline.unrecorded == 0


def test_the_baseline_compared_with_itself_gains_nothing(snapshot):
    data = load(snapshot)
    baseline = run_variant(data, BASELINE)
    paired = compare(baseline, run_variant(data, BASELINE))
    assert paired.mean_gain == 0
    assert paired.better == 0 and paired.games == GAMES


def test_a_bigger_loss_scale_changes_the_prediction_but_not_the_ratings(snapshot):
    data = load(snapshot)
    base = run_variant(data, BASELINE)
    heavier = run_variant(data, Variant("x3", loss_scale=3.0))
    assert [s.p_blue for s in heavier.scores] != [s.p_blue for s in base.scores]
    # Role-blind update: the table only matters for predictions, so H is untouched.
    assert {p: f.mu for p, f in heavier.final.items()} == {p: f.mu for p, f in base.final.items()}


def test_with_no_loss_at_all_the_prediction_ignores_roles(snapshot):
    data = load(snapshot)
    none = run_variant(data, Variant("none", loss_scale=0.0))
    # No loss: the strength is E on every role, so only E decides.
    assert all(0.3 < s.p_blue < 0.7 for s in none.scores)


def test_the_role_aware_variant_moves_ratings(snapshot):
    data = load(snapshot)
    base = run_variant(data, BASELINE)
    aware = run_variant(data, Variant("aware", role_aware=True))
    assert {p: f.mu for p, f in aware.final.items()} != {p: f.mu for p, f in base.final.items()}


def test_a_sigma_floor_makes_ratings_move_more(snapshot):
    data = load(snapshot)
    base = run_variant(data, BASELINE)
    floored = run_variant(data, Variant("floor", sigma_floor=4.0))
    start = {e.player_id: e.mu for e in data.events}  # one adjust per player in this fixture
    moved = lambda r: sum(abs(f.mu - start[p]) for p, f in r.final.items())  # noqa: E731
    assert moved(floored) > moved(base)


def test_a_player_without_a_recorded_profile_counts_as_unknown(snapshot):
    data = load(snapshot)
    stripped = Dataset(
        data.events,
        [
            type(g)(
                g.id,
                g.confirmed_at,
                g.winner,
                tuple(type(p)(p.player_id, p.side, p.role, p.rank_mu, None) for p in g.players),
            )
            for g in data.games
        ],
        data.names,
        data.stored_mu,
    )
    result = run_variant(stripped, BASELINE)
    assert result.unrecorded == 10 * GAMES


def test_log_loss_brier_and_the_paired_comparison():
    def result(ps):
        scores = [GameScore(i, p, True) for i, p in enumerate(ps)]
        return VariantResult(BASELINE, scores, {})

    sure, coin = result([0.9, 0.9, 0.9, 0.9]), result([0.5, 0.5, 0.5, 0.5])
    assert log_loss(coin) == pytest.approx(math.log(2))
    assert brier(coin) == pytest.approx(0.25)
    assert log_loss(sure) < log_loss(coin) and brier(sure) < brier(coin)
    paired = compare(coin, sure)
    assert paired.mean_gain == pytest.approx(math.log(0.9) - math.log(0.5))
    assert paired.better == 4 and paired.stderr == pytest.approx(0)
    assert compare(coin, result([0.5] * 4)).mean_gain == 0
    one = result([0.9])
    assert compare(result([0.5]), one).stderr is None  # a single game has no spread


def test_describe_names_what_differs():
    assert describe(BASELINE) == "as the bot works now"
    text = describe(Variant("v", loss_scale=2, role_aware=True, sigma_floor=3, d_up=5, d_down=1))
    for part in ("loss x2", "role-aware", "sigma floor 3", "d_up 5", "d_down 1"):
        assert part in text


def test_the_report_has_the_table_the_check_and_the_honesty_lines(snapshot):
    data = load(snapshot)
    results = [run_variant(data, v) for v in (BASELINE, *PRESETS)]
    text = summary(data, results)
    assert f"{GAMES} confirmed games" in text
    assert "reproduces the bot's stored ratings exactly" in text
    assert "hint, not a finding" in text  # few games
    assert "independent" in text
    for v in PRESETS:
        assert v.name in text
    table = detail(data, results[0], results[3])
    assert "tier now -> tier variant" in table


def test_variant_parsing():
    v = parse_variant("my try: loss=2, aware, floor=3, d_up=4.5, d_down=1")
    assert v == Variant("my try", 2.0, True, 3.0, 4.5, 1.0)
    assert parse_variant("plain") == Variant("plain")
    with pytest.raises(ValueError):
        parse_variant("x: nonsense=1")


def test_the_command_line_prints_a_report_and_validates_input(snapshot, capsys, tmp_path):
    assert main(["--db", str(snapshot)]) == 0
    out = capsys.readouterr().out
    assert "confirmed games" in out and "loss x2" in out
    assert main(["--db", str(snapshot), "--variant", "mine: loss=1.5", "--detail", "mine"]) == 0
    out = capsys.readouterr().out
    assert "mine" in out and "tier now" in out
    assert main(["--db", str(snapshot), "--detail", "nope"]) == 2
    assert main(["--db", str(snapshot), "--variant", "x: bad=1"]) == 2
    assert main(["--db", str(tmp_path / "missing.sqlite3")]) == 2


async def test_a_database_without_games_says_so(tmp_path, capsys):
    path = tmp_path / "empty.sqlite3"
    db = Database(path)
    await db.connect()
    await db.close()
    assert main(["--db", str(path)]) == 0
    assert "No confirmed games" in capsys.readouterr().out
