"""Configuration for the Assay data pipeline.

The pipeline runs entirely against a **local** mirror of the MASSIVE dataset
(downloaded out-of-band by the ``downloader_*`` scripts that live alongside the
data); it no longer fetches anything over the network. A project-root ``.env``
file is loaded at import time *without* overriding anything already set in the
shell environment, so the shell profile always wins.

Environment variables (all optional, sensible defaults)::

    MASSIVE_DATA_DIR    root of the local MASSIVE mirror   (default /data/massive_data)
    ASSAY_DATA_DIR      where the prepared parquet stores are written (default ./data)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Repo root = two levels up from this file (src/assay/config.py -> repo root).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Populate ``os.environ`` from a ``KEY=VALUE`` file, never overriding.

    Deliberately tiny (no python-dotenv dependency). Lines that are blank,
    commented (``#``), or missing ``=`` are ignored. Surrounding quotes on the
    value are stripped. ``setdefault`` ensures the real shell environment wins.
    """

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


# Load the project .env once, at import, so config works in plain `python` runs
# even when the shell profile has not been sourced.
_load_dotenv(_PROJECT_ROOT / ".env")


_DEFAULT_SOURCE_DIR = "/data/massive_data"


@dataclass(frozen=True)
class MassiveConfig:
    """On-disk layout of the locally-downloaded MASSIVE dataset.

    ``source_dir`` is the root of the local mirror; the sub-paths below mirror
    the directory layout produced by the downloader scripts::

        {source_dir}/us_stocks_sip/day_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.parquet
        {source_dir}/corporate_actions/splits/{TICKER}.jsonl
        {source_dir}/corporate_actions/dividends/{TICKER}.jsonl
    """

    source_dir: Path = Path(_DEFAULT_SOURCE_DIR)
    # Sub-paths under ``source_dir`` (relative), one per source dataset.
    day_aggs_subdir: str = "us_stocks_sip/day_aggs_v1"
    minute_aggs_subdir: str = "us_stocks_sip/minute_aggs_v1"
    splits_subdir: str = "corporate_actions/splits"
    dividends_subdir: str = "corporate_actions/dividends"

    @property
    def day_aggs_dir(self) -> Path:
        """Directory holding ``{YYYY}/{MM}/{YYYY-MM-DD}.parquet`` day aggregates."""
        return Path(self.source_dir) / self.day_aggs_subdir

    @property
    def minute_aggs_dir(self) -> Path:
        """Directory holding ``{YYYY}/{MM}/{YYYY-MM-DD}.parquet`` minute aggregates."""
        return Path(self.source_dir) / self.minute_aggs_subdir

    @property
    def splits_dir(self) -> Path:
        """Directory holding per-ticker ``{TICKER}.jsonl`` split records."""
        return Path(self.source_dir) / self.splits_subdir

    @property
    def dividends_dir(self) -> Path:
        """Directory holding per-ticker ``{TICKER}.jsonl`` dividend records."""
        return Path(self.source_dir) / self.dividends_subdir

    @classmethod
    def from_env(cls) -> "MassiveConfig":
        source = os.environ.get("MASSIVE_DATA_DIR", _DEFAULT_SOURCE_DIR)
        return cls(source_dir=Path(source).expanduser())


@dataclass
class AssayConfig:
    """Top-level pipeline configuration — the shared contract every layer reads.

    Construction stays backward compatible: ``AssayConfig(massive=..., data_dir=...)``
    still works, and :meth:`from_env` reads ``ASSAY_DATA_DIR`` plus ``MASSIVE_DATA_DIR``
    (the local source root). ``massive`` is optional so offline callers (tests, the
    WebUI, a pure library walk that only reads the prepared parquet stores) can build
    a config without pointing at a source mirror via :meth:`for_tests`.

    Directory fields default to ``None`` and resolve under ``data_dir`` via the
    :attr:`library_path` / :attr:`cache_path` properties, so a bare ``data_dir`` is
    enough to locate the factor library and the two-level evaluation cache
    (engineering-docs section 5). The dataclass is intentionally **non-frozen**: the
    derived paths are computed lazily by properties rather than baked in at init, and
    callers may override directories after construction.
    """

    massive: MassiveConfig | None = None
    data_dir: Path = field(default_factory=lambda: Path("data"))
    market: str = "US"

    # Per-market store roots for multi-market serving (e.g. {"CN": "/data/.../data_cn"}).
    # When a requested universe maps to a market listed here, that market's data_dir is
    # used; otherwise the canonical ``data_dir`` is read (which may itself hold several
    # ``price_raw/market=.../`` subtrees). Lets one server serve US + A-share at once.
    market_dirs: dict[str, Path] = field(default_factory=dict)

    # Storage roots (default under data_dir — see *_path properties below).
    library_dir: Path | None = None
    cache_dir: Path | None = None

    # Cache / parallelism budgets (engineering-docs section 5).
    l1_memory_gb: float = 4.0
    l2_max_gb: float = 20.0
    n_workers: int = 8

    # Evaluation defaults (engineering-docs sections 6-7; SDK keyword defaults).
    default_universe: str = "NASDAQ100"
    default_period: tuple[str, str] = ("2020-01-01", "2024-12-31")
    default_horizons: tuple[int, ...] = (1, 5, 10, 20)
    default_execution: str = "next_open"
    default_adj: str = "split"

    # Granularity defaults (minute-backtesting design). ``default_frequency`` keeps
    # the daily path the default; ``default_horizons_minute`` are bar horizons used
    # when a request resolves to an intraday frequency; ``annualization_basis``
    # selects daily-aggregated ("daily") vs per-bar ("bar") metric annualization.
    default_frequency: str = "1d"
    default_horizons_minute: tuple[int, ...] = (1, 5, 30, 390)
    annualization_basis: str = "daily"

    def __post_init__(self) -> None:
        # Normalise paths to Path so str inputs and ~ both work uniformly.
        self.data_dir = Path(self.data_dir).expanduser()
        if self.library_dir is not None:
            self.library_dir = Path(self.library_dir).expanduser()
        if self.cache_dir is not None:
            self.cache_dir = Path(self.cache_dir).expanduser()
        # Normalise per-market dirs to expanded Paths (keys upper-cased).
        self.market_dirs = {
            str(k).upper(): Path(v).expanduser() for k, v in (self.market_dirs or {}).items()
        }

    @property
    def library_path(self) -> Path:
        """Factor-library root: ``library_dir`` if set, else ``data_dir/'library'``."""
        return self.library_dir if self.library_dir is not None else self.data_dir / "library"

    @property
    def cache_path(self) -> Path:
        """Evaluation-cache root: ``cache_dir`` if set, else ``data_dir/'cache'``."""
        return self.cache_dir if self.cache_dir is not None else self.data_dir / "cache"

    @classmethod
    def from_env(cls) -> "AssayConfig":
        data_dir = Path(os.environ.get("ASSAY_DATA_DIR", "data")).expanduser().resolve()
        # Optional per-market stores: ASSAY_DATA_DIR_CN / _US / _HK point a market at
        # its own prepared store, so one server serves several markets at once.
        market_dirs: dict[str, Path] = {}
        for mk in ("US", "CN", "HK", "A"):
            val = os.environ.get(f"ASSAY_DATA_DIR_{mk}")
            if val:
                market_dirs[mk] = Path(val).expanduser().resolve()
        return cls(massive=MassiveConfig.from_env(), data_dir=data_dir, market_dirs=market_dirs)

    @classmethod
    def for_tests(cls, data_dir: Path | str, **overrides) -> "AssayConfig":
        """Build an offline config (``massive=None``) for tests and pure-library use.

        Requires no ``MASSIVE_*`` environment; ``data_dir`` is the only mandatory
        argument. Any field may be overridden via keyword (e.g. ``n_workers=1``).
        """
        return cls(massive=None, data_dir=Path(data_dir).expanduser(), **overrides)
