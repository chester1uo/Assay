"""Factor-id identity contract: granularity namespacing + frozen legacy digests.

The daily ``compute_factor_id`` digest is the library key (``<factor_id>.json``);
it MUST stay byte-stable forever or existing saved factors silently orphan. These
pins were computed from the pre-minute code.
"""

from __future__ import annotations

import hashlib
import json

from assay.library.report import FactorReport

# Frozen daily report-JSON key shape (design §10: granularity="1d" report keys
# byte-identical to pre-minute). Adding a tagged field (e.g. M5/M7 `granularity`)
# must break this consciously, not drift silently.
_REPORT_KEYS_SHA = "fef929a84a208cdc"

# Pinned legacy digests — frozen daily contract. Do not edit to "make it pass".
_LEGACY = {
    "ts_std(close,10)": "26e74d876b250a1f",
    "close": "310ff200149b44a3",
}


def test_factor_id_legacy_digests_frozen():
    for expr, digest in _LEGACY.items():
        assert FactorReport.compute_factor_id(expr) == digest            # default
        assert FactorReport.compute_factor_id(expr, "1d") == digest      # explicit daily == default


def test_factor_id_granularity_namespaces():
    expr = "ts_std(close,10)"
    daily = FactorReport.compute_factor_id(expr, "1d")
    minute = FactorReport.compute_factor_id(expr, "1m")
    assert minute != daily
    assert minute == hashlib.sha256(f"{expr}::1m".encode("utf-8")).hexdigest()[:16]
    # distinct intraday frequencies -> distinct ids
    assert FactorReport.compute_factor_id(expr, "5m") != minute
    assert FactorReport.compute_factor_id(expr, "15m") != minute


def test_report_json_key_shape_frozen():
    # Keys are static regardless of values; freeze the daily serialized shape.
    report = FactorReport(
        factor_id="x", expr="close", expr_canonical="close",
        ic=0.1, icir=0.2, rank_ic=0.3, rank_icir=0.4,
    )
    keys = sorted(report.to_dict().keys())
    assert hashlib.sha256(json.dumps(keys).encode("utf-8")).hexdigest()[:16] == _REPORT_KEYS_SHA
