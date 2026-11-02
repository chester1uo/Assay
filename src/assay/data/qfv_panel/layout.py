# Vendored from QFinVerse src/qfinverse/data/daily/layout.py by scripts/sync_qfv_panel.py.
# Do not edit here: change QFinVerse and sync.
"""On-disk format of the daily panel store (``qfv-daily-panel/1``).

::

    <root>/
      CURRENT                       name of the live version directory
      v000007/                      one immutable version per build or update
        manifest.json               format, coverage, blocks, fingerprints
        calendar.npy                datetime64[D]: every trading date in the store
        securities.parquet          sid, ticker, label, kind, first_date, last_date
        events.parquet              corporate actions resolved to sids (see build)
        universes.parquet           universe, effective_date, sids (list<int32>)
      blocks/2026-r7/               one dense block per calendar year (shared by versions)
        dates.npy                   datetime64[D], the year's trading dates (rows)
        sids.npy                    int32, sorted sids that traded that year (columns)
        open.npy high.npy low.npy close.npy volume.npy transactions.npy
                                    float32 (rows x columns), NaN where no bar

Readers open ``CURRENT`` once and read only from that version, so a build that
writes the next version never changes what a running reader sees.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

FORMAT = "qfv-daily-panel/1"
FIELDS = ("open", "high", "low", "close", "volume", "transactions")
PRICE_FIELDS = ("open", "high", "low", "close")

# Kinds: from the ticker reference when it has the security (see reference.py), else
# guessed from CTA/UTP ticker suffixes (where ETFs and common stocks look alike).
KINDS = ("stock", "class", "adr", "fund", "preferred", "warrant", "unit", "right", "when_issued", "other")
EQUITY_KINDS = ("stock", "class")


def kind_of(ticker: str) -> str:
    if re.fullmatch(r"[A-Z]{1,4}", ticker):
        return "stock"
    if re.fullmatch(r"[A-Z]{5}", ticker):
        # Nasdaq fifth letter: W warrant, U unit, R right
        return {"W": "warrant", "U": "unit", "R": "right"}.get(ticker[-1], "stock")
    if re.fullmatch(r"[A-Z]{1,5}\.[A-Z]", ticker):
        return "class"
    if re.search(r"\.WS(\.[A-Z])?$", ticker):
        return "warrant"
    if ticker.endswith(".U"):
        return "unit"
    m = re.fullmatch(r"[A-Z]{1,5}([a-z])[A-Z]?", ticker)
    if m:
        return {"p": "preferred", "r": "right", "w": "when_issued"}.get(m.group(1), "other")
    return "other"


def read_current(root: Path) -> Path:
    name = (root / "CURRENT").read_text().strip()
    return root / name


def write_current(root: Path, version: Path) -> None:
    tmp = root / f".CURRENT.{os.getpid()}"
    tmp.write_text(version.name + "\n")
    os.replace(tmp, root / "CURRENT")


def read_manifest(version: Path) -> dict:
    m = json.loads((version / "manifest.json").read_text())
    if m.get("format") != FORMAT:
        raise ValueError(f"{version}: format {m.get('format')!r}, expected {FORMAT!r}")
    return m


def next_version(root: Path) -> Path:
    taken = [int(p.name[1:]) for p in root.glob("v[0-9]*") if p.name[1:].isdigit()]
    return root / f"v{(max(taken) + 1) if taken else 1:06d}"
