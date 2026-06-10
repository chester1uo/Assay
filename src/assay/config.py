"""Configuration and credential loading for the Assay data pipeline.

Credentials are read from environment variables (the installer appended a
``MASSIVE_*`` block to ``~/.bashrc``). For convenience a project-root ``.env``
file is also loaded at import time *without* overriding anything already set in
the shell environment, so the shell profile always wins.

Required variables (see ``.env.example``)::

    MASSIVE_API_KEY                 REST bearer token  (api.massive.com)
    MASSIVE_S3_ACCESS_KEY_ID        S3 flat-files access key id
    MASSIVE_S3_SECRET_ACCESS_KEY    S3 flat-files secret access key

Optional (sensible defaults)::

    MASSIVE_S3_ENDPOINT     default https://files.massive.com
    MASSIVE_S3_BUCKET       default flatfiles
    MASSIVE_REST_BASE_URL   default https://api.massive.com
    ASSAY_DATA_DIR          default ./data
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


@dataclass(frozen=True)
class MassiveConfig:
    """Connection settings for the MASSIVE data provider."""

    api_key: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_endpoint: str = "https://files.massive.com"
    s3_bucket: str = "flatfiles"
    rest_base_url: str = "https://api.massive.com"
    # S3 object-key prefix for US-stock daily OHLCV flat files. Full key is
    # f"{day_aggs_prefix}/{YYYY}/{MM}/{YYYY-MM-DD}.csv.gz".
    day_aggs_prefix: str = "us_stocks_sip/day_aggs_v1"

    @classmethod
    def from_env(cls) -> "MassiveConfig":
        try:
            return cls(
                api_key=os.environ["MASSIVE_API_KEY"],
                s3_access_key_id=os.environ["MASSIVE_S3_ACCESS_KEY_ID"],
                s3_secret_access_key=os.environ["MASSIVE_S3_SECRET_ACCESS_KEY"],
                s3_endpoint=os.environ.get("MASSIVE_S3_ENDPOINT", cls.s3_endpoint),
                s3_bucket=os.environ.get("MASSIVE_S3_BUCKET", cls.s3_bucket),
                rest_base_url=os.environ.get("MASSIVE_REST_BASE_URL", cls.rest_base_url),
            )
        except KeyError as exc:  # pragma: no cover - exercised via from_env errors
            missing = exc.args[0]
            raise RuntimeError(
                f"Missing required environment variable {missing!r}. "
                "Add the MASSIVE_* exports to ~/.bashrc (see .env.example), run "
                "`source ~/.bashrc`, or create a project .env file."
            ) from exc


@dataclass
class AssayConfig:
    """Top-level pipeline configuration — the shared contract every layer reads.

    Construction stays backward compatible: ``AssayConfig(massive=..., data_dir=...)``
    still works, and :meth:`from_env` reads ``ASSAY_DATA_DIR`` plus the ``MASSIVE_*``
    block. ``massive`` is optional so offline callers (tests, the WebUI, a pure
    library walk) can build a config without credentials via :meth:`for_tests`.

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

    def __post_init__(self) -> None:
        # Normalise paths to Path so str inputs and ~ both work uniformly.
        self.data_dir = Path(self.data_dir).expanduser()
        if self.library_dir is not None:
            self.library_dir = Path(self.library_dir).expanduser()
        if self.cache_dir is not None:
            self.cache_dir = Path(self.cache_dir).expanduser()

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
        return cls(massive=MassiveConfig.from_env(), data_dir=data_dir)

    @classmethod
    def for_tests(cls, data_dir: Path | str, **overrides) -> "AssayConfig":
        """Build an offline config (``massive=None``) for tests and pure-library use.

        Requires no ``MASSIVE_*`` environment; ``data_dir`` is the only mandatory
        argument. Any field may be overridden via keyword (e.g. ``n_workers=1``).
        """
        return cls(massive=None, data_dir=Path(data_dir).expanduser(), **overrides)
