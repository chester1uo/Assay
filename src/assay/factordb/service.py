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
import hashlib
import re
import threading
from collections import OrderedDict

from assay.factordb.agent import FactorService

__all__ = ["enabled", "get"]

_lock = threading.Lock()
_service: FactorService | None = None
_tenant_lock = threading.RLock()
_tenant_services = OrderedDict()
_OWNER_PREFIX = 'assay-private:'


def owner_for_tenant(tenant: str) -> str:
    """Opaque, controller-generated owner for a verified account namespace."""
    return _OWNER_PREFIX + hashlib.sha256(tenant.encode('utf-8')).hexdigest()


def for_owner(owner: str | None, *, controller=None) -> FactorService:
    """Resolve a queue owner to its own persistent registry after a restart."""
    controller = controller or get()
    if not isinstance(owner, str) or not owner.startswith(_OWNER_PREFIX):
        return controller
    digest = owner[len(_OWNER_PREFIX):]
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise RuntimeError('invalid private compute owner')
    if getattr(controller, 'ades', None) is None:
        raise RuntimeError('Private FactorDB requires the ADES controller to be enabled')
    from assay.api.app import get_service
    from assay.factordb.registry import Registry
    if getattr(controller, '_account_data_root', None) is None:
        controller._account_data_root = get_service().config.data_dir / 'research' / 'accounts'
    root = controller._account_data_root / digest
    if not root.is_dir():
        raise RuntimeError('Private account workspace is unavailable')
    with _tenant_lock:
        key = (id(controller), str(root))
        if key not in _tenant_services:
            registry_dir = root / 'factordb'
            registry_dir.mkdir(exist_ok=True, mode=0o700)
            registry_dir.chmod(0o700)
            _tenant_services[key] = FactorService(controller.db, Registry(registry_dir / 'registry.sqlite3'),
                daily_budget_s=controller.daily_budget_s, ades=controller.ades, stores=controller.stores,
                tenant_owner=owner, start_background=False)
            if len(_tenant_services) > 128:
                _tenant_services.popitem(last=False)
        else:
            _tenant_services.move_to_end(key)
        return _tenant_services[key]


def for_current() -> FactorService:
    """Select the account registry from authenticated request context."""
    from assay.research_tenant import UNAUTHENTICATED, current_tenant
    tenant = current_tenant.get()
    if tenant == UNAUTHENTICATED:
        raise RuntimeError('Authenticated account required')
    if tenant is None:
        return get()
    from assay.api.app import get_service
    get_service()._private_resources()  # create the authenticated account root
    return for_owner(owner_for_tenant(tenant))


def recover_private_dispatches(*, controller=None):
    """Visit private registries so queued work resumes without a user request."""
    from assay.api.app import get_service
    import logging
    controller = controller or get()
    if getattr(controller, '_account_data_root', None) is None:
        controller._account_data_root = get_service().config.data_dir / 'research' / 'accounts'
    accounts = controller._account_data_root
    if not accounts.is_dir():
        return
    for root in accounts.iterdir():
        if not re.fullmatch(r'[0-9a-f]{64}', root.name) or not (root / 'factordb' / 'registry.sqlite3').is_file():
            continue
        try:
            for_owner(_OWNER_PREFIX + root.name, controller=controller).recover_dispatches()
        except Exception:
            logging.getLogger('assay.factordb').exception('Could not recover private ADES dispatch')


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
            from assay.data.markets import MarketStores

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
            controller = FactorService(db, Registry(root / "registry.sqlite3"),
                                     daily_budget_s=float(os.environ.get("ASSAY_FACTORDB_BUDGET_S") or 0),
                                     ades=ades, stores=MarketStores.from_env(us=db.qfv))
            controller.owner_router = lambda owner: for_owner(owner, controller=controller)
            controller.owner_dispatch_recovery = lambda: recover_private_dispatches(controller=controller)
            _service = controller
        return _service
