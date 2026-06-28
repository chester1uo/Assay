#!/usr/bin/env python
"""Single entry point to run the Assay test suite — whole, or by module/group.

Sets ``PYTHONPATH=src`` for you and dispatches to pytest. Tests are grouped into
module folders under ``tests/`` (``data`` / ``engine`` / ``factors``).

Usage
-----
    python scripts/run_tests.py                  # whole suite
    python scripts/run_tests.py all              # same as above
    python scripts/run_tests.py engine           # only tests/engine
    python scripts/run_tests.py data             # only tests/data
    python scripts/run_tests.py factors          # only tests/factors
    python scripts/run_tests.py diagnostics      # a single module: tests/**/test_diagnostics.py
    python scripts/run_tests.py operators        # all modules whose name contains "operators"

Extra pytest args pass straight through:
    python scripts/run_tests.py engine -k corr -x
    python scripts/run_tests.py all -q
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
GROUPS = {"data", "engine", "factors"}


def _resolve(target: str) -> list[str]:
    """Return the pytest path/marker args for a target selector."""
    if target in (None, "", "all"):
        return [str(TESTS)]
    if target in GROUPS:
        return [str(TESTS / target)]
    # otherwise treat as a module keyword: exact test_<target>.py, else fuzzy match
    exact = sorted(TESTS.rglob(f"test_{target}.py"))
    fuzzy = sorted(p for p in TESTS.rglob("test_*.py") if target in p.stem)
    matches = exact or fuzzy
    if not matches:
        sys.exit(f"run_tests: no test module matching {target!r} under {TESTS}")
    return [str(m) for m in matches]


def main(argv: list[str]) -> int:
    args = argv[1:]
    target = args[0] if args and not args[0].startswith("-") else None
    passthrough = args[1:] if target is not None else args

    env = dict(os.environ)
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    cmd = [sys.executable, "-m", "pytest", *_resolve(target), *passthrough]
    print("·", " ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
