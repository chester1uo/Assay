"""Durable, agent-independent research state and an execution-backend boundary.

Datasets are immutable evaluation specifications, not copies of market data.
Every attempt has its own identity; saving the same expression never overwrites
an earlier experiment. SQLite transactions enforce session budgets across workers.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


class ResearchBackend(Protocol):
    """Backend implementations return a JSON-safe factor report, including failures."""

    def evaluate(self, expr: str, dataset: dict) -> dict: ...


class AssayBackend:
    """Adapter to the existing Assay factor evaluator; no model dependency."""

    def __init__(self, service):
        self.service = service

    def describe(self) -> dict:
        return dict(kind="in_process", description="Assay daily factor evaluator",
                    fields=["open", "high", "low", "close", "volume"], frequencies=["1d"],
                    execution_modes=["next_open", "next_close", "close"],
                    known_universes=sorted(self.service._UNIVERSE_MARKET),
                    supports_universe_freeze=False, supports_immutable_snapshots=False)

    def probe(self) -> dict:
        """Inspect prepared local files; no downloads or credential access."""
        from assay.data.schemas import price_root, universe_snapshots_path
        import polars as pl

        cfg = self.service.config
        roots = {cfg.market: cfg.data_dir, **cfg.market_dirs}
        markets = []
        for market, root in sorted(roots.items()):
            files = list(price_root(Path(root), market).glob("**/price_raw.parquet"))
            snapshots = universe_snapshots_path(Path(root), market)
            universes = sorted(pl.read_parquet(snapshots, columns=["index_id"])["index_id"].unique().to_list()) if snapshots.is_file() else []
            markets.append(dict(market=market, price_partitions=len(files), universe_ids=universes))
        return dict(status="data_present" if any(m["price_partitions"] and m["universe_ids"] for m in markets) else "no_prepared_data",
                    markets=markets, scope="file inventory; not a requested-period coverage check")

    def evaluate(self, expr: str, dataset: dict) -> dict:
        if dataset.get("snapshot_id"):
            raise ValueError("the Assay backend cannot resolve immutable snapshot IDs")
        if dataset.get("backend_options"):
            raise ValueError("the built-in Assay backend does not support backend_options")
        return self.service.evaluate(
            expr, universe=dataset["universe"], period=tuple(dataset["period"]),
            as_of=dataset["as_of"], horizons=dataset["horizons"],
            execution=dataset["execution"], adj=dataset["adj"], save=False,
        ).to_dict()


class ResearchWorkspace:
    """Persistent datasets, sessions, notes and independent experiment attempts.

    Backend names are durable; backend objects are registered per process. An
    interrupted experiment stays ``running`` until explicitly abandoned. Retrying
    with the same request_id returns that attempt without executing it again.
    """

    def __init__(self, path: str | Path, backends: dict[str, ResearchBackend] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backends = dict(backends or {})
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"unsupported research schema version: {version}")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    goal TEXT NOT NULL, agent TEXT NOT NULL, budget INTEGER NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    request_id TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(session_id, request_id));
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id), payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS experiments_session ON experiments(session_id);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def register_backend(self, name: str, backend: ResearchBackend) -> None:
        if not name.strip():
            raise ValueError("backend name must not be empty")
        if name in self.backends:
            raise ValueError(f"backend already registered: {name}")
        self.backends[name] = backend

    def list_backends(self, *, probe: bool = False) -> list[dict]:
        """Discover registered and persisted-but-missing backends without exposing credentials.

        Capabilities are declared by adapters. Availability is checked only when
        requested; a healthy endpoint is not proof of coverage for every dataset.
        """
        if type(probe) is not bool:
            raise ValueError("probe must be a boolean")
        with self._db() as db:
            datasets = [json.loads(r[0]) for r in db.execute("SELECT payload FROM datasets")]
        names = sorted(set(self.backends) | {d["backend"] for d in datasets})
        allowed = {"kind", "description", "endpoint", "fields", "frequencies", "execution_modes",
                   "known_universes", "supports_universe_freeze", "supports_immutable_snapshots"}
        rows = []
        for name in names:
            backend = self.backends.get(name)
            row = dict(name=name, registered=backend is not None,
                       dataset_count=sum(d["backend"] == name for d in datasets),
                       capabilities={}, availability={"status": "not_checked"})
            if backend is None:
                row["availability"] = {"status": "backend_not_registered"}
            else:
                try:
                    describe = getattr(backend, "describe", None)
                    if callable(describe):
                        metadata = describe()
                        if not isinstance(metadata, dict):
                            raise TypeError("backend describe must return a dictionary")
                        row["capabilities"] = json.loads(_json({k: v for k, v in metadata.items() if k in allowed}))
                    if probe:
                        check = getattr(backend, "probe", None)
                        status = check() if callable(check) else {"status": "probe_not_supported"}
                        if not isinstance(status, dict):
                            raise TypeError("backend probe must return a dictionary")
                        row["availability"] = json.loads(_json(status))
                except Exception as exc:
                    row["availability"] = dict(status="error", error_type=type(exc).__name__, message=str(exc))
            rows.append(row)
        return rows

    def register_dataset(
        self, name: str, universe: str, period: list[str], *, backend: str = "assay",
        as_of: str | None = None, horizons: list[int] | None = None,
        execution: str = "next_open", adj: str = "split",
        snapshot_id: str | None = None, description: str = "",
        backend_options: dict[str, Any] | None = None,
        score_period: list[str] | None = None, score_dates: list[str] | None = None,
        expression_policy: dict[str, Any] | None = None,
    ) -> dict:
        """Register a fixed daily evaluation spec; snapshot_id is caller-supplied.

        Registration does not download data or certify an immutable data snapshot.
        Backends are responsible for resolving/verifying the supplied snapshot.
        """
        if not name.strip() or not universe.strip():
            raise ValueError("name and universe are required")
        if backend not in self.backends:
            raise ValueError(f"backend is not registered: {backend}")
        if isinstance(self.backends[backend], AssayBackend) and snapshot_id:
            raise ValueError("the Assay backend cannot resolve immutable snapshot IDs")
        if backend_options is not None and not isinstance(backend_options, dict):
            raise ValueError("backend_options must be a dictionary")
        if isinstance(self.backends[backend], AssayBackend) and backend_options:
            raise ValueError("the built-in Assay backend does not support backend_options")
        if len(period) != 2:
            raise ValueError("period must contain start and end ISO dates")
        start, end = (date.fromisoformat(d) for d in period)
        cutoff = date.fromisoformat(as_of) if as_of else end
        if start > end or cutoff < end:
            raise ValueError("require period start <= end <= as_of")
        horizons = [1, 5, 10, 20] if horizons is None else horizons
        if not horizons or any(type(h) is not int or h <= 0 for h in horizons):
            raise ValueError("horizons must be positive integer trading-day counts")
        if execution not in {"next_open", "next_close", "close"}:
            raise ValueError("unsupported execution model")
        if adj not in {"none", "split", "total", "forward"}:
            raise ValueError("unsupported adjustment mode")
        spec = dict(backend=backend, universe=universe, period=[str(start), str(end)],
                    as_of=str(cutoff), horizons=sorted(set(horizons)),
                    execution=execution, adj=adj, snapshot_id=snapshot_id)
        if backend_options:
            spec["backend_options"] = backend_options
        from assay.research_scoring import validate_score_window

        score = validate_score_window(spec["period"], spec["horizons"], score_period, score_dates)
        if score:
            spec.update(score)
        if expression_policy is not None:
            from assay.research_policy import normalize_policy

            spec["expression_policy"] = normalize_policy(expression_policy)
        result = dict(spec, id=_id("ds_"), name=name, description=description,
                      spec_hash=hashlib.sha256(_json(spec).encode()).hexdigest(),
                      created_at=_now())
        with self._db() as db:
            db.execute("INSERT INTO datasets VALUES (?, ?)", (result["id"], _json(result)))
        return result

    def list_datasets(self) -> list[dict]:
        with self._db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM datasets ORDER BY rowid")]

    def get_dataset(self, dataset_id: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT payload FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown dataset: {dataset_id}")
        return json.loads(row[0])

    def create_session(self, dataset_id: str, goal: str, *, agent: str = "unspecified",
                       max_experiments: int = 20) -> dict:
        self.get_dataset(dataset_id)
        if not goal.strip():
            raise ValueError("research goal is required")
        if type(max_experiments) is not int or max_experiments < 1:
            raise ValueError("max_experiments must be a positive integer")
        sid = _id("research_")
        with self._db() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                       (sid, dataset_id, goal, agent, max_experiments, _now()))
        return self.get_session(sid)

    def list_sessions(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT s.*, COUNT(e.id) AS attempts FROM sessions s "
                              "LEFT JOIN experiments e ON e.session_id=s.id "
                              "GROUP BY s.id ORDER BY s.created_at").fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: str) -> dict:
        with self._db() as db:
            db.execute("BEGIN")
            return self._session_state(db, session_id)

    @staticmethod
    def _session_state(db, session_id):
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown research session: {session_id}")
        experiments = [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM experiments WHERE session_id=? ORDER BY rowid", (session_id,))]
        notes = [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM notes WHERE session_id=? ORDER BY id", (session_id,))]
        dataset = json.loads(db.execute("SELECT payload FROM datasets WHERE id=?", (row["dataset_id"],)).fetchone()[0])
        return dict(row, dataset=dataset,
                    experiments=experiments, notes=notes,
                    remaining_experiments=max(0, row["budget"] - len(experiments)))

    def import_session(self, manifest: dict) -> dict:
        """Atomically restore terminal research records, retaining IDs and spent budget.

        Exact repeated imports are idempotent; conflicting identities are rejected.
        Backend registration and market data are separate from record migration.
        """
        if not self.verify_manifest(manifest):
            raise ValueError("invalid session manifest integrity or version")
        from assay.research_manifest import validate_state

        state = json.loads(_json(manifest["state"]))
        try:
            validate_state(state)
        except (TypeError, KeyError, OverflowError) as exc:
            raise ValueError("invalid session manifest structure") from exc
        dataset = state["dataset"]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM sessions WHERE id=?", (state["id"],)).fetchone():
                if self._session_state(db, state["id"]) != state:
                    raise ValueError("session identity conflicts with existing state")
                return state
            existing = db.execute("SELECT payload FROM datasets WHERE id=?", (dataset["id"],)).fetchone()
            if existing and json.loads(existing[0]) != dataset:
                raise ValueError("dataset identity conflicts with existing state")
            for attempt in state["experiments"]:
                if db.execute("SELECT 1 FROM experiments WHERE id=?", (attempt["id"],)).fetchone():
                    raise ValueError("experiment identity conflicts with existing state")
            incoming = {a["id"]: a for a in state["experiments"]}
            local = {r["id"]: json.loads(r["payload"]) for r in db.execute("SELECT id, payload FROM experiments")}
            # Resolve cross-session replay links in either import order. Missing
            # sources remain references; known conflicting sources are rejected.
            for attempt in [*incoming.values(), *local.values()]:
                source_id = attempt.get("replay_of")
                if not source_id or (attempt["id"] not in incoming and source_id not in incoming):
                    continue
                source = incoming.get(source_id) or local.get(source_id)
                if source is not None and (source["status"] == "running" or any(
                    attempt[k] != source[k] for k in ("expr", "hypothesis", "spec_hash")
                )):
                    raise ValueError("replay source conflicts with imported or existing state")
            graph = {**local, **incoming}
            resolved = set()
            for start in incoming:
                current, visiting = start, set()
                while current in graph and current not in resolved:
                    if current in visiting:
                        raise ValueError("cyclic replay provenance in session manifest")
                    visiting.add(current)
                    current = graph[current].get("replay_of")
                resolved.update(visiting)
            if not existing:
                db.execute("INSERT INTO datasets VALUES (?, ?)", (dataset["id"], _json(dataset)))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                       tuple(state[k] for k in ("id", "dataset_id", "goal", "agent", "budget", "created_at")))
            for attempt in state["experiments"]:
                db.execute("INSERT INTO experiments VALUES (?, ?, ?, ?)",
                           (attempt["id"], state["id"], attempt["request_id"], _json(attempt)))
            for note in state["notes"]:
                db.execute("INSERT INTO notes(session_id, payload) VALUES (?, ?)", (state["id"], _json(note)))
            return self._session_state(db, state["id"])

    def research_context(self, session_id: str, *, limit: int = 10) -> dict:
        """Compact, evidence-linked context for another agent, excluding metric series."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        from assay.research_context import build_context

        state = self.get_session(session_id)
        backend = next(r for r in self.list_backends() if r["name"] == state["dataset"]["backend"])
        return build_context(state, backend, limit)

    def export_session(self, session_id: str) -> dict:
        """Export a consistent full research-state manifest; no market data or credentials."""
        payload = dict(schema_version=1, kind="assay_research_session_manifest", state=self.get_session(session_id))
        return dict(payload, manifest_hash=hashlib.sha256(_json(payload).encode()).hexdigest())

    @staticmethod
    def verify_manifest(manifest: dict) -> bool:
        """Check content integrity, not authenticity or physical-data reproducibility."""
        if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "kind", "state", "manifest_hash"}:
            return False
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or manifest["kind"] != "assay_research_session_manifest":
            return False
        if not isinstance(manifest["state"], dict) or not isinstance(manifest["manifest_hash"], str):
            return False
        payload = {k: v for k, v in manifest.items() if k != "manifest_hash"}
        try:
            return hashlib.sha256(_json(payload).encode()).hexdigest() == manifest["manifest_hash"]
        except (TypeError, ValueError):
            return False

    def replay_experiment(self, experiment_id: str, session_id: str, *, request_id: str,
                          agent: str = "unspecified") -> dict:
        """Explicitly rerun a saved attempt under an identical dataset specification.

        This consumes a new attempt. Equal configuration does not imply frozen
        data or equal results. Original records are never overwritten.
        """
        source = self.get_experiment(experiment_id)
        return self.evaluate(session_id, source["expr"], source["hypothesis"], request_id=request_id,
                             agent=agent, replay_of=experiment_id,
                             parent_id=experiment_id if source["session_id"] == session_id else None)

    def add_note(self, session_id: str, text: str, *, agent: str = "unspecified") -> dict:
        if not text.strip():
            raise ValueError("note must not be empty")
        note = dict(text=text, agent=agent, created_at=_now())
        with self._db() as db:
            if db.execute("SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                raise ValueError(f"unknown research session: {session_id}")
            db.execute("INSERT INTO notes(session_id, payload) VALUES (?, ?)",
                       (session_id, _json(note)))
        return note

    def get_experiment(self, experiment_id: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT payload FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown experiment: {experiment_id}")
        return json.loads(row[0])

    def evaluate(self, session_id: str, expr: str, hypothesis: str, *, request_id: str,
                 agent: str = "unspecified", parent_id: str | None = None,
                 replay_of: str | None = None) -> dict:
        """Reserve budget before executing; keep failures and retry identities.

        A request_id identifies a single attempt within a session. Reuse it after
        a transport timeout; a new request_id explicitly starts another attempt.
        """
        if not expr.strip() or not hypothesis.strip() or not request_id.strip():
            raise ValueError("expr, hypothesis and request_id are required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload FROM experiments WHERE session_id=? AND request_id=?",
                                  (session_id, request_id)).fetchone()
            if existing:
                result = json.loads(existing[0])
                if any(result.get(k) != v for k, v in dict(expr=expr, hypothesis=hypothesis, parent_id=parent_id, replay_of=replay_of).items()):
                    raise ValueError("request_id already belongs to different experiment inputs")
                return result
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise ValueError(f"unknown research session: {session_id}")
            dataset = json.loads(db.execute("SELECT payload FROM datasets WHERE id=?",
                                           (session["dataset_id"],)).fetchone()[0])
            if replay_of is not None:
                old = db.execute("SELECT payload FROM experiments WHERE id=?", (replay_of,)).fetchone()
                if old is None:
                    raise ValueError("replay source experiment does not exist")
                source = json.loads(old[0])
                if source["status"] not in {"completed", "failed", "abandoned"}:
                    raise ValueError("cannot replay a running experiment")
                if source["spec_hash"] != dataset["spec_hash"]:
                    raise ValueError("replay requires an identical dataset specification")
                if source["expr"] != expr or source["hypothesis"] != hypothesis:
                    raise ValueError("replay cannot change the source expression or hypothesis")
            backend = self.backends.get(dataset["backend"])
            if backend is None:
                raise ValueError(f"register backend {dataset['backend']!r} in this process before evaluation")
            if parent_id:
                parent = db.execute("SELECT session_id FROM experiments WHERE id=?", (parent_id,)).fetchone()
                if parent is None or parent[0] != session_id:
                    raise ValueError("parent experiment must belong to this research session")
            count = db.execute("SELECT COUNT(*) FROM experiments WHERE session_id=?", (session_id,)).fetchone()[0]
            if count >= session["budget"]:
                raise ValueError("research session experiment budget exhausted")
            result = dict(id=_id("exp_"), session_id=session_id, request_id=request_id,
                          dataset_id=dataset["id"], spec_hash=dataset["spec_hash"],
                          expr=expr, hypothesis=hypothesis, parent_id=parent_id, agent=agent,
                          status="running", started_at=_now(), finished_at=None, report=None, error=None)
            if replay_of is not None:
                result["replay_of"] = replay_of
            db.execute("INSERT INTO experiments VALUES (?, ?, ?, ?)",
                       (result["id"], session_id, request_id, _json(result)))
        try:
            policy = dataset.get("expression_policy")
            if policy:
                from assay.research_policy import check_expression

                result["policy_check"] = json.loads(_json(check_expression(expr, policy)))
            if policy and not result["policy_check"]["ok"]:
                report = dict(failure_mode="EXPRESSION_POLICY_VIOLATION", ic=None, rank_ic=None,
                              suggestion="; ".join(result["policy_check"]["issues"]),
                              diagnostics=result["policy_check"])
            else:
                report = backend.evaluate(expr, dataset)
            if not isinstance(report, dict):
                raise TypeError("research backend must return a report dictionary")
            # Canonical JSON types make the immediate response identical to a
            # response read after restart (e.g. integer horizon keys become strings).
            report = json.loads(_json(report))
            if dataset.get("score_period"):
                from assay.research_scoring import apply_score_window

                result["panel_report"] = report
                report = json.loads(_json(apply_score_window(report, dataset)))
            result.update(report=report, status="failed" if report.get("failure_mode") else "completed")
        except Exception as exc:
            result.update(status="failed", error=dict(type=type(exc).__name__, message=str(exc)))
        result["finished_at"] = _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            # An explicit abandon can race a slow backend; retain the terminal state.
            current = json.loads(db.execute("SELECT payload FROM experiments WHERE id=?", (result["id"],)).fetchone()[0])
            if current["status"] != "running":
                return current
            db.execute("UPDATE experiments SET payload=? WHERE id=?", (_json(result), result["id"]))
        return result

    def abandon_experiment(self, experiment_id: str, reason: str) -> dict:
        """Explicitly close an interrupted attempt; its budget remains consumed."""
        if not reason.strip():
            raise ValueError("abandon reason is required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM experiments WHERE id=?", (experiment_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown experiment: {experiment_id}")
            result = json.loads(row[0])
            if result["status"] != "running":
                raise ValueError("only running experiments can be abandoned")
            result.update(status="abandoned", finished_at=_now(),
                          error=dict(type="Abandoned", message=reason))
            db.execute("UPDATE experiments SET payload=? WHERE id=?", (_json(result), experiment_id))
        return result

    def compare_experiments(self, experiment_ids: list[str]) -> dict:
        if len(experiment_ids) < 2 or len(set(experiment_ids)) != len(experiment_ids):
            raise ValueError("provide at least two distinct experiment IDs")
        experiments = [self.get_experiment(eid) for eid in experiment_ids]
        comparable = len({e["spec_hash"] for e in experiments}) == 1
        metrics = ("rank_ic", "rank_icir", "ic", "icir", "turnover_1d", "decay_halflife_days")
        return dict(comparable=comparable,
                    caveat="Matching specs do not certify unchanged underlying data or out-of-sample validity.",
                    experiments=[dict(id=e["id"], expr=e["expr"], status=e["status"],
                                      dataset_id=e["dataset_id"], spec_hash=e["spec_hash"],
                                      failure_mode=(e["report"] or {}).get("failure_mode"), error=e["error"],
                                      metrics={k: (e["report"] or {}).get(k) for k in metrics})
                                 for e in experiments])
