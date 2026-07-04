"""Legacy skin — the same SPA at ``/legacy``, dressed up in a 2000s web look.

An Easter egg. The modern app is a hash-router SPA served from ``/static`` with
absolute asset paths, so the *same* ``index.html`` works under any document path and
every ``#/...`` route is prefix-independent. This route reads that real index.html
and injects three things — a retro stylesheet (``/legacy.css``, loaded after
``styles.css`` so it wins), a ``class="legacy"`` hook on ``<body>``, and some
period-correct chrome (a ``<marquee>`` banner + a hit-counter footer) — so the egg
always tracks the live app with zero duplication.

Mounted before the static ``/`` mount in :mod:`assay.api.app` so ``GET /legacy``
resolves here. Reached by URL only; nothing in the app links to it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_STATIC = Path(__file__).resolve().parent.parent / "static"

_BANNER = (
    '<div class="legacy-banner">'
    '<marquee scrollamount="6" behavior="scroll" direction="left">'
    "&#9733;&#9733;&#9733; WELCOME TO ASSAY &middot; Factor Research since 2025 &middot; "
    "Best viewed in 1024&times;768 with Netscape Navigator &middot; "
    '<span class="legacy-blink">NEW!</span> now with A-share support! &middot; '
    "sign our guestbook! &#9733;&#9733;&#9733;"
    "</marquee></div>"
)

_FOOTER = (
    '<div class="legacy-foot">'
    "<hr>"
    'You are visitor <span class="legacy-counter">00013370</span> &nbsp;|&nbsp; '
    '<span class="legacy-blink">&#9679;</span> Made with Notepad &nbsp;|&nbsp; '
    "&copy; 2025 Assay Labs &nbsp;|&nbsp; "
    '<a href="/">&laquo; back to the modern site</a>'
    "<div style=\"margin-top:6px\">[ "
    '<a href="#/dashboard">Home</a> | <a href="#/library">Factors</a> | '
    '<a href="#/factor">Test</a> | <a href="#/combination">Combine</a> | '
    '<a href="#/portfolio">Backtest</a> | '
    '<a href="#/chart">Charts</a> | <a href="#/data">Data</a> | <a href="/admin">Admin</a>'
    ' ] &middot; <span class="uc">This site is under construction</span> &#128679;</div>'
    "</div>"
)


def _render_legacy() -> str:
    """Read the live index.html and inject the legacy skin + retro chrome."""
    idx = (_STATIC / "index.html").read_text(encoding="utf-8")
    # 1) load the retro stylesheet last (robust: inject right before </head>)
    idx = idx.replace("</head>", '  <link rel="stylesheet" href="/legacy.css" />\n</head>', 1)
    # 2) tag the body + drop the marquee banner at the very top
    idx = idx.replace("<body>", '<body class="legacy">\n' + _BANNER, 1)
    # 3) hit-counter footer before </body>
    idx = idx.replace("</body>", _FOOTER + "\n</body>", 1)
    return idx


@router.get("/legacy", response_class=HTMLResponse, include_in_schema=False)
@router.get("/legacy/", response_class=HTMLResponse, include_in_schema=False)
def legacy_index() -> HTMLResponse:
    """Serve the SPA with the 2000s skin (Easter egg)."""
    return HTMLResponse(_render_legacy())
