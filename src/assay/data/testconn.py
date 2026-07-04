"""Provider connection tests for the Data Manager ("test connect").

Answers "do my saved credentials actually work?" for each source, best-effort and
never raising: a failure is reported as ``{"ok": False, "error": "..."}`` so the UI
renders a red state instead of 500ing.

* :func:`test_massive` — lists the MASSIVE flat-files bucket's top-level datasets
  with the stored S3 key (the definitive entitlement signal).
* :func:`test_tushare` — validates the token with a tiny ``trade_cal`` request.
"""

from __future__ import annotations

from typing import Any

from assay import config_store

# Datasets the US pipeline actually reads (flagged "required" in the result).
_REQUIRED = {"us_stocks_sip": "US stocks (SIP)"}


def test_massive(s3: dict[str, str] | None = None) -> dict[str, Any]:
    """List the flat-files bucket's datasets with the configured S3 key."""
    s3 = s3 or config_store.massive_s3()
    if not (s3.get("access_key_id") and s3.get("secret_access_key")):
        return {"ok": False, "error": "S3 credentials not configured",
                "endpoint": s3.get("endpoint"), "bucket": s3.get("bucket")}
    try:
        from assay.data.massive import s3 as s3mod

        client = s3mod._client(s3)
        resp = client.list_objects_v2(Bucket=s3.get("bucket") or "flatfiles", Delimiter="/")
        prefixes = sorted({p["Prefix"].rstrip("/") for p in resp.get("CommonPrefixes", [])})
    except ModuleNotFoundError as exc:  # boto3 absent
        return {"ok": False, "error": f"{exc} (pip install boto3)",
                "endpoint": s3.get("endpoint"), "bucket": s3.get("bucket")}
    except Exception as exc:  # noqa: BLE001 — network/permission → red state, not a 500
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "endpoint": s3.get("endpoint"), "bucket": s3.get("bucket")}

    datasets = [{"name": n, "required": n in _REQUIRED} for n in prefixes]
    missing = [n for n in _REQUIRED if n not in prefixes]
    return {"ok": True, "endpoint": s3.get("endpoint"), "bucket": s3.get("bucket"),
            "datasets": datasets, "count": len(datasets), "missing_required": missing}


def test_tushare(token: str | None = None) -> dict[str, Any]:
    """Validate the Tushare token with a minimal ``trade_cal`` request (fast-fail)."""
    token = token if token is not None else config_store.tushare_token()
    if not token:
        return {"ok": False, "error": "Tushare token not configured"}
    try:
        from assay.data.tushare.client import TushareClient

        df = TushareClient(token, max_retries=1, timeout=15).call(
            "trade_cal",
            {"exchange": "SSE", "start_date": "20250101", "end_date": "20250110"},
            "cal_date,is_open",
        )
        return {"ok": True, "rows": int(df.height)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def test(provider: str) -> dict[str, Any]:
    """Dispatch a connection test. ``provider`` ∈ ``massive`` | ``tushare``."""
    p = (provider or "").lower()
    if p == "massive":
        return {"provider": "massive", **test_massive()}
    if p == "tushare":
        return {"provider": "tushare", **test_tushare()}
    return {"provider": provider, "ok": False, "error": f"unknown provider {provider!r}"}
