"""Editable runtime configuration — data dirs + provider credentials.

The analyst/operator edits these from the WebUI (data manager) instead of hand-
writing ``.env``: RAW source dirs (MASSIVE mirror, Tushare mirror), per-market ASSAY
output dirs, the MASSIVE S3 credentials (for the in-repo flat-files downloader) and
the Tushare API token. Persisted as JSON at the repo root (``.assay.config.json``,
gitignored — it holds secrets) and **applied to ``os.environ``** so the rest of the
app (``AssayConfig.from_env``) keeps working unchanged.

Secrets never leave the box in the clear: :func:`masked` renders them as
``••••last4`` for display, and :func:`update` only overwrites a secret when a fresh
non-masked value is supplied (so saving the form back doesn't wipe a key).

House style: ``from __future__ import annotations``, stdlib-only, best-effort I/O.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

__all__ = [
    "config_path", "load", "save", "update", "masked",
    "apply_to_env", "tushare_token", "massive_s3", "raw_dir", "assay_dir",
]

_LOCK = threading.RLock()
_REPO_ROOT = Path(__file__).resolve().parents[2]
# secret leaf paths (section, key) — masked on display, preserved on save
_SECRETS = {("massive_s3", "secret_access_key"), ("massive_s3", "access_key_id"), ("tushare", "token")}


def config_path() -> Path:
    """Where the editable config lives (override with ``ASSAY_CONFIG_FILE``)."""
    return Path(os.environ.get("ASSAY_CONFIG_FILE", str(_REPO_ROOT / ".assay.config.json")))


def _defaults() -> dict[str, Any]:
    """Seed values from the environment / known mirror locations."""
    return {
        "dirs": {
            "raw_massive": os.environ.get("MASSIVE_DATA_DIR", "/data/massive_data"),
            "raw_tushare": os.environ.get("TUSHARE_DATA_DIR", "/data/tushare_data"),
            "assay_us": os.environ.get("ASSAY_DATA_DIR", str(_REPO_ROOT / "data_us")),
            "assay_cn": os.environ.get("ASSAY_DATA_DIR_CN", str(_REPO_ROOT / "data_cn")),
        },
        "massive_s3": {
            "access_key_id": os.environ.get("MASSIVE_ACCESS_KEY_ID", ""),
            "secret_access_key": os.environ.get("MASSIVE_SECRET_ACCESS_KEY", ""),
            "endpoint": os.environ.get("MASSIVE_S3_ENDPOINT", "https://files.massive.com"),
            "bucket": os.environ.get("MASSIVE_S3_BUCKET", "flatfiles"),
        },
        "tushare": {"token": os.environ.get("TUSHARE_TOKEN", "")},
    }


def _merge(base: dict, over: dict) -> dict:
    """Deep-merge ``over`` into a copy of ``base`` (one level of nesting)."""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def load() -> dict[str, Any]:
    """Return the full config (file merged over env/defaults). Secrets in the clear."""
    with _LOCK:
        cfg = _defaults()
        p = config_path()
        if p.exists():
            try:
                cfg = _merge(cfg, json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        return cfg


def save(cfg: dict[str, Any]) -> Path:
    """Write the full config to disk (0600) and apply it to the environment."""
    with _LOCK:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        try:
            os.chmod(p, 0o600)  # secrets — owner-only
        except OSError:
            pass
        apply_to_env(cfg)
        return p


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into the stored config and persist.

    A secret field is overwritten only when the patch carries a non-empty value that
    is not the masked placeholder — so posting the (masked) form back keeps the key.
    Returns the new full config (clear).
    """
    with _LOCK:
        cur = load()
        new = _merge(cur, {})
        for section, vals in (patch or {}).items():
            if not isinstance(vals, dict):
                new[section] = vals
                continue
            dst = new.setdefault(section, {})
            for k, v in vals.items():
                if (section, k) in _SECRETS:
                    sv = str(v or "")
                    if not sv or "•" in sv:  # blank or masked (carries the mask glyph) -> keep
                        continue
                dst[k] = v
        return new if save(new) else new  # save returns Path; keep new


def masked(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Display view: secret leaves become ``••••last4`` (or '' when unset)."""
    cfg = cfg or load()
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}
    for section, key in _SECRETS:
        val = str((out.get(section) or {}).get(key, "") or "")
        if section in out:
            out[section][key] = (("•" * 4 + val[-4:]) if val else "")
    return out


def apply_to_env(cfg: dict[str, Any] | None = None) -> None:
    """Push dirs + credentials into ``os.environ`` so ``AssayConfig.from_env`` sees them."""
    cfg = cfg or load()
    dirs = cfg.get("dirs", {})
    s3 = cfg.get("massive_s3", {})
    tok = (cfg.get("tushare", {}) or {}).get("token", "")
    env = {
        "MASSIVE_DATA_DIR": dirs.get("raw_massive", ""),
        "TUSHARE_DATA_DIR": dirs.get("raw_tushare", ""),
        "ASSAY_DATA_DIR": dirs.get("assay_us", ""),
        "ASSAY_DATA_DIR_CN": dirs.get("assay_cn", ""),
        "TUSHARE_TOKEN": tok or "",
        "MASSIVE_ACCESS_KEY_ID": s3.get("access_key_id", ""),
        "MASSIVE_SECRET_ACCESS_KEY": s3.get("secret_access_key", ""),
        "MASSIVE_S3_ENDPOINT": s3.get("endpoint", ""),
        "MASSIVE_S3_BUCKET": s3.get("bucket", ""),
    }
    for k, v in env.items():
        if v:
            os.environ[k] = str(v)


# ----- typed accessors (used by the downloaders / orchestration) -----
def tushare_token() -> str:
    return str((load().get("tushare", {}) or {}).get("token", "") or "")


def massive_s3() -> dict[str, str]:
    return dict(load().get("massive_s3", {}) or {})


def raw_dir(market: str) -> str:
    d = load().get("dirs", {})
    return d.get("raw_tushare", "") if market.upper() in ("CN", "HK", "A") else d.get("raw_massive", "")


def assay_dir(market: str) -> str:
    d = load().get("dirs", {})
    return d.get("assay_cn", "") if market.upper() in ("CN", "HK", "A") else d.get("assay_us", "")
