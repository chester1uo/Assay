"""Optional native kernels (the ``assay_kernels`` Rust extension, ``rust/assay-kernels``).

``kernels`` is the extension module when it is installed and not switched off with
``ASSAY_KERNELS=off``; otherwise ``None`` and callers use their numpy/numba code.
The extension spreads rows over ``RAYON_NUM_THREADS`` threads (default: all cores)
with the GIL released, and reproduces the Python kernels' results.
"""

from __future__ import annotations

import os

__all__ = ["kernels"]

kernels = None
if os.environ.get("ASSAY_KERNELS", "").lower() not in ("off", "0", "python", "numpy"):
    try:  # pragma: no cover - depends on the host build
        import assay_kernels as kernels  # type: ignore[no-redef]
    except ImportError:
        kernels = None
