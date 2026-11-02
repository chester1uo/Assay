"""Draw the documentation figures as SVG, in English and Chinese.

Writes ``docs/assets/figures/{en,cn}/<name>.svg``. Colours are CSS variables with the
light palette as fallback and a dark palette under ``prefers-color-scheme`` (GitHub,
plain viewers); the Doc Hub inlines the SVG and sets the variables from its own theme.
Run after changing a figure::

    python scripts/doc_figures.py
"""

from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "assets" / "figures"
W = 760

FONT = "'DM Sans','PingFang SC','Microsoft YaHei','Helvetica Neue',Arial,sans-serif"
MONO = "'JetBrains Mono',SFMono-Regular,Menlo,Consolas,monospace"
LIGHT = {"box": "#FFFFFF", "line": "#CBD0DC", "group": "#F4F6FA", "text": "#1A1D2E", "muted": "#5A6477",
         "arrow": "#8A93A6", "accent": "#00A38B", "accent-bg": "#E3F6F2", "warm": "#E8590C", "warm-bg": "#FFF0E6",
         "gold": "#B7791F", "gold-bg": "#FDF4DF"}
DARK = {"box": "#1F232E", "line": "#3A3F4C", "group": "#171B25", "text": "#D7DAE1", "muted": "#8A8F9C",
        "arrow": "#6E7484", "accent": "#00D6B3", "accent-bg": "#11302C", "warm": "#FF7A3D", "warm-bg": "#3A2418",
        "gold": "#E3B341", "gold-bg": "#352B14"}


def _style() -> str:
    v = lambda k: f"var(--fig-{k},{LIGHT[k]})"
    dark = ";".join(f"--fig-{k}:{c}" for k, c in DARK.items())
    return f"""<style>
.f-box{{fill:{v('box')};stroke:{v('line')};stroke-width:1}}
.f-group{{fill:{v('group')};stroke:{v('line')};stroke-width:1}}
.f-acc{{fill:{v('accent-bg')};stroke:{v('accent')};stroke-width:1.2}}
.f-warm{{fill:{v('warm-bg')};stroke:{v('warm')};stroke-width:1.2}}
.f-gold{{fill:{v('gold-bg')};stroke:{v('gold')};stroke-width:1.2}}
.f-t{{fill:{v('text')};font:600 13px {FONT}}}
.f-h{{fill:{v('muted')};font:600 10.5px {FONT};letter-spacing:.08em;text-transform:uppercase}}
.f-s{{fill:{v('muted')};font:400 11.5px {FONT}}}
.f-m{{fill:{v('text')};font:500 11.5px {MONO}}}
.f-ms{{fill:{v('muted')};font:400 11px {MONO}}}
.f-ta{{fill:{v('accent')};font:600 11px {FONT}}}
.f-tw{{fill:{v('warm')};font:600 11px {FONT}}}
.f-tg{{fill:{v('gold')};font:600 11px {FONT}}}
.f-a{{fill:none;stroke:{v('arrow')};stroke-width:1.4}}
.f-ad{{fill:none;stroke:{v('arrow')};stroke-width:1.4;stroke-dasharray:4 3}}
.f-head{{fill:{v('arrow')}}}
.f-dot-ok{{fill:{v('accent')}}} .f-dot-part{{fill:{v('gold')}}} .f-dot-plan{{fill:none;stroke:{v('muted')};stroke-width:1.2}}
@media (prefers-color-scheme:dark){{svg.fig{{{dark}}}}}
</style>"""


class Fig:
    def __init__(self, name: str, height: int, label: str):
        self.name, self.h, self.label, self.parts = name, height, label, []

    def add(self, s: str) -> None:
        self.parts.append(s)

    def rect(self, x, y, w, h, cls="f-box", r=8):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" class="{cls}"/>')

    def text(self, x, y, s, cls="f-t", anchor="start"):
        self.add(f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{html.escape(s)}</text>')

    def box(self, x, y, w, h, title, lines=(), cls="f-box", center=True, mono_title=False, status=None):
        self.rect(x, y, w, h, cls)
        cx = x + w / 2 if center else x + 12
        anchor = "middle" if center else "start"
        n = 1 + len(lines)
        top = y + h / 2 - (n - 1) * 8 + 4
        self.text(cx, top, title, "f-m" if mono_title else "f-t", anchor)
        for i, ln in enumerate(lines):
            self.text(cx, top + 17 * (i + 1) - 1, ln, "f-s", anchor)
        if status:
            self.dot(x + w - 12, y + 12, status)

    def dot(self, x, y, status):
        cls = {"ok": "f-dot-ok", "part": "f-dot-part", "plan": "f-dot-plan"}[status]
        self.add(f'<circle cx="{x}" cy="{y}" r="4" class="{cls}"/>')

    def arrow(self, pts, dashed=False, head=True):
        d = "M" + " L".join(f"{x},{y}" for x, y in pts)
        self.add(f'<path d="{d}" class="{"f-ad" if dashed else "f-a"}"/>')
        if head:
            (x1, y1), (x2, y2) = pts[-2], pts[-1]
            import math
            a = math.atan2(y2 - y1, x2 - x1)
            p = [(x2, y2), (x2 - 8 * math.cos(a - 0.45), y2 - 8 * math.sin(a - 0.45)),
                 (x2 - 8 * math.cos(a + 0.45), y2 - 8 * math.sin(a + 0.45))]
            self.add('<path d="M{} L{} L{} Z" class="f-head"/>'.format(*(f"{px:.1f},{py:.1f}" for px, py in p)))

    def legend(self, y, items, x=None):
        """items: (status, label) for dots."""
        x = 24 if x is None else x
        for status, label in items:
            self.dot(x + 4, y - 4, status)
            self.text(x + 14, y, label, "f-s")
            x += 30 + 7 * len(label) + (6 * sum(1 for ch in label if ord(ch) > 0x2E80))

    def svg(self) -> str:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" class="fig" viewBox="0 0 {W} {self.h}" '
                f'width="{W}" height="{self.h}" role="img" aria-label="{html.escape(self.label)}">'
                f"{_style()}{''.join(self.parts)}</svg>\n")


# ── figures ─────────────────────────────────────────────────────────────────

def architecture(L) -> Fig:
    f = Fig("architecture", 470, L["arch"])
    f.rect(12, 12, W - 24, 120, "f-group", 10)
    f.text(28, 36, L["surfaces"], "f-h")
    xs = [36, 210, 384, 558]
    for x, (t, s, st) in zip(xs, [("Python SDK", "assay.*", "ok"), ("REST API", "FastAPI · /v1", "ok"),
                                  ("WebUI", L["webui_sub"], "ok"), ("MCP", "FastMCP tools", "ok")]):
        f.box(x, 52, 166, 60, t, [s], status=st)
    f.rect(12, 150, W - 24, 92, "f-group", 10)
    f.text(28, 174, L["service"], "f-h")
    f.box(220, 172, 320, 56, "AssayService", ["evaluate · batch · library · session · portfolio"], "f-acc", status="ok")
    for x in xs:
        f.arrow([(x + 83, 112), (x + 83, 136), (380, 136), (380, 170)], head=False)
    f.arrow([(380, 136), (380, 170)])
    f.rect(12, 260, W - 24, 168, "f-group", 10)
    f.text(28, 284, L["core"], "f-h")
    f.box(250, 296, 260, 56, "FactorEngine", [L["engine_sub"]], status="ok")
    f.arrow([(380, 228), (380, 294)])
    cores = [(36, "DataStore", ["Parquet · QFV daily panel"], "ok"),
             (282, L["cache"], [L["cache_sub"]], "part"),
             (528, "Evaluator", ["IC · RankIC · decay · turnover"], "ok")]
    for x, t, s, st in cores:
        f.box(x, 368, 196, 48, t, s, status=st)
    f.arrow([(320, 352), (134, 352), (134, 366)])
    f.arrow([(380, 352), (380, 366)])
    f.arrow([(440, 352), (626, 352), (626, 366)])
    f.legend(452, [("ok", L["ok"]), ("part", L["part"]), ("plan", L["plan"])])
    return f


def eval_flow(L) -> Fig:
    steps = [("User / Agent", "factor_expr: str", None),
             ("ExprParser", L["ef_parser"], "AST"),
             ("FactorEngine", L["ef_engine"], L["ef_miss"]),
             ("OperatorRegistry", L["ef_ops"], L["ef_matrix"]),
             ("BatchFactorBT", L["ef_bt"], L["ef_pair"]),
             ("ICAnalyzer", L["ef_ic"], None),
             ("FactorReport", L["ef_report"], "JSON")]
    f = Fig("eval-flow", 40 + 66 * len(steps), L["evalflow"])
    for i, (name, note, edge) in enumerate(steps):
        y = 20 + 66 * i
        cls = "f-acc" if i in (0, len(steps) - 1) else "f-box"
        f.rect(150, y, 220, 40, cls)
        f.text(260, y + 25, name, "f-m", "middle")
        f.text(392, y + 25, note, "f-s")
        if i < len(steps) - 1:
            f.arrow([(260, y + 40), (260, y + 64)])
            if steps[i + 1][2]:
                f.text(130, y + 57, steps[i + 1][2], "f-ms", "end")
    return f


def ingestion(L) -> Fig:
    f = Fig("ingestion", 260, L["ingest"])
    f.rect(12, 12, 340, 236, "f-group", 10)
    f.text(28, 36, "MassiveConnector", "f-m")
    rows = [("fetch_ohlcv", L["in_ohlcv"]), ("fetch_adj_events", L["in_adj"]),
            ("fetch_universe", L["in_univ"]), ("fetch_financials", L["in_fin"])]
    for i, (m, out) in enumerate(rows):
        y = 52 + 46 * i
        f.rect(28, y, 308, 38)
        f.text(40, y + 16, m + "()", "f-m")
        f.text(40, y + 31, "→ " + out, "f-s")
    f.arrow([(352, 130), (392, 130)])
    f.rect(394, 12, 220, 236, "f-group", 10)
    f.text(410, 36, "DataIngester", "f-m")
    steps = [("validate_schema", L["in_val"]), ("deduplicate", L["in_dedup"]),
             ("normalize_timezone", L["in_tz"]), ("write_parquet", L["in_write"])]
    for i, (m, s) in enumerate(steps):
        y = 52 + 46 * i
        f.rect(410, y, 188, 38)
        f.text(422, y + 16, m + "()", "f-m")
        f.text(422, y + 31, s, "f-s")
        if i < 3:
            f.arrow([(504, y + 38), (504, y + 46)], head=False)
    f.arrow([(614, 130), (642, 130)])
    # DataStore cylinder
    x, y, w, h = 646, 90, 100, 84
    f.add(f'<path d="M{x},{y+10} v{h-20} a{w/2},10 0 0 0 {w},0 v-{h-20}" class="f-acc"/>')
    f.add(f'<ellipse cx="{x+w/2}" cy="{y+10}" rx="{w/2}" ry="10" class="f-acc"/>')
    f.text(x + w / 2, y + 48, "DataStore", "f-t", "middle")
    f.text(x + w / 2, y + 64, "Parquet", "f-s", "middle")
    return f


def cache_layers(L) -> Fig:
    f = Fig("cache-layers", 330, L["cache_fig"])
    layers = [("L2", L["c_l2"], L["c_l2_key"], L["c_l2_store"], L["c_l2_inv"], "f-acc"),
              ("L1", L["c_l1"], L["c_l1_key"], L["c_l1_store"], L["c_l1_inv"], "f-box"),
              ("Δ", L["c_inc"], L["c_inc_1"], L["c_inc_2"], "", "f-group")]
    for i, (tag, title, a, b, c, cls) in enumerate(layers):
        y = 16 + 100 * i
        f.rect(110, y, 634, 86, cls, 10)
        f.text(130, y + 26, title, "f-t")
        for j, ln in enumerate([a, b, c]):
            if ln:
                f.text(130, y + 46 + 16 * j, ln, "f-s")
        f.add(f'<circle cx="62" cy="{y + 43}" r="22" class="{cls}"/>')
        f.text(62, y + 48, tag, "f-t", "middle")
    f.arrow([(62, 65), (62, 137)])
    f.arrow([(62, 165), (62, 237)])
    f.text(20, 104, L["c_miss"], "f-ms")
    f.text(20, 204, L["c_miss"], "f-ms")
    return f


def portfolio(L) -> Fig:
    steps = L["pf_steps"]
    f = Fig("portfolio-pipeline", 30 + 48 * (len(steps) + 1), L["pf"])
    f.rect(20, 14, 200, 34, "f-acc")
    f.text(120, 36, L["pf_in"], "f-t", "middle")
    for i, (name, desc, st) in enumerate(steps):
        y = 14 + 48 * (i + 1)
        f.arrow([(120, y - 14), (120, y - 2)])
        cls = "f-acc" if i == len(steps) - 1 else "f-box"
        f.rect(20, y, 200, 34, cls)
        f.text(120, y + 22, name, "f-m", "middle")
        f.text(240, y + 22, desc, "f-s")
        f.dot(728, y + 17, st)
    f.legend(30 + 48 * (len(steps) + 1) - 8, [("ok", L["ok"]), ("part", L["part"])], x=240)
    return f


def agent_loop(L) -> Fig:
    f = Fig("agent-loop", 232, L["loop"])
    steps = L["loop_steps"]
    xs = [16, 166, 316, 466, 616]
    for i, (tool, purpose) in enumerate(steps):
        x = xs[i]
        f.rect(x, 40, 128, 92, "f-acc" if i == 3 else "f-box")
        f.text(x + 12, 64, f"{i + 1}", "f-ta")
        f.text(x + 12, 84, tool, "f-m")
        lines = purpose
        for j, ln in enumerate(lines):
            f.text(x + 12, 104 + 15 * j, ln, "f-s")
        if i < 4:
            f.arrow([(x + 128, 86), (x + 148, 86)])
    f.arrow([(680, 132), (680, 196), (80, 196), (80, 134)], dashed=True)
    f.text(380, 218, L["loop_back"], "f-s", "middle")
    f.text(16, 24, L["loop_title"], "f-h")
    return f


def topology(L) -> Fig:
    f = Fig("topology", 380, L["topo"])
    f.box(16, 30, 150, 50, L["t_browser"], ["WebUI · Doc Hub"])
    f.box(16, 100, 150, 50, L["t_scripts"], ["REST · X-API-Key"])
    f.box(16, 246, 150, 50, L["t_agent"], [L["t_agent_sub"]])
    f.box(206, 64, 150, 52, L["t_proxy"], ["TLS · HTTPS"])
    f.arrow([(166, 55), (186, 55), (186, 82), (204, 82)])
    f.arrow([(166, 125), (186, 125), (186, 98), (204, 98)])
    f.rect(396, 16, 348, 204, "f-group", 10)
    f.text(412, 40, L["t_proc"], "f-h")
    f.box(412, 54, 316, 50, "uvicorn · FastAPI", [L["t_api"]], "f-acc")
    f.arrow([(356, 90), (410, 90)])
    f.box(412, 120, 150, 84, "AssayService", [L["t_svc1"], L["t_svc2"]])
    f.box(578, 120, 150, 84, L["t_acc"], [L["t_acc1"], L["t_acc2"]])
    f.arrow([(487, 104), (487, 118)])
    f.arrow([(653, 104), (653, 118)])
    f.box(170 + 226, 250, 168, 56, L["t_panel"], [L["t_panel_sub"]], "f-gold")
    f.box(580, 250, 164, 56, L["t_home"], [L["t_home_sub"]])
    f.arrow([(470, 204), (470, 248)])
    f.arrow([(540, 204), (640, 248)])
    f.box(396, 326, 168, 44, L["t_build"], [L["t_build_sub"]])
    f.arrow([(480, 326), (480, 308)], dashed=True)
    f.box(206, 246, 150, 50, L["t_mcp"], [L["t_mcp_sub"]])
    f.arrow([(166, 271), (204, 271)])
    f.arrow([(281, 246), (281, 172), (410, 172)], dashed=True)
    return f


def qfv_flow(L) -> Fig:
    f = Fig("qfv-data-flow", 300, L["qfv"])
    f.rect(12, 12, 180, 276, "f-group", 10)
    f.text(28, 36, "MASSIVE", "f-h")
    for i, (t, s) in enumerate(L["q_src"]):
        f.box(24, 50 + 76 * i, 156, 62, t, [s])
    f.arrow([(192, 150), (212, 150)])
    f.box(214, 104, 156, 92, L["q_build"], [L["q_build1"], L["q_build2"], L["q_build3"]], "f-acc")
    f.arrow([(370, 150), (392, 150)])
    f.rect(394, 12, 190, 276, "f-gold", 10)
    f.text(410, 36, L["q_store"], "f-tg")
    for i, (t, s) in enumerate(L["q_store_rows"]):
        f.text(410, 66 + 46 * i, t, "f-m")
        f.text(410, 82 + 46 * i, s, "f-s")
    f.arrow([(584, 150), (604, 150)])
    f.box(606, 96, 142, 108, "QFVPanelStore", [L["q_read1"], L["q_read2"], L["q_read3"], L["q_read4"]], "f-acc")
    return f


EN = {
    "arch": "Assay layers: surfaces, service, core engine",
    "surfaces": "Surfaces", "service": "Service layer", "core": "Core engine",
    "webui_sub": "console · Doc Hub", "engine_sub": "parser · DAG/CSE planned",
    "cache": "Cache", "cache_sub": "session + L2 · L1 arena planned",
    "ok": "implemented", "part": "partial", "plan": "planned",
    "evalflow": "Path of one factor evaluation",
    "ef_parser": "qlib or Python syntax → one AST", "ef_engine": "L2 factor cache, then L1 operator cache",
    "ef_miss": "cache miss", "ef_ops": "uncached nodes via Polars / Numba", "ef_matrix": "T × N matrix",
    "ef_bt": "aligns the factor with forward returns", "ef_pair": "(factor, returns)",
    "ef_ic": "IC, RankIC, ICIR, decay (Numba, parallel)", "ef_report": "structured result for the caller",
    "ingest": "Ingestion: vendor connector to the Parquet store",
    "in_ohlcv": "raw OHLCV frame", "in_adj": "corporate action log", "in_univ": "constituent snapshot",
    "in_fin": "point-in-time financials", "in_val": "reject bad rows to quarantine", "in_dedup": "drop exact duplicates",
    "in_tz": "NYSE trading calendar", "in_write": "append to a partition",
    "cache_fig": "Cache layers, checked top to bottom",
    "c_l2": "L2 factor cache · disk, Parquet (~1 MB per factor)",
    "c_l2_key": "key: hash(expression, universe, dates, adjustment version)",
    "c_l2_store": "stores the full T × N factor matrix", "c_l2_inv": "dropped when the adjustment version changes",
    "c_l1": "L1 operator cache · RAM arena blocks, float32",
    "c_l1_key": "key: (operator, field, window, market, universe, dates)",
    "c_l1_store": "stores intermediate results such as ts_mean(close, 20)", "c_l1_inv": "eviction: LFU with EMA heat score",
    "c_inc": "Incremental maintenance", "c_inc_1": "daily append: O(1) update for linear sliding-window operators",
    "c_inc_2": "topological scheduler updates dependents in order", "c_miss": "miss",
    "pf": "Portfolio backtest pipeline", "pf_in": "factor expression",
    "pf_steps": [("UniverseFilter", "finite factor and price, tradable mask", "ok"),
                 ("SignalProcessor", "winsorize · neutralize · rank / z-score / raw", "ok"),
                 ("RebalanceScheduler", "calendar · threshold · signal autocorrelation", "ok"),
                 ("WeightConstructor", "equal · signal · quantile · mean-variance · risk parity", "ok"),
                 ("ConstraintApplicator", "position / count / turnover caps · gross-net · sector", "part"),
                 ("ExecutionSimulator", "slippage · ADV cap · price limits · T+1", "part"),
                 ("PortfolioAccountant", "weight drift · daily NAV · costs · trade log", "ok"),
                 ("PortfolioReport", "Sharpe · drawdown · turnover · cost drag · benchmark", "ok")],
    "loop": "A typical agent loop over the MCP tools", "loop_title": "Agent loop (MCP tools)",
    "loop_steps": [("library_list", ["what good factors", "exist already?"]),
                   ("batch", ["score many", "candidates, sorted"]),
                   ("correlation", ["redundant with", "the library?"]),
                   ("library_save", ["keep the unique,", "high-ICIR factor"]),
                   ("library_prune", ["propose removing", "dominated factors"])],
    "loop_back": "next round: new candidates from what was learned",
    "topo": "How a deployment fits together",
    "t_browser": "Browser", "t_scripts": "Scripts, SDK", "t_agent": "Agent host", "t_agent_sub": "Claude, Codex …",
    "t_proxy": "Reverse proxy", "t_proc": "One Assay process", "t_api": "REST /v1 · /auth · WebUI · /doc",
    "t_svc1": "engine + caches", "t_svc2": "library, sessions", "t_acc": "Accounts", "t_acc1": "users, keys,", "t_acc2": "sessions (SQLite)",
    "t_panel": "QFV daily panel", "t_panel_sub": "read-only, versioned", "t_home": "Assay home", "t_home_sub": "library · L2 cache",
    "t_build": "Nightly panel build", "t_build_sub": "QFinVerse, after the close", "t_mcp": "MCP server", "t_mcp_sub": "stdio · same engine",
    "qfv": "US data: from MASSIVE to an Assay evaluation",
    "q_src": [("Day bars", "us_stocks_sip, from 2003"), ("Corporate actions", "splits · dividends"),
              ("Ticker reference", "type · FIGI · CIK · renames")],
    "q_build": "qfz-data daily", "q_build1": "security ids · renames", "q_build2": "checks every split", "q_build3": "PIT universes",
    "q_store": "Daily panel store",
    "q_store_rows": [("CURRENT", "points to the live version"), ("v00000N/", "immutable versions"),
                     ("blocks/YYYY", "float32 dates × sids"), ("events", "checked splits, dividends"),
                     ("universes", "TOP500 / 1000 / 3000")],
    "q_read1": "memory-mapped reads", "q_read2": "adjusts on read", "q_read3": "member mask per day", "q_read4": "picks up new versions",
}

CN = {
    "arch": "Assay 分层：使用入口、服务层、核心引擎",
    "surfaces": "使用入口", "service": "服务层", "core": "核心引擎",
    "webui_sub": "控制台 · Doc Hub", "engine_sub": "解析器 · DAG/CSE 规划中",
    "cache": "缓存", "cache_sub": "会话 + L2 · L1 arena 规划中",
    "ok": "已实现", "part": "部分实现", "plan": "规划中",
    "evalflow": "一次因子评估的路径",
    "ef_parser": "qlib 或 Python 语法 → 统一 AST", "ef_engine": "先查 L2 因子缓存，再查 L1 算子缓存",
    "ef_miss": "未命中", "ef_ops": "未缓存的节点用 Polars / Numba 计算", "ef_matrix": "T × N 矩阵",
    "ef_bt": "因子与未来收益对齐", "ef_pair": "(因子, 收益)",
    "ef_ic": "IC、RankIC、ICIR、衰减（Numba 并行）", "ef_report": "返回给调用方的结构化结果",
    "ingest": "数据导入：从数据商接口到 Parquet 存储",
    "in_ohlcv": "原始 OHLCV", "in_adj": "公司行动记录", "in_univ": "成分股快照",
    "in_fin": "时点财务数据", "in_val": "坏行隔离", "in_dedup": "去掉完全重复的行",
    "in_tz": "按 NYSE 交易日历", "in_write": "追加到分区",
    "cache_fig": "缓存分层，自上而下查找",
    "c_l2": "L2 因子缓存 · 磁盘 Parquet（每个因子约 1 MB）",
    "c_l2_key": "键：hash(表达式, 股票池, 区间, 复权版本)",
    "c_l2_store": "存完整的 T × N 因子矩阵", "c_l2_inv": "复权版本变化时失效",
    "c_l1": "L1 算子缓存 · 内存 arena 块，float32",
    "c_l1_key": "键：(算子, 字段, 窗口, 市场, 股票池, 区间)",
    "c_l1_store": "存中间结果，如 ts_mean(close, 20)", "c_l1_inv": "淘汰：LFU + EMA 热度",
    "c_inc": "增量维护", "c_inc_1": "每日追加：线性滑窗算子 O(1) 更新",
    "c_inc_2": "按拓扑顺序更新依赖项", "c_miss": "未命中",
    "pf": "组合回测流水线", "pf_in": "因子表达式",
    "pf_steps": [("UniverseFilter", "因子和价格有效、可交易", "ok"),
                 ("SignalProcessor", "去极值 · 中性化 · 排序 / z-score / 原值", "ok"),
                 ("RebalanceScheduler", "按日历 · 阈值 · 信号自相关调仓", "ok"),
                 ("WeightConstructor", "等权 · 信号加权 · 分位 · 均值方差 · 风险平价", "ok"),
                 ("ConstraintApplicator", "持仓 / 数量 / 换手上限 · 总净敞口 · 行业", "part"),
                 ("ExecutionSimulator", "滑点 · ADV 上限 · 涨跌停 · T+1", "part"),
                 ("PortfolioAccountant", "权重漂移 · 日度净值 · 成本 · 成交记录", "ok"),
                 ("PortfolioReport", "夏普 · 回撤 · 换手 · 成本拖累 · 基准", "ok")],
    "loop": "agent 通过 MCP 工具的典型循环", "loop_title": "Agent 循环（MCP 工具）",
    "loop_steps": [("library_list", ["库里已经有", "哪些好因子？"]),
                   ("batch", ["批量评估候选，", "按结果排序"]),
                   ("correlation", ["和库里的因子", "是否冗余？"]),
                   ("library_save", ["保存独特、", "ICIR 高的因子"]),
                   ("library_prune", ["建议删除", "被支配的因子"])],
    "loop_back": "下一轮：根据结果生成新的候选",
    "topo": "一次部署的组成",
    "t_browser": "浏览器", "t_scripts": "脚本、SDK", "t_agent": "Agent 宿主", "t_agent_sub": "Claude、Codex …",
    "t_proxy": "反向代理", "t_proc": "一个 Assay 进程", "t_api": "REST /v1 · /auth · WebUI · /doc",
    "t_svc1": "引擎 + 缓存", "t_svc2": "因子库、会话", "t_acc": "账户", "t_acc1": "用户、密钥、", "t_acc2": "登录（SQLite）",
    "t_panel": "QFV 日频面板", "t_panel_sub": "只读、带版本", "t_home": "Assay 数据目录", "t_home_sub": "因子库 · L2 缓存",
    "t_build": "每晚构建面板", "t_build_sub": "QFinVerse，收盘后", "t_mcp": "MCP 服务", "t_mcp_sub": "stdio · 同一引擎",
    "qfv": "美股数据：从 MASSIVE 到 Assay 的一次评估",
    "q_src": [("日线", "us_stocks_sip，2003 年起"), ("公司行动", "拆股 · 分红"),
              ("Ticker 参考数据", "类型 · FIGI · CIK · 改名")],
    "q_build": "qfz-data daily", "q_build1": "证券 ID · 改名接续", "q_build2": "逐个核对拆股", "q_build3": "时点股票池",
    "q_store": "日频面板存储",
    "q_store_rows": [("CURRENT", "指向当前版本"), ("v00000N/", "不可变版本"),
                     ("blocks/YYYY", "float32 日期 × 证券"), ("events", "核对过的拆股、分红"),
                     ("universes", "TOP500 / 1000 / 3000")],
    "q_read1": "内存映射读取", "q_read2": "读取时复权", "q_read3": "按日成分掩码", "q_read4": "自动切换新版本",
}

FIGURES = [architecture, eval_flow, ingestion, cache_layers, portfolio, agent_loop, topology, qfv_flow]


def main() -> None:
    for folder, labels in (("en", EN), ("cn", CN)):
        out = OUT / folder
        out.mkdir(parents=True, exist_ok=True)
        for make in FIGURES:
            fig = make(labels)
            (out / f"{fig.name}.svg").write_text(fig.svg(), encoding="utf-8")
    print(f"{len(FIGURES)} figures x 2 languages -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
