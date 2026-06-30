"""Offline tests for the WebUI static mount and the data-free ``/v1/factor/lint`` route.

No network, no ingested data, no MASSIVE credentials. The static shell is served by a
``StaticFiles`` mount at ``/`` (added in :func:`assay.api.app.create_app`); ``/lint`` only
touches :mod:`assay.engine` (parse / lint), so neither needs the :class:`AssayService`.

Run with::

    PYTHONPATH=src python -m pytest tests/api/test_webui.py -q
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from assay.api.app import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# static shell
# ---------------------------------------------------------------------------
def test_root_serves_html_shell() -> None:
    """GET / returns the index.html shell — 200, text/html, mentions 'Assay'."""
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers.get("content-type", "")
    assert "Assay" in res.text or "ASSAY" in res.text


def test_static_assets_served() -> None:
    """The CSS and the app entry module are served with sensible content types."""
    css = client.get("/styles.css")
    assert css.status_code == 200
    assert "css" in css.headers.get("content-type", "")

    js = client.get("/js/app.js")
    assert js.status_code == 200
    ctype = js.headers.get("content-type", "")
    assert "javascript" in ctype or "ecmascript" in ctype or "text/" in ctype


def test_v1_routes_still_resolve_under_static_mount() -> None:
    """Mounting StaticFiles at / must not shadow /health or the /v1 routers."""
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_combination_page_assets_and_nav_present() -> None:
    """The Factor Combination page module is served and linked from the top nav."""
    js = client.get("/js/pages/combination.js")
    assert js.status_code == 200
    shell = client.get("/").text
    assert 'data-route="#/combination"' in shell


def test_legacy_skin_serves_updated_spa_with_win98_css() -> None:
    """/legacy serves the same (updated) SPA in the Windows-98 skin."""
    page = client.get("/legacy").text
    assert 'class="legacy"' in page                      # body skin hook
    assert "/legacy.css" in page                         # retro stylesheet injected
    assert 'data-route="#/combination"' in page          # new page reachable (shared SPA)
    assert '#/combination">Combine' in page              # added to the retro footer nav
    css = client.get("/legacy.css")
    assert css.status_code == 200
    assert "#008080" in css.text and "--w98-raised" in css.text  # Win98 teal + 3D bevels


# ---------------------------------------------------------------------------
# /v1/factor/lint — data-free
# ---------------------------------------------------------------------------
def test_lint_valid_expression() -> None:
    """A valid expr lints to 200 with operators containing the op + a canonical string."""
    res = client.post("/v1/factor/lint", json={"expr": "ts_corr(close,volume,20)"})
    assert res.status_code == 200
    body = res.json()
    assert "ts_corr" in body["operators"]
    assert isinstance(body["canonical"], str) and body["canonical"]
    assert body["dialect"] == "func"
    assert set(body["fields"]) == {"close", "volume"}
    assert body["ast"] is not None
    assert body["diagnostics"]["status"] == "ok"


def test_lint_syntax_error_is_200_with_error_diagnostics() -> None:
    """A malformed expr still returns 200; diagnostics.status == 'error', ast is null."""
    res = client.post("/v1/factor/lint", json={"expr": "ts_mean(close,"})
    assert res.status_code == 200
    body = res.json()
    assert body["diagnostics"]["status"] == "error"
    assert body["ast"] is None
    assert body["canonical"] is None
