"""Versioned canonical parquet panels, including dated universe membership."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import polars as pl

from assay.data.schemas import price_root, universe_snapshots_path
from .datastore import DataStore, _as_date, _months_in_range

MANIFEST = "SNAPSHOT.json"


def publish_snapshot(root: str | Path, markets=("CN", "HK")) -> dict:
    """Publish content identities after an offline build, before syncing workers.

    No path names, mtimes or host-specific state enter a snapshot identity.
    Prices, corporate actions, membership and execution inputs all participate.
    """
    root = Path(root)
    entries = {}
    for market in markets:
        files = sorted(p for p in root.rglob("*.parquet") if f"market={market}" in p.parts)
        if not files:
            raise ValueError(f"no canonical files for {market}")
        hashes = []
        for p in files:
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            hashes.append([p.relative_to(root).as_posix(), h.hexdigest()])
        digest = hashlib.sha256(json.dumps(hashes, separators=(",", ":")).encode()).hexdigest()
        prices = sorted(price_root(root, market).glob("**/price_raw.parquet"))
        stats = pl.scan_parquet(prices).select("date", "symbol").collect()
        dates = [str(d) for d in sorted(stats["date"].unique().to_list())]
        pools = pl.read_parquet(universe_snapshots_path(root, market))["index_id"].unique().sort().to_list()
        entries[market] = {
            "version": f"assay-parquet:{market}:{digest}", "files": hashes,
            "dates": dates, "first_date": dates[0], "last_date": dates[-1],
            "trading_days": len(dates), "symbols_in_store": stats["symbol"].n_unique(),
            "rows": stats.height, "universes": pools,
        }
    manifest = {"format": "assay-canonical-snapshot/1", "markets": entries}
    temporary = root / (MANIFEST + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(root / MANIFEST)
    return manifest


class CanonicalPanelStore(DataStore):
    """The engine/ADES matrix interface over Assay's CN/HK parquet stores."""

    def __init__(self, config=None):
        super().__init__(config)
        self.market = self.config.market
        self._manifest_stamp = None
        self._metadata = None
        self._file_stamps = {}
        self.refresh()

    def refresh(self, **kwargs):
        path = self.config.data_dir / MANIFEST
        if not path.exists():
            return False
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._manifest_stamp:
            for p, expected in self._file_stamps.items():
                s = p.stat()
                if (s.st_size, s.st_mtime_ns, s.st_ino) != expected:
                    raise ValueError("canonical data changed without publishing a new snapshot")
            return False
        manifest = json.loads(path.read_text())
        if manifest.get("format") != "assay-canonical-snapshot/1":
            raise ValueError(f"unsupported canonical snapshot format: {path}")
        metadata = manifest["markets"].get(self.market)
        stamps = {}
        if metadata:
            for relative, expected in metadata["files"]:
                p = self.config.data_dir / relative
                before = p.stat()
                h = hashlib.sha256()
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                after = p.stat()
                state = lambda s: (s.st_size, s.st_mtime_ns, s.st_ino)
                if h.hexdigest() != expected or state(before) != state(after):
                    raise ValueError(f"canonical snapshot checksum mismatch: {relative}")
                stamps[p] = state(after)
        self._metadata = metadata
        self._file_stamps = stamps
        self._manifest_stamp = stamp
        return True

    @property
    def snapshot_id(self):
        self.refresh()
        return self._metadata["version"] if self._metadata else None

    def price_span(self):
        if self._metadata:
            return {k: self._metadata[k] for k in ("first_date", "last_date", "trading_days", "symbols_in_store")} | {
                "ingested": True, "last_sync": self._metadata["last_date"], "version": self.snapshot_id,
            }
        files = sorted(price_root(self.config.data_dir, self.market).glob("**/price_raw.parquet"))
        if not files:
            return {"first_date": None, "last_date": None, "trading_days": 0,
                    "symbols_in_store": 0, "last_sync": None, "ingested": False}
        frame = pl.scan_parquet(files).select("date", "symbol", "as_of_date").collect()
        return {"first_date": str(frame["date"].min()), "last_date": str(frame["date"].max()),
                "trading_days": frame["date"].n_unique(), "symbols_in_store": frame["symbol"].n_unique(),
                "last_sync": str(frame["as_of_date"].max()), "ingested": bool(frame.height)}

    def universes(self):
        if self._metadata:
            return self._metadata["universes"]
        p = universe_snapshots_path(self.config.data_dir, self.market)
        return pl.read_parquet(p)["index_id"].unique().sort().to_list() if p.exists() else []

    def _memberships(self, universe, as_of):
        return (
            pl.read_parquet(universe_snapshots_path(self.config.data_dir, self.market))
            .filter((pl.col("index_id") == universe.upper()) & (pl.col("as_of_date") <= _as_date(as_of)))
            .with_columns(pl.max_horizontal("effective_date", "as_of_date").alias("_active"))
            .sort(["_active", "effective_date"])
        )

    def get_universe(self, index_id, date, as_of_date):
        rows = self._memberships(index_id, as_of_date).filter(pl.col("_active") <= _as_date(date))
        return list(rows["symbols"][-1]) if rows.height else []

    def universe_span(self, universe, start, end, as_of=None):
        rows = self._memberships(universe, as_of or end).filter(pl.col("_active") <= _as_date(end))
        before = rows.filter(pl.col("_active") <= _as_date(start)).tail(1)
        during = rows.filter(pl.col("_active") > _as_date(start))
        return sorted({s for members in pl.concat([before, during])["symbols"].to_list() for s in members})

    def get_membership(self, universe, dates, symbols, as_of=None):
        dates = np.asarray(dates).astype("datetime64[D]")
        rows = self._memberships(universe, as_of or str(dates[-1]))
        result = np.zeros((len(dates), len(symbols)), dtype=bool)
        if not rows.height:
            return result
        active = rows["_active"].to_numpy().astype("datetime64[D]")
        which = np.searchsorted(active, dates, side="right") - 1
        columns = {str(s): i for i, s in enumerate(symbols)}
        for index in np.unique(which[which >= 0]):
            selected = [columns[s] for s in rows["symbols"][int(index)] if s in columns]
            result[np.ix_(np.flatnonzero(which == index), selected)] = True
        return result

    def _dates(self, start, end, as_of):
        if self._metadata:
            dates = np.asarray(self._metadata["dates"], dtype="datetime64[D]")
            return dates[(dates >= np.datetime64(str(start))) & (dates <= np.datetime64(str(end)))
                         & (dates <= np.datetime64(str(as_of)))]
        from assay.data.schemas import price_partition_path
        paths = [price_partition_path(self.config.data_dir, self.market, y, m)
                 for y, m in _months_in_range(_as_date(start), _as_date(end))]
        paths = [p for p in paths if p.exists()]
        if not paths:
            return np.array([], dtype="datetime64[D]")
        frame = pl.scan_parquet(paths).filter(
            (pl.col("date") >= _as_date(start)) & (pl.col("date") <= _as_date(end))
            & (pl.col("as_of_date") <= _as_date(as_of))
        ).select("date").unique().sort("date").collect()
        return frame["date"].to_numpy().astype("datetime64[D]")

    def get_matrices(self, fields, symbols, start_date, end_date, as_of_date, adj="split"):
        frame = self.get_panel(fields, symbols, start_date, end_date, as_of_date, adj)
        dates = self._dates(start_date, end_date, as_of_date)
        labels = np.asarray(sorted(str(s) for s in symbols))
        matrices = {field: np.full((len(dates), len(labels)), np.nan) for field in fields}
        if frame.height and len(dates) and len(labels):
            ti = np.searchsorted(dates, frame["date"].to_numpy().astype("datetime64[D]"))
            si = np.searchsorted(labels, frame["symbol"].to_numpy().astype(str))
            for field in fields:
                matrices[field][ti, si] = frame[field].to_numpy()
        return dates, labels, matrices
