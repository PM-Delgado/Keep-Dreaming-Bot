"""Smoke tests for tools/ladder_sim.py (a dev tool, loaded by path: tools/ is not a package)."""

import importlib.util
from pathlib import Path

import pytest

from bot.features.ratings.params import MatchmakingParams

SPEC = importlib.util.spec_from_file_location(
    "ladder_sim", Path(__file__).parent.parent / "tools" / "ladder_sim.py"
)
assert SPEC is not None
assert SPEC.loader is not None
sim = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sim)


def test_simulation_is_reproducible_and_respects_the_drift_bounds():
    params = MatchmakingParams(d_down=2.0, d_up=3.0)
    first = sim.simulate(12, 40, seed=5, params=params)
    again = sim.simulate(12, 40, seed=5, params=params)
    assert first == again
    assert all(-2.0 - 1e-9 <= d <= 3.0 + 1e-9 for d in first.drift)
    assert sum(first.tiers.values()) == 12


def test_parameters_change_the_outcome():
    tight = sim.simulate(12, 60, seed=5, params=MatchmakingParams(d_down=0.2, d_up=0.2))
    loose = sim.simulate(12, 60, seed=5, params=MatchmakingParams(d_down=9.0, d_up=9.0))
    assert tight.max_abs_drift <= 0.2 + 1e-9
    assert loose.max_abs_drift > tight.max_abs_drift


def test_cli_prints_the_distribution(capsys: pytest.CaptureFixture[str]):
    sim.main(["--players", "12", "--matches", "20", "--seed", "2", "--tier-shift", "1"])
    out = capsys.readouterr().out
    assert "In-house tier distribution" in out
    assert "E - R drift" in out
    assert "Lopsided matches" in out
