"""Exercise durable research through a real stdio MCP subprocess and restart."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path


def test_research_over_stdio_and_restart(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("assay_reuse_smoke", root / "scripts/smoke_alphabench_reuse.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def atomic(path, value):
        path.write_text(json.dumps(value))

    env = dict(os.environ, PYTHONPATH=str(root / "src"), ASSAY_DATA_DIR=str(tmp_path / "data"),
               PYTHONDONTWRITEBYTECODE="1")
    params = StdioServerParameters(command=sys.executable, args=["-m", "assay.mcp.server"],
                                    env=env, cwd=str(tmp_path))

    async def scenario():
        with (tmp_path / "worker.log").open("a") as log:
            async with stdio_client(params, errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = module.MCPTools(session, tmp_path / "first.json", atomic)
                    backends = await tools.call("assay_backend_list", probe=True)
                    assert backends["backends"][0]["name"] == "assay"
                    assert backends["backends"][0]["availability"]["status"] == "no_prepared_data"
                    lint = await tools.call("assay_lint", expr="ts_mean(ts_returns(close,5),20)")
                    assert lint["ast"] == {"op": "ts_mean", "args": [
                        {"op": "ts_returns", "args": [{"field": "close"}, {"lit": 5}]}, {"lit": 20}]}
                    invalid = await tools.call("assay_lint", expr="unknown_operator(close)")
                    assert invalid["ast"] is None
                    dataset = await tools.call("assay_dataset_register", name="transport-test",
                        universe="NASDAQ100", period=["2024-01-01", "2024-06-30"])
                    created = await tools.call("assay_research_session_create", dataset_id=dataset["id"],
                        goal="Check durable MCP transport", max_experiments=2)
                    sid = created["id"]
                    await tools.call("assay_research_note", session_id=sid, text="Resume after restart", agent="a")
                    args = dict(session_id=sid, expr="ts_returns(close,5)", hypothesis="No-data retention probe",
                                request_id="one", agent="a")
                    attempt = await tools.call("assay_experiment_run", **args)
                    assert attempt["status"] == "failed"  # no ingested store, retained as a report
                    before = await tools.call("assay_research_session_get", session_id=sid)
                    assert before["remaining_experiments"] == 1
                    context = await tools.call("assay_research_context", session_id=sid, limit=1)
                    assert context["counts"]["statuses"] == {"failed": 1}
                    assert context["remaining_experiments"] == 1
                    manifest = await tools.call("assay_research_export", session_id=sid)
                    from assay.research import ResearchWorkspace
                    assert ResearchWorkspace.verify_manifest(manifest)
                    target = await tools.call("assay_research_session_create", dataset_id=dataset["id"],
                                              goal="Explicit rerun", max_experiments=1)
                    replay_args = dict(experiment_id=attempt["id"], session_id=target["id"],
                                       request_id="replay", agent="b")
                    replay = await tools.call("assay_experiment_replay", **replay_args)
                    assert replay["status"] == "failed" and replay["replay_of"] == attempt["id"]
            async with stdio_client(params, errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = module.MCPTools(session, tmp_path / "second.json", atomic)
                    after = await tools.call("assay_research_session_get", session_id=sid)
                    assert after == before
                    assert await tools.call("assay_research_export", session_id=sid) == manifest
                    assert await tools.call("assay_research_context", session_id=sid, limit=1) == context
                    assert await tools.call("assay_experiment_replay", **replay_args) == replay
                    assert await tools.call("assay_experiment_run", **args) == attempt
                    assert (await tools.call("assay_research_session_get", session_id=sid)) == before
            migrated_params = StdioServerParameters(command=sys.executable, args=["-m", "assay.mcp.server"],
                env=dict(env, ASSAY_DATA_DIR=str(tmp_path / "migrated-data")), cwd=str(tmp_path))
            async with stdio_client(migrated_params, errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = module.MCPTools(session, tmp_path / "migrated.json", atomic)
                    assert await tools.call("assay_research_import", manifest=manifest) == before
                    assert await tools.call("assay_research_import", manifest=manifest) == before
                    assert await tools.call("assay_research_export", session_id=sid) == manifest
                    assert await tools.call("assay_experiment_run", **args) == attempt
                    continued = await tools.call("assay_experiment_run", **dict(args, request_id="next"))
                    assert continued["id"] != attempt["id"] and continued["status"] == "failed"
                    final = await tools.call("assay_research_session_get", session_id=sid)
                    assert final["remaining_experiments"] == 0 and len(final["experiments"]) == 2

    asyncio.run(scenario())
