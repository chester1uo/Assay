"""Executable dataset expression contracts, independent of an agent strategy."""
from __future__ import annotations


def normalize_policy(policy: dict) -> dict:
    required = {"fields", "operators", "max_window", "max_cumulative_lookback"}
    if not isinstance(policy, dict) or set(policy) != required:
        raise ValueError(f"expression_policy must contain exactly {sorted(required)}")
    result = dict(policy)
    for key in ("fields", "operators"):
        values = policy[key]
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f"expression_policy.{key} must be a nonempty list of names")
        result[key] = sorted(set(values))
    for key in ("max_window", "max_cumulative_lookback"):
        if type(policy[key]) is not int or policy[key] < 1:
            raise ValueError(f"expression_policy.{key} must be a positive integer")
    return result


def check_expression(expr: str, policy: dict) -> dict:
    """Check canonical fields, operators and nested trading-bar lookbacks.

    Window semantics match AlphaBench's stable OHLCV grammar: lag/returns use d,
    rolling statistics use d-1, DEMA uses 2*(d-1), quantile's window is argument 2.
    Unresolvable temporal windows are rejected, never counted as zero lookback.
    """
    from assay.engine import lint, parse, iter_fields, iter_ops
    from assay.engine.ast import ast_to_dict

    diagnostics = lint(expr).to_dict()
    result = dict(ok=False, diagnostics=diagnostics, ast=None, issues=[])
    if not diagnostics["ok"]:
        result["issues"].append("expression did not pass engine lint")
        return result
    node = parse(expr)
    ast = ast_to_dict(node)
    result["ast"] = ast
    fields, operators = iter_fields(node), iter_ops(node)
    forbidden_fields = sorted(fields - set(policy["fields"]))
    forbidden_operators = sorted(operators - set(policy["operators"]))
    if forbidden_fields:
        result["issues"].append(f"forbidden fields: {forbidden_fields}")
    if forbidden_operators:
        result["issues"].append(f"forbidden operators: {forbidden_operators}")
    windows = []

    def lookback(tree):
        children = tree.get("args", [])
        depth = max((lookback(c) for c in children), default=0)
        op = tree.get("op", "")
        if not op.startswith("ts_"):
            return depth
        index = 1 if op == "ts_quantile" else len(children) - 1
        window = children[index].get("lit") if 0 <= index < len(children) else None
        if type(window) is not int or window < 1:
            result["issues"].append(f"{op} requires a positive literal integer window")
            return depth
        windows.append(window)
        if window > policy["max_window"]:
            result["issues"].append(f"{op} window {window} exceeds {policy['max_window']}")
        increment = window if op in {"ts_delay", "ts_delta", "ts_returns", "ts_log_returns"} else window - 1
        if op == "ts_dema":
            increment *= 2
        return depth + increment

    depth = lookback(ast)
    if depth > policy["max_cumulative_lookback"]:
        result["issues"].append(f"cumulative lookback {depth} exceeds {policy['max_cumulative_lookback']}")
    result.update(ok=not result["issues"], windows=windows, cumulative_lookback=depth)
    return result
