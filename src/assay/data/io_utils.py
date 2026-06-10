"""Small parquet IO helpers shared by the ingesters."""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_parquet_atomic(df: pl.DataFrame, path: Path) -> None:
    """Write ``df`` to ``path`` atomically (write to a temp file, then rename)."""
    ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.write_parquet(tmp, compression="zstd")
    os.replace(tmp, path)


def upsert_parquet(
    path: Path,
    new_df: pl.DataFrame,
    keys: list[str],
    sort_by: list[str] | None = None,
) -> int:
    """Merge ``new_df`` into the parquet at ``path``, new rows winning on ``keys``.

    Append-only by intent: existing rows are preserved unless a new row shares the
    same key, in which case the new value replaces it (idempotent re-ingest).
    Returns the total row count after the merge.
    """
    if path.is_file():
        old = pl.read_parquet(path)
        if set(new_df.columns) != set(old.columns):
            raise ValueError(
                f"schema mismatch upserting {path}: "
                f"existing columns {sorted(old.columns)} != new {sorted(new_df.columns)}. "
                "Delete the store to rebuild, or migrate it deliberately."
            )
        # Normalize column order before concatenating (sets already match).
        new_df = new_df.select(old.columns)
        combined = pl.concat([old, new_df], how="vertical_relaxed")
        combined = combined.unique(subset=keys, keep="last")
    else:
        combined = new_df
    if sort_by:
        combined = combined.sort(sort_by)
    write_parquet_atomic(combined, path)
    return combined.height
