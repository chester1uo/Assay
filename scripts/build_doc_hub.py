"""Render ``docs/{en,cn}`` into the Doc Hub served at ``/doc/``.

Writes ``src/assay/api/static/doc/content/``: one HTML fragment per page and language,
``manifest.json`` (navigation, titles, headings, dates), ``search-<lang>.json`` and the
images under ``docs/assets/`` (figures from ``scripts/doc_figures.py`` are inlined so they
follow the hub's light/dark theme; screenshots are linked).
The page shell (``index.html``, ``doc.js``, ``doc.css``) lives next to it and is
hand-written. Run after editing docs; needs ``markdown-it-py`` (dev only)::

    python scripts/build_doc_hub.py
"""

from __future__ import annotations

import html
import json
import posixpath
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT = ROOT / "src" / "assay" / "api" / "static" / "doc" / "content"
REPO_URL = "https://github.com/QFinVerse/Assay"
LANGS = {"en": "en", "zh": "cn"}          # UI language -> docs folder

# Navigation: (group id, {lang: label}, [(slug, source path under docs/<lang>/)]).
NAV = [
    ("start", {"en": "Get started", "zh": "入门"}, [
        ("project-overview", "project-overview.md"),
        ("guide/getting-started", "guide/getting-started.md"),
        ("guide/webui", "guide/webui.md"),
        ("guide/cli", "guide/cli.md"),
        ("guide/deployment", "guide/deployment.md"),
    ]),
    ("data", {"en": "Data", "zh": "数据"}, [
        ("guide/qfv-data", "guide/qfv-data.md"),
        ("guide/data-and-adjustment", "guide/data-and-adjustment.md"),
        ("guide/data-pipeline", "guide/data-pipeline.md"),
        ("guide/multi-market", "multi_market.md"),
        ("guide/tushare", "tushare.md"),
    ]),
    ("research", {"en": "Research", "zh": "研究"}, [
        ("guide/agent-research", "guide/agent-research.md"),
        ("guide/research-audit", "guide/research-audit.md"),
        ("guide/reference-agent", "guide/reference-agent.md"),
        ("guide/research-workbench", "guide/research-workbench.md"),
        ("guide/factor-combination", "guide/factor-combination.md"),
        ("guide/portfolio-backtest", "guide/portfolio-backtest.md"),
        ("guide/precompute-cse", "guide/precompute-cse.md"),
        ("guide/performance", "guide/performance.md"),
    ]),
    ("api", {"en": "Interfaces", "zh": "接口"}, [
        ("guide/rest-api", "guide/rest-api.md"),
        ("guide/python-sdk", "guide/python-sdk.md"),
        ("guide/mcp-server", "guide/mcp-server.md"),
    ]),
    ("design", {"en": "Design", "zh": "设计"}, [
        ("design/integration", "design/integration.md"),
        ("design/factordb", "design/factordb.md"),
        ("design/architecture", "design/architecture.md"),
        ("design/engineering", "design/engineering.md"),
        ("design/portfolio-backtest", "design/portfolio-backtest.md"),
        ("design/minute-backtesting", "design/minute-backtesting.md"),
        ("design/operator-compatibility", "design/operator-compatibility.md"),
        ("design/webui", "design/webui.md"),
    ]),
    ("reports", {"en": "Reports", "zh": "报告"}, [
        ("reports/alpha101", "reports/alpha101.md"),
    ]),
]
SLUG_OF = {src: slug for _, _, pages in NAV for slug, src in pages}

# The "English · 简体中文" switch line at the top of mirrored pages; the hub has its own.
_LANG_LINE = re.compile(r"^\s*(\*\*English\*\*|\[English\]\([^)]*\))\s*·\s*(\*\*简体中文\*\*|\[简体中文\]\([^)]*\))\s*$")


def _slugify(text: str, seen: dict[str, int]) -> str:
    base = re.sub(r"[^\w一-鿿-]+", "-", text.strip().lower()).strip("-") or "section"
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if n == 0 else f"{base}-{n}"


def _href(href: str, src: str) -> str:
    """Rewrite a relative link in ``docs/<lang>/<src>``: other docs -> /doc/..., files -> GitHub."""
    if not href or href.startswith(("#", "http://", "https://", "mailto:")):
        return href
    path, _, anchor = href.partition("#")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(src), path))
    if target.endswith(".md"):
        # cross-language links point at ../../en/... or ../../cn/...
        for folder in LANGS.values():
            prefix = f"../{folder}/"
            if target.startswith(prefix):
                target = target[len(prefix):]
        slug = SLUG_OF.get(target)
        if slug is None and target in ("README.md", "../README.md"):
            return "/doc/" + (f"#{anchor}" if anchor else "")
        if slug is not None:
            return f"/doc/{slug}" + (f"#{anchor}" if anchor else "")
    repo_path = posixpath.normpath(posixpath.join("docs/x", posixpath.dirname(src), path))
    kind = "tree" if path.endswith("/") else "blob"
    return f"{REPO_URL}/{kind}/main/{repo_path}" + (f"#{anchor}" if anchor else "")


def _asset(path: str, src: str) -> str | None:
    """``docs/assets/...`` path of an image linked from ``docs/<lang>/<src>``, else None."""
    if path.startswith(("http://", "https://", "/")):
        return None
    target = posixpath.normpath(posixpath.join("docs/x", posixpath.dirname(src), path))
    return target if target.startswith("docs/assets/") and (ROOT / target).is_file() else None


_IMG = re.compile(r'<p><img src="([^"]+)" alt="([^"]*)"(?: title="([^"]*)")? ?/?></p>')


def _figures(body: str) -> str:
    """Inline SVG figures; wrap screenshots. Both get an optional caption (the image title)."""
    def swap(m: re.Match) -> str:
        src, alt, caption = m.group(1), m.group(2), m.group(3)
        cap = f"<figcaption>{caption}</figcaption>" if caption else ""
        if src.startswith("inline:"):
            svg = (ROOT / src[len("inline:"):]).read_text(encoding="utf-8").strip()
            return f'<figure class="doc-fig">{svg}{cap}</figure>'
        return f'<figure class="doc-shot"><img src="{src}" alt="{alt}" loading="lazy" />{cap}</figure>'
    return _IMG.sub(swap, body)


def _render(md: MarkdownIt, text: str, src: str) -> tuple[str, str, list[dict], str]:
    lines = text.splitlines()
    lines = [ln for i, ln in enumerate(lines) if not (i < 6 and _LANG_LINE.match(ln))]
    tokens = md.parse("\n".join(lines))
    title, headings, seen = "", [], {}
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open":
            inline = tokens[i + 1]
            label = "".join(c.content for c in inline.children or [] if c.type in ("text", "code_inline"))
            level = int(tok.tag[1])
            if level == 1 and not title:
                title = label
                tok.attrSet("class", "doc-title")
                continue
            hid = _slugify(label, seen)
            tok.attrSet("id", hid)
            if level in (2, 3):
                headings.append({"id": hid, "text": label, "level": level})
        elif tok.type == "inline":
            for child in tok.children or []:
                if child.type == "image":
                    asset = _asset(child.attrGet("src") or "", src)
                    if asset:
                        child.attrSet("src", ("inline:" + asset) if asset.endswith(".svg")
                                      else "/doc/content/" + asset[len("docs/"):])
                if child.type == "link_open":
                    child.attrSet("href", _href(child.attrGet("href") or "", src))
                    if str(child.attrGet("href")).startswith("http"):
                        child.attrSet("target", "_blank")
                        child.attrSet("rel", "noopener noreferrer")
        elif tok.type == "table_open":
            tok.attrSet("class", "doc-table")
    body = md.renderer.render(tokens, md.options, {})
    # tables scroll inside their own box
    body = body.replace('<table class="doc-table">', '<div class="table-wrap"><table class="doc-table">')
    body = body.replace("</table>", "</table></div>")
    plain = re.sub(r"<[^>]+>", " ", body)
    plain = re.sub(r"\s+", " ", html.unescape(plain)).strip()
    return title, _figures(body), headings, plain


def _updated(path: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%cs", "--", str(path)],
                             capture_output=True, text=True, timeout=10)
        dated = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        dated = ""
    # not committed yet: the file's own date
    return dated or date.fromtimestamp(path.stat().st_mtime).isoformat()


def main() -> None:
    md = MarkdownIt("commonmark", {"html": False, "typographer": False}).enable(["table", "strikethrough"])
    if OUT.exists():
        shutil.rmtree(OUT)
    manifest: dict = {"repo": REPO_URL, "groups": [], "pages": {}}
    search: dict[str, list] = {lang: [] for lang in LANGS}
    for gid, labels, pages in NAV:
        manifest["groups"].append({"id": gid, "label": labels, "pages": [slug for slug, _ in pages]})
        for slug, src in pages:
            entry: dict = {"group": gid, "title": {}, "headings": {}, "updated": None}
            for lang, folder in LANGS.items():
                path = DOCS / folder / src
                if not path.is_file():
                    continue
                title, body, headings, plain = _render(md, path.read_text(encoding="utf-8"), src)
                dest = OUT / lang / f"{slug}.html"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(body, encoding="utf-8")
                entry["title"][lang] = title or slug.rsplit("/", 1)[-1]
                entry["headings"][lang] = headings
                entry["updated"] = max(filter(None, [entry["updated"], _updated(path)]), default=None)
                search[lang].append({"slug": slug, "title": entry["title"][lang],
                                     "headings": [h["text"] for h in headings], "text": plain})
            if not entry["title"]:
                raise SystemExit(f"missing doc for {slug}: docs/*/{src}")
            manifest["pages"][slug] = entry
    OUT.mkdir(parents=True, exist_ok=True)
    if (DOCS / "assets").is_dir():
        shutil.copytree(DOCS / "assets", OUT / "assets", ignore=shutil.ignore_patterns("*.svg", ".*"))
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    for lang, rows in search.items():
        (OUT / f"search-{lang}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    n = sum(len(p) for _, _, p in NAV)
    print(f"{n} pages -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
