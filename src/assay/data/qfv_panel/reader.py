# Vendored from QFinVerse src/qfinverse/data/daily/reader.py by scripts/sync_qfv_panel.py.
# Do not edit here: change QFinVerse and sync.
"""Reading the daily panel store.

:class:`DailyPanel` answers "these fields, for these securities, over these dates"
with dense ``(dates x securities)`` float64 matrices:

>>> p = DailyPanel("/srv/qfinverse/data/daily_panel")
>>> sids = p.sids(["AAPL", "MSFT"], on="2020-06-30")
>>> m = p.matrix(["close", "volume"], sids, "2020-01-01", "2020-12-31", adjust="split")
>>> m.data["close"].shape          # (trading days, 2)

Adjustment happens here, not in the files. ``adjust="none"`` gives prices as
traded; ``"split"`` puts every bar on the share basis of the window's last day;
``"total"`` also takes cash dividends out of earlier prices (so the series is a
total-return index). Only events with an ex-date on or before the window's last
day count, so a window that ends in the past never sees later splits: reading
2020 with ``end="2020-12-31"`` gives the same 2020 prices it gave on 2021-01-01.
Volume is put on the same share basis by splits; dividends leave it alone; trade
counts are never adjusted.

Derived fields: ``dvol`` (close x volume as traded, dollars) and ``ret`` (daily
total return, independent of the anchor).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np

from .layout import FIELDS, PRICE_FIELDS, read_current, read_manifest
from .securities import RunIndex

DERIVED = ("dvol", "ret")
ADJUST = ("none", "split", "total")


def _d64(day) -> np.datetime64:
    return np.datetime64(str(day)[:10], "D")


class Block:
    """One calendar year: ``dates`` (rows) x ``sids`` (columns), fields memory-mapped."""

    def __init__(self, path: Path):
        self.path = path
        self.dates = np.load(path / "dates.npy")
        self.sids = np.load(path / "sids.npy")
        self._fields: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()

    def field(self, name: str) -> np.ndarray:
        arr = self._fields.get(name)
        if arr is None:
            with self._lock:
                arr = self._fields.get(name)
                if arr is None:
                    arr = np.load(self.path / f"{name}.npy", mmap_mode="r")
                    self._fields[name] = arr
        return arr

    def columns(self, sids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(positions in this block, mask of ``sids`` present)."""
        pos = np.searchsorted(self.sids, sids)
        pos = np.minimum(pos, len(self.sids) - 1) if len(self.sids) else np.zeros(len(sids), dtype=np.intp)
        ok = (self.sids[pos] == sids) if len(self.sids) else np.zeros(len(sids), dtype=bool)
        return pos, ok


class BlockSet:
    """All year blocks of a version, in date order."""

    def __init__(self, root: Path, manifest: dict):
        self.blocks = [Block(root / manifest["blocks"][y]["dir"]) for y in sorted(manifest["blocks"])]
        self.starts = np.array([b.dates[0] for b in self.blocks], dtype="datetime64[D]") if self.blocks else \
            np.array([], dtype="datetime64[D]")

    def gather(self, fields: Sequence[str], sids: np.ndarray, start: np.datetime64, end: np.datetime64,
               dtype=np.float64) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """Dates in [start, end] and, per field, a (dates x len(sids)) matrix (NaN where no bar)."""
        parts_dates, parts = [], {f: [] for f in fields}
        sids = np.asarray(sids, dtype=np.int32)
        for b in self.blocks:
            if b.dates[-1] < start or b.dates[0] > end:
                continue
            r0, r1 = np.searchsorted(b.dates, start), np.searchsorted(b.dates, end, side="right")
            if r1 <= r0:
                continue
            pos, ok = b.columns(sids)
            parts_dates.append(b.dates[r0:r1])
            for f in fields:
                block = b.field(f)[r0:r1]
                out = np.full((r1 - r0, len(sids)), np.nan, dtype=dtype)
                if ok.any():
                    out[:, ok] = block[:, pos[ok]]
                parts[f].append(out)
        if not parts_dates:
            return np.array([], dtype="datetime64[D]"), {f: np.empty((0, len(sids)), dtype=dtype) for f in fields}
        return np.concatenate(parts_dates), {f: np.concatenate(v) for f, v in parts.items()}


@dataclass
class Panel:
    dates: np.ndarray                 # datetime64[D]
    sids: np.ndarray                  # int32
    data: dict[str, np.ndarray]       # field -> (dates x sids) float64
    adjust: str
    anchor: Optional[str]             # the share basis (window's last day) for adjusted prices


class DailyPanel:
    def __init__(self, root):
        self.root = Path(root)
        self._open()

    def _open(self) -> None:
        self.version = read_current(self.root)
        self.manifest = read_manifest(self.version)
        self.calendar = np.load(self.version / "calendar.npy")
        self.blocks = BlockSet(self.root, self.manifest)
        self._tables: dict[str, dict] = {}
        self._runs: Optional[RunIndex] = None
        self._events: Optional[dict] = None
        self._universes: Optional[dict] = None

    def refresh(self) -> bool:
        """Switch to a newer version if one was published; True when it did."""
        if read_current(self.root) != self.version:
            self._open()
            return True
        return False

    # ── reference tables ─────────────────────────────────────────────────
    def _table(self, name: str) -> dict:
        if name not in self._tables:
            import pyarrow.parquet as pq

            t = pq.read_table(self.version / f"{name}.parquet")
            self._tables[name] = {c: t[c].to_numpy(zero_copy_only=False) for c in t.column_names}
        return self._tables[name]

    @property
    def first_date(self) -> str:
        return self.manifest["first_date"]

    @property
    def last_date(self) -> str:
        return self.manifest["last_date"]

    def securities(self) -> dict:
        """Columns sid, ticker, label, kind, first_date, last_date (row = sid)."""
        return self._table("securities")

    def sids(self, tickers: Iterable[str], on: Optional[str] = None) -> np.ndarray:
        """Sids the ``tickers`` meant on date ``on`` (default: the last date): the latest
        security that used the ticker by then, also after it stopped trading; ``-1``
        when the ticker was never used. Labels (``ABC~2004-03``) are accepted too."""
        if self._runs is None:
            self._runs = RunIndex({k: [str(x) if k != "sid" else int(x) for x in v]
                                   for k, v in self._table("tickers").items()})
            self._by_label = {str(lab): i for i, lab in enumerate(self.securities()["label"])}
        day = str(on or self.last_date)[:10]
        out = []
        for t in tickers:
            t = str(t)
            sid = self._by_label.get(t) if "~" in t else self._runs.latest(t.upper(), day)
            out.append(sid)
        return np.array([-1 if s is None else s for s in out], dtype=np.int32)

    def labels(self, sids: Sequence[int]) -> list[str]:
        """Unique display names: the ticker, or ``TICKER~YYYY-MM`` for an older holder of a reused ticker."""
        lab = self.securities()["label"]
        return [str(lab[s]) if 0 <= s < len(lab) else "" for s in np.asarray(sids)]

    # ── universes ────────────────────────────────────────────────────────
    def universe_names(self) -> list[str]:
        return sorted(self.manifest.get("universes", {}))

    def _universe_table(self) -> dict:
        if self._universes is None:
            import pyarrow.parquet as pq

            t = pq.read_table(self.version / "universes.parquet")
            out: dict[str, tuple[np.ndarray, list[np.ndarray]]] = {}
            names = t["universe"].to_pylist()
            dates = t["effective_date"].to_numpy(zero_copy_only=False).astype("datetime64[D]")
            members = t["sids"].to_pylist()
            for name in sorted(set(names)):
                idx = [i for i, n in enumerate(names) if n == name]
                idx.sort(key=lambda i: dates[i])
                out[name] = (dates[idx], [np.asarray(members[i], dtype=np.int32) for i in idx])
            self._universes = out
        return self._universes

    def universe(self, name: str, on: str) -> np.ndarray:
        """Members of ``name`` in effect on ``on`` (sorted sids; empty before the first rebalance)."""
        dates, members = self._universe_table()[name]
        i = int(np.searchsorted(dates, _d64(on), side="right")) - 1
        return members[i] if i >= 0 else np.array([], dtype=np.int32)

    def universe_span(self, name: str, start: str, end: str) -> np.ndarray:
        """Every sid that was a member at some point in [start, end]."""
        dates, members = self._universe_table()[name]
        lo = max(int(np.searchsorted(dates, _d64(start), side="right")) - 1, 0)
        hi = int(np.searchsorted(dates, _d64(end), side="right"))
        sets = members[lo:hi]
        return np.unique(np.concatenate(sets)) if sets else np.array([], dtype=np.int32)

    def membership(self, name: str, dates: np.ndarray, sids: np.ndarray) -> np.ndarray:
        """Bool (dates x sids): was the security a member on that date."""
        udates, members = self._universe_table()[name]
        out = np.zeros((len(dates), len(sids)), dtype=bool)
        which = np.searchsorted(udates, dates.astype("datetime64[D]"), side="right") - 1
        for k in np.unique(which):
            if k < 0:
                continue
            rows = which == k
            out[np.ix_(rows, np.isin(sids, members[k]))] = True
        return out

    # ── corporate actions ────────────────────────────────────────────────
    def events(self) -> dict:
        """Resolved corporate actions (see :mod:`.build`): sid, ex_date, kind, factor, applied, ..."""
        if self._events is None:
            ev = dict(self._table("events"))
            ev["ex_date"] = ev["ex_date"].astype("datetime64[D]")
            self._events = ev
        return self._events

    def _back_factors(self, dates: np.ndarray, sids: np.ndarray, kinds: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
        """(price, volume) multipliers putting each row on the share basis of the last row."""
        T, N = len(dates), len(sids)
        price = np.ones((T + 1, N))
        volume = np.ones((T + 1, N))
        if not T or not N:
            return price[:T], volume[:T]
        ev = self.events()
        m = ev["applied"] & np.isin(ev["kind"], kinds) & (ev["ex_date"] > dates[0]) & (ev["ex_date"] <= dates[-1])
        order = np.argsort(sids, kind="stable")
        pos = np.searchsorted(sids[order], ev["sid"][m])
        pos = np.minimum(pos, N - 1)
        hit = sids[order][pos] == ev["sid"][m]
        cols = order[pos[hit]]
        rows = np.searchsorted(dates, ev["ex_date"][m][hit])     # first row on or after the ex-date
        np.multiply.at(price, (rows, cols), ev["factor"][m][hit])
        np.multiply.at(volume, (rows, cols), ev["volume_factor"][m][hit])
        # row t takes every event after it: product over rows t+1..T
        price = np.cumprod(price[::-1], axis=0)[::-1][1:]
        volume = np.cumprod(volume[::-1], axis=0)[::-1][1:]
        return price, volume

    # ── matrices ─────────────────────────────────────────────────────────
    def matrix(self, fields: Sequence[str], sids, start: str, end: str, adjust: str = "split",
               as_of: Optional[str] = None) -> Panel:
        """Fields for ``sids`` over trading days in [start, min(end, as_of)]."""
        if adjust not in ADJUST:
            raise ValueError(f"adjust is one of {ADJUST}")
        unknown = [f for f in fields if f not in FIELDS and f not in DERIVED]
        if unknown:
            raise ValueError(f"unknown fields {unknown}; available: {list(FIELDS) + list(DERIVED)}")
        sids = np.asarray(sids, dtype=np.int32)
        stop = min(_d64(end), _d64(as_of)) if as_of else _d64(end)
        lo = _d64(start)
        need = set(fields) - set(DERIVED)
        if "dvol" in fields:
            need |= {"close", "volume"}
        if "ret" in fields:
            need |= {"close"}
            # one bar before the window gives the first day's return
            k = int(np.searchsorted(self.calendar, lo)) - 1
            if k >= 0:
                lo = self.calendar[k]
        dates, raw = self.blocks.gather(sorted(need), sids, lo, stop)
        data: dict[str, np.ndarray] = {}
        if "dvol" in fields:
            data["dvol"] = raw["close"] * raw["volume"]
        if "ret" in fields:
            pf, _ = self._back_factors(dates, sids, ("split", "dividend"))
            c = raw["close"] * pf
            r = np.full_like(c, np.nan)
            r[1:] = c[1:] / c[:-1] - 1.0
            data["ret"] = r
        if adjust != "none":
            pf, vf = self._back_factors(dates, sids, ("split",) if adjust == "split" else ("split", "dividend"))
            for f in PRICE_FIELDS:
                if f in raw:
                    raw[f] = raw[f] * pf
            if "volume" in raw:
                raw["volume"] = raw["volume"] * vf
        for f in fields:
            if f not in data:
                data[f] = raw[f]
        cut = dates >= _d64(start)
        if not cut.all():
            dates = dates[cut]
            data = {f: v[cut] for f, v in data.items()}
        return Panel(dates, sids, data, adjust, str(dates[-1]) if len(dates) and adjust != "none" else None)

    def info(self) -> dict:
        m = dict(self.manifest)
        m["version"] = self.version.name
        return json.loads(json.dumps(m))
