"""FactorDB: factor values computed once on a global grid, shared by every request.

See ``docs/en/design/factordb.md``. :class:`FactorDB` is the entry point.
"""

from assay.factordb.db import Evaluated, FactorDB

__all__ = ["FactorDB", "Evaluated"]
