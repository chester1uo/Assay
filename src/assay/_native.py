"""Optional native kernels (the ``assay_kernels`` Rust extension, ``rust/assay-kernels``).

``kernels`` is the extension module when it is installed and not switched off with
``ASSAY_KERNELS=off``; otherwise ``None`` and callers use their numpy/numba code.
The extension spreads rows over ``RAYON_NUM_THREADS`` threads (default: all cores)
with the GIL released, and reproduces the Python kernels' results.
"""

from __future__ import annotations

import os
import weakref

__all__ = ["kernels", "remember_orders", "known_orders"]

kernels = None
if os.environ.get("ASSAY_KERNELS", "").lower() not in ("off", "0", "python", "numpy"):
    try:  # pragma: no cover - depends on the host build
        import assay_kernels as kernels  # type: ignore[no-redef]
    except ImportError:
        kernels = None

# row orders ``(order, count)`` of arrays that came out of a kernel already sorted
# (``cs_rank``), by identity, dropped with the array: scoring a factor that ends in
# ``cs_rank`` then reads them instead of sorting the factor again
_ORDERS: dict[int, tuple] = {}


def remember_orders(arr, orders) -> None:
    key = id(arr)
    try:
        ref = weakref.ref(arr, lambda _r, k=key: _ORDERS.pop(k, None))
    except TypeError:
        return
    _ORDERS[key] = (ref, orders)


def known_orders(arr):
    """The orders remembered for ``arr`` (this very object), or ``None``."""
    hit = _ORDERS.get(id(arr))
    return hit[1] if hit is not None and hit[0]() is arr else None
