"""Score one task's factors on a local QFinVerse panel (what a worker computes).

The panel for a spec (universe, period) is loaded once per process and kept while
tasks for the same spec keep coming; each task then evaluates its expressions in
one CSE batch with :meth:`FactorEngine.evaluate_iter` (a subtree is freed after its
last use) and returns only metrics and a compact novelty fingerprint per factor.
"""

from __future__ import annotations

import base64
import time
import zlib
from dataclasses import dataclass

import numpy as np

from assay import evaluator
from assay.engine import iter_fields, parse
from assay.engine.engine import FactorEngine

__all__ = ["SpecData", "load_spec", "score_items", "encode_print", "decode_print", "fingerprint"]

FIELDS = ["open", "high", "low", "close", "volume"]
FINGERPRINT_DAYS = 60


@dataclass
class SpecData:
    key: str
    engine: FactorEngine
    fwd: dict
    loaded_s: float


def load_spec(store, spec: dict) -> SpecData:
    """Engine (members-masked OHLCV) and unmasked forward returns for one spec."""
    t0 = time.perf_counter()
    end = spec["end"]
    eng = FactorEngine.from_store(store, spec["universe"], (spec["start"], end), end, fields=FIELDS, adj="total")
    _, _, px = store.get_matrices(["close", "open"], list(eng.symbols), spec["start"], end, end, "total")
    fwd = evaluator.forward_returns(np.asarray(px["close"], float), np.asarray(px["open"], float),
                                    list(spec["horizons"]), spec["execution"])
    return SpecData(spec["key"], eng, fwd, time.perf_counter() - t0)


def fingerprint(values: np.ndarray) -> np.ndarray:
    """Cross-sectional ranks in [-1, 1] on ``FINGERPRINT_DAYS`` evenly spaced days (NaN kept)."""
    idx = np.linspace(0, len(values) - 1, min(FINGERPRINT_DAYS, len(values))).astype(int)
    x = values[idx]
    out = np.full(x.shape, np.nan)
    for i, row in enumerate(x):
        ok = np.isfinite(row)
        n = int(ok.sum())
        if n > 2:
            r = np.empty(n)
            r[np.argsort(row[ok], kind="stable")] = np.arange(n)
            out[i, ok] = 2 * r / (n - 1) - 1
    return out


def encode_print(ranks: np.ndarray) -> dict:
    """int8 (NaN = -128), zlib, base64: ~1 byte per value before compression."""
    q = np.where(np.isfinite(ranks), np.round(np.nan_to_num(ranks) * 127), -128).astype(np.int8)
    return {"shape": list(q.shape), "b64": base64.b64encode(zlib.compress(q.tobytes(), 6)).decode()}


def decode_print(p: dict) -> np.ndarray:
    q = np.frombuffer(zlib.decompress(base64.b64decode(p["b64"])), dtype=np.int8).reshape(p["shape"])
    return np.where(q == -128, np.nan, q / 127.0).astype(np.float32)


def score_items(data: SpecData, items: list) -> list[dict]:
    """``[{factor_id, metrics | error, print}]`` for ``items`` = [(factor_id, expr), ...]."""
    from assay.factordb.agent import score

    eng = data.engine
    have = set(eng._field_cols) if hasattr(eng, "_field_cols") else set(FIELDS)
    out, nodes, keep = [], [], []
    for fid, expr in items:
        try:
            node = parse(expr)
            missing = sorted(iter_fields(node) - have)
            if missing:
                raise ValueError(f"fields not available: {missing}")
        except Exception as exc:  # noqa: BLE001 — one bad expression fails alone
            out.append({"factor_id": fid, "error": {"code": "PARSE_ERROR", "message": str(exc)[:300]}})
            continue
        nodes.append(node)
        keep.append(fid)
    for fid, res in zip(keep, eng.evaluate_iter(nodes, errors="yield")):
        if isinstance(res, Exception):
            out.append({"factor_id": fid, "error": {"code": "RUNTIME_ERROR", "operator": getattr(res, "op", None),
                                                     "message": str(res)[:300]}})
            continue
        v = res.values
        out.append({"factor_id": fid, "metrics": score(v, data.fwd), "print": encode_print(fingerprint(v))})
    return out
