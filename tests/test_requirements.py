"""requirements.txt (read by the Railway build) must list the same packages as pyproject.toml."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _lines(raw: list[str]) -> set[str]:
    return {line.split("#")[0].strip() for line in raw if line.split("#")[0].strip()}


def test_requirements_match_pyproject() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = _lines(pyproject["project"]["dependencies"])
    pinned = _lines((ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines())
    assert pinned == declared
