"""Tests for the Assay MCP server — agent-facing tools over AssayService.

Offline only — no network, no MASSIVE credentials, no ingested data. Importing
``assay.mcp.server`` must succeed with the data store unbuilt (architecture §6:
"importing this module needs no MASSIVE credentials"), so every test here either
imports the module or invokes a tool whose service has been monkeypatched. Run::

    PYTHONPATH=src python -m pytest tests/mcp -q

These tests pin three things about the server (architecture §6.2 / §6.3):

1. The module imports cleanly with no credentials.
2. Exactly the documented tool set is registered on the ``FastMCP`` server
   (assay_evaluate, assay_batch, the five library ops, assay_system_status).
3. ``assay_evaluate`` runs end-to-end against a monkeypatched ``AssayService``
   and returns the report dict (carrying ``factor_id``), converting ``period``
   to a tuple on the way through.

IMPLEMENTATION NOTE (a real attribute-shadowing gotcha, not a test bug):
``assay/mcp/__init__.py`` does ``from assay.mcp.server import ... server``, which
binds the FastMCP object to the name ``assay.mcp.server`` as an *attribute* of the
``assay.mcp`` package. That attribute then shadows the *submodule* of the same
name: ``import assay.mcp.server as x`` (and plain attribute access
``assay.mcp.server``) yields the FastMCP **object**, not the module. To reach the
actual module object reliably we use :func:`importlib.import_module`, which
returns the entry from ``sys.modules`` regardless of the shadowing.
"""

from __future__ import annotations

import importlib
import inspect

import pytest


# The tools the server documents (architecture §6.2). Exact set — the test asserts
# both directions (none missing, none extra) so adding/removing a tool without
# updating this list fails loudly.
EXPECTED_TOOLS = {
    "assay_evaluate",
    "assay_batch",
    "assay_lint",
    "assay_universes",
    "assay_portfolio_backtest",
    "assay_library_list",
    "assay_library_get",
    "assay_library_save",
    "assay_library_correlation",
    "assay_library_prune",
    "assay_system_status",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _server_module():
    """Return the real ``assay.mcp.server`` module object.

    Uses :func:`importlib.import_module` rather than ``import ... as`` because the
    package re-export shadows the submodule with the FastMCP ``server`` object
    (see the module docstring). ``import_module`` returns the ``sys.modules``
    entry, which is always the module.
    """
    return importlib.import_module("assay.mcp.server")


def _registered_tools(mod):
    """Return ``{name: Tool}`` for every tool registered on the FastMCP server.

    Robust across the mcp 1.26 FastMCP API: prefer the synchronous
    ``_tool_manager.list_tools()`` (returns ``Tool`` objects with ``.name`` /
    ``.fn``); fall back to the public async ``FastMCP.list_tools()`` if the
    private manager is ever renamed.
    """
    tm = getattr(mod.mcp, "_tool_manager", None)
    if tm is not None and hasattr(tm, "list_tools"):
        return {t.name: t for t in tm.list_tools()}

    # Fallback: drive the public async listing API.
    import asyncio

    tools = asyncio.run(mod.mcp.list_tools())
    return {t.name: t for t in tools}


def _tool_fn(mod, name):
    """Return the underlying Python callable for a registered tool ``name``.

    The ``@mcp.tool`` decorator consumes the module-level function (it is *not*
    left in the module namespace), so we recover the callable from the tool
    manager's ``Tool.fn`` rather than from the module globals.
    """
    tm = getattr(mod.mcp, "_tool_manager", None)
    if tm is not None and hasattr(tm, "get_tool"):
        return tm.get_tool(name).fn
    return _registered_tools(mod)[name].fn


def _make_report(factor_id: str = "deadbeef0123"):
    """Build a minimal, valid :class:`FactorReport` for a fake service to return."""
    from assay.library import FactorReport

    return FactorReport(
        factor_id=factor_id,
        expr="ts_corr(close, volume, 20)",
        expr_canonical="ts_corr(close,volume,20)",
        ic=0.05,
        icir=0.5,
        rank_ic=0.06,
        rank_icir=0.6,
    )


# ---------------------------------------------------------------------------
# import: must be credential-free / offline-safe
# ---------------------------------------------------------------------------
def test_module_imports_cleanly():
    """The server module imports with no network and no MASSIVE credentials."""
    mod = _server_module()
    assert inspect.ismodule(mod)
    # The FastMCP server and its ``server`` alias are both present (architecture
    # §6.4 names the object ``server``; §6.3 builds it as ``mcp``).
    from mcp.server.fastmcp import FastMCP

    assert isinstance(mod.mcp, FastMCP)
    assert mod.server is mod.mcp
    assert mod.mcp.name == "assay"
    # Lazy service access + CLI entry point are exported (architecture §6.5).
    assert callable(mod.get_service)
    assert callable(mod.main)


def test_package_reexports():
    """``assay.mcp`` re-exports the stable surface (mcp/main/get_service).

    It deliberately does NOT bind ``server`` to the FastMCP instance: that would
    shadow the ``assay.mcp.server`` submodule on attribute access. The instance is
    available as ``assay.mcp.mcp``.
    """
    import types

    import assay.mcp as pkg

    mod = _server_module()
    assert pkg.mcp is mod.mcp
    assert pkg.main is mod.main
    assert pkg.get_service is mod.get_service
    # Package __all__ promises exactly these names (no ``server`` alias).
    assert set(pkg.__all__) == {"mcp", "main", "get_service"}
    # ``assay.mcp.server`` stays the submodule, not the FastMCP instance.
    assert isinstance(pkg.server, types.ModuleType) and pkg.server is mod


# ---------------------------------------------------------------------------
# registration: exactly the documented tool set
# ---------------------------------------------------------------------------
def test_tools_registered():
    """Exactly the documented tool set is registered — no more, no fewer."""
    mod = _server_module()
    names = set(_registered_tools(mod))
    assert len(names) == len(EXPECTED_TOOLS), (
        f"expected {len(EXPECTED_TOOLS)} tools, found {len(names)}: {sorted(names)}"
    )
    assert names == EXPECTED_TOOLS, (
        f"missing={sorted(EXPECTED_TOOLS - names)} extra={sorted(names - EXPECTED_TOOLS)}"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_each_tool_is_callable_with_description(name):
    """Every registered tool exposes a Python callable and a non-empty description."""
    mod = _server_module()
    tools = _registered_tools(mod)
    tool = tools[name]
    assert callable(tool.fn), f"{name}.fn is not callable"
    # Tool descriptions steer the agent (architecture §6.6/§6.7) — they must exist.
    assert isinstance(tool.description, str) and tool.description.strip()


def test_evaluate_description_carries_operator_vocabulary():
    """assay_evaluate's description is enriched with the live operator schema (§6.6).

    The server injects ``_operator_docs()`` into the description so the agent sees
    the operator vocabulary inline; assert a couple of stable markers survive.
    """
    mod = _server_module()
    desc = _registered_tools(mod)["assay_evaluate"].description
    assert "Operator vocabulary" in desc
    # Dual-dialect steer (qlib + Python) is part of the agent-actionable copy.
    assert "qlib" in desc and "assay_batch" in desc


# ---------------------------------------------------------------------------
# invocation: assay_evaluate end-to-end over a monkeypatched service
# ---------------------------------------------------------------------------
def test_assay_evaluate_returns_report_dict(monkeypatch):
    """assay_evaluate runs through get_service -> AssayService.get and returns a dict.

    Monkeypatch ``AssayService.get`` (what ``server.get_service`` calls first) to
    hand back a fake service whose ``evaluate`` returns a prebuilt report. The
    tool must return that report's ``to_dict()`` — in particular carrying
    ``factor_id`` — without ever touching price data or the network.
    """
    mod = _server_module()
    from assay.service import AssayService

    report = _make_report("deadbeef0123")
    captured: dict[str, object] = {}

    class FakeService:
        def evaluate(self, expr, **kwargs):
            captured["expr"] = expr
            captured["kwargs"] = kwargs
            return report

    # server.get_service() tries AssayService.get() first and only bootstraps on
    # RuntimeError; returning a fake here short-circuits before any DataStore.
    monkeypatch.setattr(AssayService, "get", staticmethod(lambda: FakeService()))

    fn = _tool_fn(mod, "assay_evaluate")
    out = fn(
        "ts_corr(close, volume, 20)",
        universe="NASDAQ100",
        period=["2020-01-01", "2020-12-31"],
    )

    assert isinstance(out, dict)
    assert out["factor_id"] == "deadbeef0123"
    assert out["expr"] == "ts_corr(close, volume, 20)"

    # The tool forwards the user's expr verbatim and normalizes the period list to
    # a tuple before calling the service (server.assay_evaluate body).
    assert captured["expr"] == "ts_corr(close, volume, 20)"
    assert captured["kwargs"]["universe"] == "NASDAQ100"
    assert captured["kwargs"]["period"] == ("2020-01-01", "2020-12-31")


def test_assay_evaluate_default_period_is_none(monkeypatch):
    """With no period the tool passes ``period=None`` (service resolves the default)."""
    mod = _server_module()
    from assay.service import AssayService

    captured: dict[str, object] = {}

    class FakeService:
        def evaluate(self, expr, **kwargs):
            captured["kwargs"] = kwargs
            return _make_report("cafef00d")

    monkeypatch.setattr(AssayService, "get", staticmethod(lambda: FakeService()))

    fn = _tool_fn(mod, "assay_evaluate")
    out = fn("cs_rank(close)")

    assert out["factor_id"] == "cafef00d"
    # Defaults: period omitted -> None; universe defaults to NASDAQ100.
    assert captured["kwargs"]["period"] is None
    assert captured["kwargs"]["universe"] == "NASDAQ100"


def test_get_service_uses_assay_service_get(monkeypatch):
    """get_service() returns the AssayService.get() singleton when it is initialized."""
    mod = _server_module()
    from assay.service import AssayService

    sentinel = object()
    monkeypatch.setattr(AssayService, "get", staticmethod(lambda: sentinel))
    assert mod.get_service() is sentinel
