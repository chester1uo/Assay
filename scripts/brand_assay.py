"""Assay lockup: the QFinVerse mark + ASSAY, as SVG (light and dark).

The mark is QFinVerse's, unchanged (four cyan and four orange 13 × 13 squares turned
45 degrees on one lattice). The wordmark is ASSAY in Helvetica Neue Bold, outlined so
it needs no font, set like the QFinVerse lockup: capitals 3/4 of the mark's height,
centred on it, tracked out slightly for an all-caps word.

    python scripts/brand_assay.py      # writes src/assay/api/static/brand/assay-*.svg
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "assay" / "api" / "static" / "brand"
FONT = "/System/Library/Fonts/HelveticaNeue.ttc"     # face 1: Helvetica Neue Bold
THEMES = {"light": {"q": "#00B8A0", "v": "#FC5200", "ink": "#131722"},
          "dark": {"q": "#00DBB6", "v": "#FC5200", "ink": "#E8EAEF"}}
Q = [(14, 32), (26, 20), (26, 44), (38, 32)]
V = [(50, 20), (62, 32), (74, 20), (50, 44)]
MARK_TOP, MARK_BOTTOM, MARK_RIGHT = 20 - 9.19, 44 + 9.19, 74 + 9.19


def mark(q: str, v: str) -> str:
    sq = lambda x, y, c: (f'<rect x="{x - 6.5:g}" y="{y - 6.5:g}" width="13" height="13" fill="{c}" '
                          f'transform="rotate(45 {x} {y})"/>')
    return "".join(sq(x, y, q) for x, y in Q) + "".join(sq(x, y, v) for x, y in V)


def wordmark(text: str, x0: float, cap: float, mid: float, fill: str, tracking: float) -> tuple[str, float]:
    """Outlined ``text``: capitals ``cap`` tall, centred on ``mid``; ``tracking`` in em."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTCollection

    font = TTCollection(FONT).fonts[1]
    gs, cmap = font.getGlyphSet(), font.getBestCmap()
    upm = font["head"].unitsPerEm
    s = cap / font["OS/2"].sCapHeight
    base = mid + cap / 2
    x, d = x0, []
    for i, ch in enumerate(text):
        g = gs[cmap[ord(ch)]]
        pen = SVGPathPen(gs, ntos=lambda v: f"{v:.2f}".rstrip("0").rstrip("."))
        g.draw(TransformPen(pen, (s, 0, 0, -s, x, base)))
        d.append(pen.getCommands())
        x += g.width * s + (tracking * upm * s if i < len(text) - 1 else 0)
    return f'<path d="{" ".join(d)}" fill="{fill}"/>', x


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cap = 0.75 * (MARK_BOTTOM - MARK_TOP)
    mid = (MARK_TOP + MARK_BOTTOM) / 2
    for theme, c in THEMES.items():
        text, right = wordmark("ASSAY", MARK_RIGHT + 13, cap, mid, c["ink"], tracking=0.06)
        w = round(right + 4, 2)
        (OUT / f"assay-lockup-{theme}.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:g} 64" width="{w:g}" height="64">\n'
            "<!-- Assay lockup: the QFinVerse mark + ASSAY (Helvetica Neue Bold, outlined, tracked +0.06 em). -->\n"
            f"{mark(c['q'], c['v'])}{text}</svg>\n")
    print(f"wrote assay-lockup-{{light,dark}}.svg in {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
