"""Offline lifecycle demo with synthetic prices and two simulated agent clients.

No provider calls or API credentials. Uses the real store, evaluator and workspace.
Run: PYTHONPATH=src python scripts/demo_agent_research.py [--output EMPTY_DIR]
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import polars as pl

import assay
from assay.config import AssayConfig
from assay.data.schemas import (
    PRICE_RAW_SCHEMA, UNIVERSE_SNAPSHOTS_SCHEMA,
    price_partition_path, universe_snapshots_path,
)
from bench_alpha101 import build_synthetic_panel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="An empty directory; defaults to a new temporary directory")
    args = parser.parse_args()
    root = args.output or Path(tempfile.mkdtemp(prefix="assay-agent-demo-"))
    if root.exists() and any(root.iterdir()):
        parser.error("output directory must be empty")
    root.mkdir(parents=True, exist_ok=True)
    panel, _ = build_synthetic_panel(16, 100, seed=7)
    panel = panel.with_columns(pl.Series("date", panel["date"].to_list(), dtype=pl.Date))
    raw = panel.with_columns(pl.col("date").alias("as_of_date"),
                             pl.lit(1).alias("transactions"), pl.lit("synthetic-demo").alias("source_id"))
    dates = panel["date"].to_list()
    start, end = min(dates), max(dates)
    for year, month in sorted({(d.year, d.month) for d in dates}):
        path = price_partition_path(root, "US", year, month)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw.filter((pl.col("date").dt.year() == year) & (pl.col("date").dt.month() == month)).select(
            [pl.col(k).cast(v) for k, v in PRICE_RAW_SCHEMA.items()]
        ).write_parquet(path)
    path = universe_snapshots_path(root, "US")
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([dict(index_id="NASDAQ100", effective_date=start, as_of_date=start,
                       symbols=sorted(panel["symbol"].unique().to_list()))],
                 schema=UNIVERSE_SNAPSHOTS_SCHEMA).write_parquet(path)

    config = AssayConfig.for_tests(root)
    assay.init(config)
    ws = assay.research_workspace()
    ds = ws.register_dataset("synthetic-demo", "NASDAQ100", [str(start), str(end)], horizons=[1, 5])
    backends = ws.list_backends(probe=True)
    assert backends[0]["availability"]["status"] == "data_present"
    session = ws.create_session(ds["id"], "Demonstrate persistent factor research", max_experiments=4)
    sid = session["id"]
    first = ws.evaluate(sid, "-cs_rank(ts_returns(close,5))", "Test reversal",
                        request_id="first", agent="simulated-agent-a")
    ws.evaluate(sid, "unknown_operator(close)", "Demonstrate diagnostic retention",
                request_id="failure", agent="simulated-agent-a")
    ws.add_note(sid, "Inspect the failed attempt and compare a shorter lookback next.", agent="simulated-agent-a")
    manifest = ws.export_session(sid)
    context = ws.research_context(sid)
    (root / "handoff-manifest.json").write_text(json.dumps(manifest, indent=2))
    (root / "handoff-context.json").write_text(json.dumps(context, indent=2))

    # Replace the service: all research context must be recovered from SQLite.
    assay.init(config)
    resumed = assay.research_workspace()
    recovered = resumed.get_session(sid)
    assert len(recovered["experiments"]) == 2 and recovered["notes"]
    assert resumed.verify_manifest(manifest)
    assert resumed.export_session(sid) == manifest
    assert resumed.research_context(sid) == context
    assert resumed.evaluate(sid, "-cs_rank(ts_returns(close,5))", "Test reversal",
                            request_id="first")["id"] == first["id"]
    second = resumed.evaluate(sid, "-cs_rank(ts_returns(close,3))", "Compare a shorter lookback",
                               request_id="revision", parent_id=first["id"], agent="simulated-agent-b")
    replay = resumed.replay_experiment(first["id"], sid, request_id="explicit-replay", agent="simulated-agent-b")
    assert replay["status"] == "completed" and replay["replay_of"] == first["id"]
    assert replay["report"]["rank_ic"] == first["report"]["rank_ic"]
    assert resumed.replay_experiment(first["id"], sid, request_id="explicit-replay") == replay
    comparison = resumed.compare_experiments([first["id"], second["id"]])
    final = resumed.get_session(sid)
    assert final["remaining_experiments"] == 0
    (root / "final-manifest.json").write_text(json.dumps(resumed.export_session(sid), indent=2))
    print(json.dumps(dict(
        data="synthetic; no investment evidence", workspace=str(root.resolve()), session_id=sid,
        restored_attempts=len(recovered["experiments"]),
        backend_discovery=backends, handoff_verified=True, manifest_verified=True,
        replay_idempotency_verified=True, provider_calls=0,
        statuses=[e["status"] for e in final["experiments"]],
        remaining_experiments=final["remaining_experiments"], comparison=comparison,
    ), indent=2))


if __name__ == "__main__":
    main()
