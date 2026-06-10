"""Shared pytest configuration for the Assay test suite.

Tests are grouped into module folders (``tests/data``, ``tests/engine``,
``tests/factors``). Each test is auto-tagged with a marker matching its folder,
so a whole module can be targeted with ``-m`` as well as by path::

    pytest tests/engine            # by path
    pytest -m engine               # by marker (equivalent)
"""

from __future__ import annotations

import pytest

_GROUPS = {
    "data",
    "engine",
    "factors",
    "evaluator",
    "library",
    "service",
    "api",
    "mcp",
}


def pytest_collection_modifyitems(items):
    """Tag each collected test with a marker named after its module folder."""
    for item in items:
        group = item.path.parent.name
        if group in _GROUPS:
            item.add_marker(getattr(pytest.mark, group))
