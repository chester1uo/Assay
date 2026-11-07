"""Reuse AlphaBench V2's patched Assay client as a research execution backend.

Install alphabench-v2 or expose its src directory on PYTHONPATH. No benchmark
runner, inventory, results or mutable campaign state is loaded by this adapter.
"""
from __future__ import annotations


class AlphaBenchBackend:
    def __init__(self, client):
        self.client = client

    def describe(self) -> dict:
        return dict(kind="remote_patched_assay", endpoint=self.client.base_url,
                    description="AlphaBench-compatible patched Assay execution backend",
                    fields=["open", "high", "low", "close", "volume"], frequencies=["1d"],
                    execution_modes=["next_open", "next_close", "close"],
                    supports_universe_freeze=True, supports_immutable_snapshots=False)

    def probe(self) -> dict:
        provenance = self.client.provenance()
        return dict(status="service_available" if provenance["health"].get("status") == "ok" else "unhealthy",
                    health=provenance["health"], markets=provenance["status"].get("markets", []),
                    calendar_dates=len(provenance["data_calendar"]),
                    scope="service inventory; not a requested-period coverage check")

    def evaluate(self, expr: str, dataset: dict) -> dict:
        from alphabench_v2.assay import EvaluationSpec

        if dataset.get("snapshot_id"):
            raise ValueError("this backend cannot resolve immutable snapshot IDs")
        options = dataset.get("backend_options", {})
        required = {"endpoint", "universe_as_of", "universe_symbols_hash", "n_symbols"}
        if set(options) != required:
            raise ValueError(f"backend_options must contain exactly {sorted(required)}")
        if options["endpoint"].rstrip("/") != self.client.base_url:
            raise ValueError("dataset endpoint does not match the registered backend")
        if self.client.base_url != "http://127.0.0.1:8002":
            raise ValueError("AlphaBench backend requires the patched localhost:8002 service")
        spec = EvaluationSpec(
            universe=dataset["universe"], period=tuple(dataset["period"]),
            as_of=dataset["as_of"], horizons=tuple(dataset["horizons"]),
            universe_as_of=options["universe_as_of"],
            execution=dataset["execution"], adj=dataset["adj"],
        )
        report = self.client.evaluate(expr, spec)
        # Preserve diagnostic failures as reports. Successful numeric evaluations
        # must match the frozen axis and period before an agent sees their scores.
        if not report.get("failure_mode"):
            expected = {
                "universe_id": dataset["universe"], "eval_period": dataset["period"],
                "universe_as_of": options["universe_as_of"],
                "universe_symbols_hash": options["universe_symbols_hash"],
                "n_symbols": options["n_symbols"], "data_as_of": dataset["period"][1],
                "execution": dataset["execution"],
            }
            for field, value in expected.items():
                if report.get(field) != value:
                    raise ValueError(f"patched Assay contract mismatch: {field}")
        return report
