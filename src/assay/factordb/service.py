"""The process-wide :class:`~assay.factordb.agent.FactorService`, configured from the env.

FactorDB is on when ``ASSAY_FACTORDB_DIR`` and ``ASSAY_QFV_PANEL_DIR`` are both set:

=============================  =============================================================
``ASSAY_FACTORDB_DIR``         node store + registry (a local disk with room: ~150 MB per
                               stored node on a TOP3000 grid from 2010)
``ASSAY_FACTORDB_START``       first day of the grid (default 2010-01-01)
``ASSAY_FACTORDB_WORKERS``     compute processes (default: cores - 1)
``ASSAY_FACTORDB_DOMAIN``      universes whose members make up the grid (default
                               ``TOP3000,SP500,NASDAQ100``)
``ASSAY_FACTORDB_BUDGET_S``    optional cap on compute seconds per owner per day (default 0:
                               no cap; owners are interleaved fairly either way)
``ASSAY_ADES``                 ``1``: this process is the ADES controller; slices go to the
                               task queue (``<dir>/ades.sqlite3``) for remote workers
``ASSAY_ADES_LEASE_S``         seconds a worker holds a task between heartbeats (default 120)
``ASSAY_WORKER_KEYS``          comma-separated keys workers present on ``/v1/ades/*``
=============================  =============================================================
"""

from __future__ import annotations

import os
import threading

from assay.factordb.agent import FactorService

__all__ = ["enabled", "get"]

_lock = threading.Lock()
_service: FactorService | None = None


def enabled() -> bool:
    return bool(os.environ.get("ASSAY_FACTORDB_DIR") and os.environ.get("ASSAY_QFV_PANEL_DIR"))


def get() -> FactorService:
    """The shared service (built on first use). Raises ``RuntimeError`` when FactorDB is off."""
    global _service
    if not enabled():
        raise RuntimeError("FactorDB is not configured (set ASSAY_FACTORDB_DIR and ASSAY_QFV_PANEL_DIR)")
    with _lock:
        if _service is None:
            from pathlib import Path

            from assay.factordb.db import FactorDB
            from assay.factordb.registry import Registry

            root = Path(os.environ["ASSAY_FACTORDB_DIR"]).expanduser()
            workers = int(os.environ.get("ASSAY_FACTORDB_WORKERS") or 0) or None
            domain = tuple(u.strip() for u in os.environ.get("ASSAY_FACTORDB_DOMAIN", "").split(",") if u.strip())
            db = FactorDB(root, os.environ["ASSAY_QFV_PANEL_DIR"],
                          start=os.environ.get("ASSAY_FACTORDB_START", "2010-01-01"), workers=workers,
                          **({"domain": domain} if domain else {}))
            ades = None
            if os.environ.get("ASSAY_ADES", "") in ("1", "true", "yes"):
                from assay.ades.tasks import TaskQueue

                ades = TaskQueue(root / "ades.sqlite3", lease_s=float(os.environ.get("ASSAY_ADES_LEASE_S", "120")))
            _service = FactorService(db, Registry(root / "registry.sqlite3"),
                                     daily_budget_s=float(os.environ.get("ASSAY_FACTORDB_BUDGET_S") or 0),
                                     ades=ades)
        return _service
