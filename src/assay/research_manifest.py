"""Validate record migration separately from hash integrity and backend execution."""
import hashlib
import json
from datetime import date, datetime


def validate_state(state, *, allow_running=False):
    def require(ok, message):
        if not ok:
            raise ValueError(f"invalid session manifest: {message}")

    def strings(record, keys):
        require(all(isinstance(record.get(k), str) and record[k].strip() for k in keys),
                f"required strings: {', '.join(keys)}")

    def timestamp(value):
        require(isinstance(value, str), "timestamp must be a string")
        datetime.fromisoformat(value)

    fields = {"id", "dataset_id", "goal", "agent", "budget", "created_at",
              "dataset", "experiments", "notes", "remaining_experiments", "decisions"}
    require(fields <= set(state) <= fields | {'checkpoints'}, "state fields")
    strings(state, ("id", "dataset_id", "goal"))
    require(isinstance(state["agent"], str), "agent must be a string")
    timestamp(state["created_at"])
    require(type(state["budget"]) is int and state["budget"] > 0, "positive integer budget")
    dataset = state["dataset"]
    require(isinstance(dataset, dict), "dataset must be an object")
    metadata = {"id", "name", "description", "spec_hash", "created_at"}
    required = {"backend", "universe", "period", "as_of", "horizons", "execution", "adj", "snapshot_id"}
    optional = {"backend_options", "score_period", "score_dates", "expression_policy", "portfolio", "evaluation_protocol", "portfolio_accounting_protocol"}
    require(metadata | required <= set(dataset) <= metadata | required | optional, "dataset fields")
    strings(dataset, ("id", "name", "backend", "universe", "spec_hash"))
    require(dataset["id"] == state["dataset_id"], "dataset identity")
    require(isinstance(dataset["description"], str), "dataset description")
    timestamp(dataset["created_at"])
    spec = {k: v for k, v in dataset.items() if k not in metadata}
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, allow_nan=False).encode()).hexdigest()
    require(digest == dataset["spec_hash"], "dataset specification hash")
    period = dataset["period"]
    require(isinstance(period, list) and len(period) == 2 and all(isinstance(d, str) for d in period), "period")
    require(isinstance(dataset["as_of"], str), "as_of")
    start, end = map(date.fromisoformat, period)
    require(start <= end <= date.fromisoformat(dataset["as_of"]), "date order")
    horizons = dataset["horizons"]
    require(isinstance(horizons, list) and horizons and all(type(h) is int and h > 0 for h in horizons), "horizons")
    require(horizons == sorted(set(horizons)), "canonical horizons")
    require(dataset["execution"] in ("next_open", "next_close", "close"), "execution")
    require(dataset["adj"] in ("none", "split", "total", "forward"), "adjustment")
    require(dataset["snapshot_id"] is None or isinstance(dataset["snapshot_id"], str), "snapshot ID")
    require("backend_options" not in dataset or isinstance(dataset["backend_options"], dict), "backend options")
    from assay.research_scoring import validate_score_window
    score = validate_score_window(period, horizons, dataset.get("score_period"), dataset.get("score_dates"))
    require((score or {}) == {k: dataset[k] for k in ("score_period", "score_dates") if k in dataset}, "score window")
    if "expression_policy" in dataset:
        from assay.research_policy import normalize_policy
        require(normalize_policy(dataset["expression_policy"]) == dataset["expression_policy"], "expression policy")
    if 'portfolio' in dataset:
        from assay.research_portfolio import normalize_portfolio
        value = dataset['portfolio']
        require(isinstance(value, dict), 'portfolio configuration')
        market = value.get('market', 'US')
        require(normalize_portfolio(dataset, value, market) == value, 'canonical portfolio configuration')
    if 'evaluation_protocol' in dataset:
        require(dataset['evaluation_protocol'] == 'assay-daily-v2', 'evaluation protocol version')
    if 'portfolio_accounting_protocol' in dataset:
        strings(dataset, ('portfolio_accounting_protocol',))
        require(bool(dataset.get('portfolio')) and dataset['portfolio'].get('enabled') is not False,
                'portfolio accounting protocol requires an enabled portfolio')
    attempts, notes = state["experiments"], state["notes"]
    require(isinstance(attempts, list) and isinstance(notes, list), "record lists")
    require(len(attempts) <= state["budget"], "attempts exceed budget")
    require(type(state["remaining_experiments"]) is int and
            state["remaining_experiments"] == state["budget"] - len(attempts), "remaining budget")
    ids, requests = {}, set()
    fields = {"id", "session_id", "request_id", "dataset_id", "spec_hash", "expr", "hypothesis",
              "parent_id", "agent", "status", "started_at", "finished_at", "report", "error"}
    for attempt in attempts:
        require(isinstance(attempt, dict), "attempt must be an object")
        require(fields <= set(attempt) <= fields | {"replay_of", "policy_check", "panel_report", "execution", "cancellation", "factor_id", "portfolio_accounting_protocol"}, "attempt fields")
        strings(attempt, ("id", "request_id", "expr", "hypothesis"))
        if 'portfolio_accounting_protocol' in attempt:
            strings(attempt, ('portfolio_accounting_protocol',))
            require(bool(dataset.get('portfolio')) and
                    dataset['portfolio'].get('enabled') is not False, 'attempt accounting requires a portfolio')
            declared = dataset.get('portfolio_accounting_protocol')
            require(declared is None or attempt['portfolio_accounting_protocol'] == declared,
                    'attempt accounting differs from dataset protocol')
        report = attempt.get('panel_report') or attempt.get('report') or {}
        evidence = report.get('portfolio') if isinstance(report, dict) else None
        lineage = evidence.get('lineage') if isinstance(evidence, dict) else None
        protocol = lineage.get('accounting_protocol') if isinstance(lineage, dict) else None
        if protocol is not None:
            require(isinstance(protocol, str) and bool(protocol.strip()), 'report accounting protocol')
            require(dataset.get('portfolio_accounting_protocol') in (None, protocol),
                    'report accounting differs from dataset protocol')
            require(attempt.get('portfolio_accounting_protocol') in (None, protocol),
                    'attempt accounting differs from report lineage')
        if 'factor_id' in attempt:
            from assay.research_lifecycle import identity
            require(attempt['factor_id'] == identity(attempt['expr'])[0], 'canonical factor identity')
        if "execution" in attempt:
            execution = attempt["execution"]
            require(isinstance(execution, dict), "execution must be an object")
            strings(execution, ("backend", "phase"))
            require(execution["backend"] == dataset["backend"], "execution backend")
            if "data_version" in execution and dataset.get('snapshot_id') is not None:
                require(execution["data_version"] == dataset["snapshot_id"], "execution data version")
            if dataset['backend'] == 'factordb_ades':
                from assay.research_lifecycle import identity
                from assay.factordb.registry import spec_key
                from assay.integrations.factordb import _job_id
                expected = dict(factor_id=identity(attempt['expr'])[0], request_id=attempt['id'],
                    spec=spec_key(dataset['universe'], *dataset['period'], dataset['horizons'], dataset['execution']),
                    job_id=_job_id(attempt['id'], execution.get('owner', 'research')))
                if dataset['execution'] == 'next_close' and 'evaluation_protocol' not in dataset:
                    legacy = json.dumps([dataset['universe'], *dataset['period'], dataset['horizons'], 'next_close'])
                    expected['spec'] = hashlib.blake2b(legacy.encode(), digest_size=8).hexdigest()
                for key, value in expected.items():
                    require(key not in execution or execution[key] == value, f'execution {key} identity')
            if attempt["status"] == "completed":
                strings(execution, ('job_id',))
                require(execution["phase"] == "completed", "execution completion")
                if dataset['backend'] == 'factordb_ades':
                    strings(execution, ("factor_id", "spec", "data_version", "request_id", "owner"))
                    require(isinstance(attempt["report"], dict) and attempt["report"].get("data_version") == execution["data_version"], "report data version")
        require(isinstance(attempt["agent"], str), "attempt agent")
        require(attempt["id"] not in ids and attempt["request_id"] not in requests, "duplicate attempt identity")
        require(attempt["session_id"] == state["id"] and attempt["dataset_id"] == dataset["id"] and
                attempt["spec_hash"] == dataset["spec_hash"], "attempt specification or session")
        require(attempt["status"] in ("completed", "failed", "abandoned", "cancelled") or
                (allow_running and attempt["status"] == "running"), "running attempts cannot be imported; stop the worker and explicitly abandon first")
        timestamp(attempt["started_at"])
        if attempt['status'] == 'running':
            require(attempt['finished_at'] is None and attempt['report'] is None and attempt['error'] is None,
                    'running attempt cannot have terminal evidence')
        else:
            timestamp(attempt["finished_at"])
        for key in ("report", "error", "policy_check", "panel_report"):
            require(attempt.get(key) is None or isinstance(attempt[key], dict), f"{key} must be an object or null")
        require(attempt["status"] != "completed" or (isinstance(attempt["report"], dict) and
                not attempt["report"].get("failure_mode") and attempt["error"] is None), "completed attempt report")
        require(attempt["status"] != "failed" or bool(attempt["error"] or
                (attempt["report"] or {}).get("failure_mode")), "failed attempt needs diagnostics")
        require(attempt["status"] != "abandoned" or
                (attempt["error"] or {}).get("type") == "Abandoned", "abandoned attempt needs reason")
        if "cancellation" in attempt:
            require(isinstance(attempt["cancellation"], dict), "cancellation must be an object")
            strings(attempt["cancellation"], ("reason", "requested_at"))
            timestamp(attempt["cancellation"]["requested_at"])
        if attempt["status"] == "cancelled":
            require((attempt["error"] or {}).get("type") == "Cancelled", "cancelled attempt needs diagnostics")
            require(attempt.get("execution", {}).get("phase") == "cancelled", "cancelled execution phase")
        parent = attempt["parent_id"]
        require(parent is None or (isinstance(parent, str) and parent in ids), "parent must be an earlier session attempt")
        if "replay_of" in attempt:
            strings(attempt, ("replay_of",))
            require(attempt["replay_of"] != attempt["id"], "self replay")
            source = ids.get(attempt["replay_of"])
            if source:
                require(all(attempt[k] == source[k] for k in ("expr", "hypothesis", "spec_hash")), "replay source mismatch")
                from assay.research_portfolio import accounting_protocol
                require(accounting_protocol(attempt, dataset) == accounting_protocol(source, dataset),
                        'replay accounting protocol mismatch')
        ids[attempt["id"]] = attempt
        requests.add(attempt["request_id"])
    positions = {a["id"]: i for i, a in enumerate(attempts)}
    for attempt in attempts:
        source_id = attempt.get("replay_of")
        if source_id in positions:
            require(positions[source_id] < positions[attempt["id"]], "replay must reference earlier attempt")
    from assay.research_checkpoints import validate_checkpoints
    validate_checkpoints(state)
    for note in notes:
        require(isinstance(note, dict) and set(note) == {"text", "agent", "created_at"}, "note fields")
        strings(note, ("text",))
        require(isinstance(note["agent"], str), "note agent")
        timestamp(note["created_at"])
    from assay.research_lifecycle import identity
    events, requests = set(), set()
    require(isinstance(state['decisions'], list), 'decision records')
    for event in state['decisions']:
        require(isinstance(event, dict) and set(event) == {'id', 'session_id', 'factor_id',
                'experiment_id', 'decision', 'reason', 'request_id', 'agent', 'created_at'}, 'decision fields')
        strings(event, ('id', 'factor_id', 'experiment_id', 'reason', 'request_id', 'agent'))
        require(event['id'] not in events and event['request_id'] not in requests, 'duplicate decision identity')
        require(event['session_id'] == state['id'] and event['experiment_id'] in ids, 'decision evidence')
        evidence = ids[event['experiment_id']]
        require(event['factor_id'] == identity(evidence['expr'])[0], 'decision factor identity')
        require(event['decision'] in {'accepted', 'rejected', 'archived', 'candidate'}, 'decision value')
        require(event['decision'] != 'accepted' or evidence['status'] == 'completed', 'acceptance evidence')
        timestamp(event['created_at'])
        events.add(event['id'])
        requests.add(event['request_id'])
