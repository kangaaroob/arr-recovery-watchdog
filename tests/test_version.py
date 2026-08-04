"""The version reported by the application tracks pyproject.toml."""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

import arr_recovery_watchdog


def test_version_matches_installed_metadata() -> None:
    assert arr_recovery_watchdog.__version__ == version("arr-recovery-watchdog")


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert arr_recovery_watchdog.__version__ == declared, (
        "Installed package metadata disagrees with pyproject.toml; "
        "re-run `pip install -e '.[dev]'` after bumping the version"
    )
