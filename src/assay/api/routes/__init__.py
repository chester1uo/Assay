"""REST route modules grouped by surface (architecture §4.2-§4.4).

Each module exposes a ``router`` (a :class:`fastapi.APIRouter`) mounted under its
``/v1`` prefix by :func:`assay.api.app.create_app`: ``factor`` (evaluate / batch),
``library`` (CRUD + correlation + prune), ``session`` (create / expire) and
``system`` (status / universes / data-calendar).
"""

from __future__ import annotations
