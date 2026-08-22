"""Runs the standalone regression that pins per-statement validation.

That script predates this suite and is documented in CLAUDE.md as runnable on
its own; keeping it collectible here means CI cannot forget it.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_period_gap_regression():
    spec = importlib.util.spec_from_file_location(
        "period_gap_regression",
        ROOT / "backend" / "processor" / "tests" / "test_period_gap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()
