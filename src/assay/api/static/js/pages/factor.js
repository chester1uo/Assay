// pages/factor.js — Single Factor Test (assay_webui_design.md §5).
//
// The centerpiece screen. A lightweight expression editor (mono <textarea>),
// live data-free lint (POST /v1/factor/lint, debounced), SSE-streamed evaluation
// (api.evaluateStream) rendering chart cards progressively, and a structured
// FactorReport summary panel with save-to-library + diagnostics/error states.
//
// Contract: export render(root, ctx); ctx = {api, store, router, charts, el, ...dom}.
// For '#/factor/:id' the matched id is at ctx.params.id (prefill from library).
//
// Zero deps. NO Monaco, NO CDN. Charts come from ctx.charts (hand-rolled SVG).

const STYLE_ID = "factor-page-style";
const HISTORY_KEY = "assay_expr_history";
const HISTORY_MAX = 20;
const LINT_DEBOUNCE_MS = 300;

const DEFAULT_EXPR = "ts_corr(close, volume, 20)";
const HORIZONS_ALL = [1, 5, 10, 20];
const EXECUTIONS = ["next_open", "next_close"]; // vwap unsupported (no intraday source)
const NEUTRALIZE = [
  { value: "", label: "None" },
  { value: "sector", label: "Sector" },
  { value: "industry", label: "Industry" },
  { value: "market_cap", label: "Market cap" },
];

// Curated operator reference (no operator-schema endpoint exists; we list the
// registered names we know and note that more are available via /v1/factor/lint).
const OPERATOR_DOCS = [
  { group: "Time-series", ops: [
    ["ts_delay(x, d)", "value of x, d days ago"],
    ["ts_delta(x, d)", "x − ts_delay(x, d)"],
    ["ts_mean(x, d)", "rolling mean over d days"],
    ["ts_std(x, d)", "rolling std (d ≥ 2)"],
    ["ts_sum(x, d)", "rolling sum"],
    ["ts_min(x, d) / ts_max(x, d)", "rolling extrema"],
    ["ts_rank(x, d)", "rolling time-series rank"],
    ["ts_corr(x, y, d)", "rolling correlation"],
    ["ts_cov(x, y, d)", "rolling covariance"],
    ["ts_ema(x, d) / ts_dema(x, d)", "exponential / double-EMA"],
    ["ts_decay_linear(x, d)", "linearly-weighted mean"],
    ["ts_argmax(x, d) / ts_argmin(x, d)", "index of extrema"],
    ["ts_returns(x, d) / ts_log_returns(x, d)", "returns"],
    ["ts_zscore(x, d) / ts_skew / ts_kurt / ts_product", "moments"],
  ] },
  { group: "Cross-sectional", ops: [
    ["cs_rank(x)", "rank across symbols per day"],
    ["cs_zscore(x) / cs_demean(x) / cs_scale(x)", "standardise"],
    ["cs_winsorize(x, p)", "clip tails (0 < p < 0.5)"],
    ["cs_neutralize(x, g)", "residualise vs group g"],
    ["cs_group_rank(x, g) / cs_group_mean(x, g)", "by group"],
  ] },
  { group: "Element-wise / math", ops: [
    ["+  −  *  /  (or add/sub/mul/div)", "arithmetic"],
    ["abs, neg, sign, log, sqrt, sigmoid", "unary"],
    ["pow(x, n), signed_power(x, n)", "powers"],
    ["clip(x, lo, hi), fillna(x, m)", "cleanup"],
    ["elem_max(x, y) / elem_min(x, y)", "pairwise"],
    ["where(cond, a, b), safe_div(x, y)", "conditional"],
  ] },
];

// ----------------------------------------------------------------- syntax bridge ----
// §9.2 — pure in-process qlib <-> Assay-Python rewrite. No API call.

export function toAssayPython(expr) {
  return String(expr)
    .replace(/\$(\w+)/g, "$1")
    .replace(/\bRef\(([^,]+),\s*(\d+)\)/g, "ts_delay($1, $2)")
    .replace(/\bMean\(([^,]+),\s*(\d+)\)/g, "ts_mean($1, $2)")
    .replace(/\bStd\(([^,]+),\s*(\d+)\)/g, "ts_std($1, $2)")
    .replace(/\bCorr\(([^,]+),\s*([^,]+),\s*(\d+)\)/g, "ts_corr($1, $2, $3)")
    .replace(/\bEMA\(([^,]+),\s*(\d+)\)/g, "ts_ema($1, $2)")
    .replace(/\bRank\(([^)]+)\)/g, "cs_rank($1)")
    .replace(/\bDelta\(([^,]+),\s*(\d+)\)/g, "ts_delta($1, $2)")
    .replace(/\bSum\(([^,]+),\s*(\d+)\)/g, "ts_sum($1, $2)")
    .replace(/\bIdxMax\(([^,]+),\s*(\d+)\)/g, "ts_argmax($1, $2)")
    .replace(/\bIdxMin\(([^,]+),\s*(\d+)\)/g, "ts_argmin($1, $2)");
}

export function toQlib(expr) {
  return String(expr)
    .replace(/\bts_delay\(([^,]+),\s*(\d+)\)/g, "Ref($$$1, $2)")
    .replace(/\bts_mean\(([^,]+),\s*(\d+)\)/g, "Mean($$$1, $2)")
    .replace(/\bts_std\(([^,]+),\s*(\d+)\)/g, "Std($$$1, $2)")
    .replace(/\bts_corr\(([^,]+),\s*([^,]+),\s*(\d+)\)/g, "Corr($$$1, $$$2, $3)")
    .replace(/\bts_ema\(([^,]+),\s*(\d+)\)/g, "EMA($$$1, $2)")
    .replace(/\bcs_rank\(([^)]+)\)/g, "Rank($1)")
    .replace(/\bts_delta\(([^,]+),\s*(\d+)\)/g, "Delta($$$1, $2)")
    .replace(/\bts_sum\(([^,]+),\s*(\d+)\)/g, "Sum($$$1, $2)")
    // Prefix bare price fields with $, but not ones already prefixed (avoids $$close).
    .replace(/(?<!\$)\b(close|volume|open|high|low|transactions|vwap)\b/g, "$$$1");
}

/** Detect dialect locally (fallback when lint hasn't responded yet).
 * The backend reports "qlib" (CamelCase + $fields) or "func" (Assay-Python). */
function localDialect(expr) {
  if (/\$\w+/.test(expr) || /\b(Ref|Mean|Std|Corr|EMA|Rank|Delta|Sum|IdxMax|IdxMin)\(/.test(expr)) {
    return "qlib";
  }
  return "func";
}

// ----------------------------------------------------------------- history ----

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.filter((s) => typeof s === "string") : [];
  } catch (_) {
    return [];
  }
}

function pushHistory(expr) {
  const e = (expr || "").trim();
  if (!e) return loadHistory();
  let hist = loadHistory().filter((h) => h !== e);
  hist.unshift(e);
  hist = hist.slice(0, HISTORY_MAX);
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(hist));
  } catch (_) {
    /* storage unavailable */
  }
  return hist;
}

// ----------------------------------------------------------------- styles ----

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const css = `
.factor-page { display: flex; flex-direction: column; gap: var(--sp-4); }
.factor-editor-shell { display: flex; flex-direction: column; gap: var(--sp-2); }
.factor-toolbar { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.factor-toolbar .grow { min-width: 0; }
.factor-dialect { display: inline-flex; align-items: center; gap: var(--sp-1); }
.factor-editor {
  width: 100%; min-height: 120px; resize: vertical;
  font-family: var(--font-mono); font-size: 14px; line-height: 1.55;
  padding: var(--sp-3); border: 1px solid var(--border); border-radius: var(--radius-card);
  background: var(--gray-1); color: var(--text); tab-size: 2;
}
.factor-editor:focus-visible { outline: none; box-shadow: var(--focus-ring); border-color: var(--blue); }
.factor-editor.is-error { border-color: #E8B5AE; }
.factor-lintbar { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; font-size: 12px; min-height: 20px; }
.factor-diag { display: flex; gap: var(--sp-2); align-items: flex-start; padding: var(--sp-1) 0; }
.factor-diag-msg { font-size: 12px; }
.factor-diag-snippet { font-family: var(--font-mono); font-size: 12px; white-space: pre; color: var(--text-muted); margin: var(--sp-1) 0 0; }
.factor-ast { margin: 0; font-family: var(--font-mono); font-size: 12px; line-height: 1.5; white-space: pre; overflow-x: auto; }
.ast-op { color: var(--blue); }
.ast-field { color: var(--teal); }
.ast-lit { color: var(--amber); }
.factor-config { display: flex; align-items: flex-end; gap: var(--sp-3); flex-wrap: wrap; }
.factor-config-field { display: flex; flex-direction: column; gap: var(--sp-1); }
.factor-config-field > .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; }
.factor-horizons { display: inline-flex; gap: var(--sp-3); align-items: center; }
.factor-horizons label { display: inline-flex; gap: 4px; align-items: center; font-size: 13px; cursor: pointer; }
.factor-override { color: var(--amber); font-weight: 600; cursor: help; }
.factor-main { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: var(--sp-4); align-items: start; }
@media (max-width: 1200px) { .factor-main { grid-template-columns: minmax(0, 1fr); } }
.factor-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-4); }
@media (max-width: 900px) { .factor-grid { grid-template-columns: minmax(0, 1fr); } }
.factor-card .card-head { flex-direction: column; align-items: flex-start; gap: 2px; }
.factor-hint { font-size: 12px; color: var(--text-muted); }
.factor-summary { position: sticky; top: var(--sp-4); }
.factor-summary-section { padding: var(--sp-3) 0; border-top: 1px solid var(--border); }
.factor-summary-section:first-child { border-top: none; padding-top: 0; }
.factor-kv { display: grid; grid-template-columns: auto 1fr; gap: 4px var(--sp-3); font-size: 13px; }
.factor-kv dt { color: var(--text-muted); }
.factor-kv dd { margin: 0; font-family: var(--font-mono); }
.factor-metrics { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--sp-2); }
.factor-metric { display: flex; flex-direction: column; }
.factor-metric .label { font-size: 11px; }
.factor-metric .val { font-family: var(--font-mono); font-size: 16px; font-weight: 500; }
.factor-expr-box { font-family: var(--font-mono); font-size: 13px; background: var(--gray-1); padding: var(--sp-2); border-radius: var(--radius-badge); word-break: break-word; }
.factor-json { font-family: var(--font-mono); font-size: 12px; white-space: pre-wrap; word-break: break-word; max-height: 360px; overflow: auto; background: var(--gray-1); padding: var(--sp-2); border-radius: var(--radius-badge); margin: var(--sp-2) 0 0; }
.factor-diag-card { border: 1px solid #E8B5AE; background: #FCF4F3; border-radius: var(--radius-card); padding: var(--sp-3); }
.factor-diag-card-head { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); }
.factor-diag-card-title { font-weight: 500; color: var(--red); }
.factor-toast { position: fixed; bottom: var(--sp-6); left: 50%; transform: translateX(-50%);
  background: var(--navy); color: #fff; padding: var(--sp-2) var(--sp-4); border-radius: var(--radius-btn);
  font-size: 13px; z-index: 50; opacity: 0; transition: opacity .15s ease; }
.factor-toast.is-on { opacity: 1; }
.factor-toast--err { background: var(--red); }
.factor-drawer { border: 1px solid var(--border); border-radius: var(--radius-card); padding: var(--sp-3); background: var(--bg); }
.factor-drawer h4 { font-size: 13px; margin-bottom: var(--sp-1); }
.factor-drawer table { width: 100%; border-collapse: collapse; }
.factor-drawer td { padding: 2px var(--sp-2); vertical-align: top; font-size: 12px; }
.factor-drawer td:first-child { font-family: var(--font-mono); white-space: nowrap; color: var(--navy); }
.factor-menu-wrap { position: relative; display: inline-block; }
.factor-menu { position: absolute; top: calc(100% + 4px); left: 0; z-index: 20; min-width: 280px; max-height: 320px; overflow: auto;
  background: var(--bg); border: 1px solid var(--border-strong); border-radius: var(--radius-card); padding: var(--sp-1); }
.factor-menu button { display: block; width: 100%; text-align: left; border: none; background: none; padding: var(--sp-1) var(--sp-2);
  font-family: var(--font-mono); font-size: 12px; cursor: pointer; border-radius: var(--radius-badge); color: var(--text); }
.factor-menu button:hover { background: var(--gray-1); }
.factor-latency { font-family: var(--font-mono); font-size: 12px; }
`;
  document.head.appendChild(el("style", { id: STYLE_ID }, css));
}

// `el` shim — bound from ctx in render(); declared here so injectStyle/helpers
// can build nodes. Reassigned on every render() call.
let el = (tag) => document.createElement(tag);

// ----------------------------------------------------------------- helpers ----

function redundancyBadge(score, ctx) {
  if (score === null || score === undefined || !Number.isFinite(score)) {
    return ctx.el("span", { className: "badge badge--gray" }, "—");
  }
  let cls = "badge--green";
  let lbl = "unique";
  if (score > 0.7) { cls = "badge--red"; lbl = "redundant"; }
  else if (score >= 0.4) { cls = "badge--amber"; lbl = "similar"; }
  return ctx.el("span", { className: "badge " + cls }, `${ctx.fmt(score, 2)} ${lbl}`);
}

function decayBadge(days, ctx) {
  if (days === null || days === undefined || !Number.isFinite(days)) {
    return ctx.el("span", { className: "badge badge--gray" }, "—");
  }
  let cls = "badge--green";
  if (days > 30) cls = "badge--red";
  else if (days >= 10) cls = "badge--amber";
  return ctx.el("span", { className: "badge " + cls }, `${days}d`);
}

function metricNode(label, value, ctx) {
  return ctx.el("div", { className: "factor-metric" },
    ctx.el("span", { className: "label" }, label),
    ctx.el("span", { className: "val" }, value)
  );
}

function chartWrap(node) {
  const w = el("div", { className: "chart-wrap" });
  if (node) w.appendChild(node);
  return w;
}

// Render the lint AST (compact nested {op,args}/{field}/{lit}) as an indented tree.
function renderAst(node, ctx, prefix, isLast, isRoot) {
  const lines = [];
  const connector = isRoot ? "" : isLast ? "└── " : "├── ";
  let labelEl;
  if (node && typeof node === "object" && "op" in node) {
    labelEl = ctx.el("span", null,
      prefix + connector,
      ctx.el("span", { className: "ast-op" }, node.op)
    );
    lines.push(labelEl);
    const args = node.args || [];
    const childPrefix = prefix + (isRoot ? "" : isLast ? "    " : "│   ");
    args.forEach((a, i) => {
      lines.push(...renderAst(a, ctx, childPrefix, i === args.length - 1, false));
    });
  } else if (node && typeof node === "object" && "field" in node) {
    lines.push(ctx.el("span", null, prefix + connector,
      ctx.el("span", { className: "ast-field" }, String(node.field))));
  } else if (node && typeof node === "object" && "lit" in node) {
    lines.push(ctx.el("span", null, prefix + connector,
      ctx.el("span", { className: "ast-lit" }, String(node.lit))));
  } else {
    lines.push(ctx.el("span", null, prefix + connector + String(node)));
  }
  return lines;
}

// =================================================================== render ====

export function render(root, ctx) {
  el = ctx.el; // bind dom helper for module-level builders
  injectStyle();

  const { api, store, router, charts } = ctx;
  const cleanups = [];

  // ---- evaluation state (mutable across SSE events) ----
  let lastReport = null;
  let lintController = null;
  let evalController = null;
  let lintTimer = null;
  let lastDialect = null;

  // ---------------------------------------------------------------- editor ----
  const editor = ctx.el("textarea", {
    className: "factor-editor mono",
    spellcheck: "false",
    autocomplete: "off",
    autocorrect: "off",
    autocapitalize: "off",
    rows: "4",
    "aria-label": "Factor expression editor",
    placeholder: "e.g. ts_corr(close, volume, 20)",
  });

  const dialectBadge = ctx.el("span", { className: "badge badge--gray" }, "—");
  const lintStatus = ctx.el("span", { className: "muted" }, "");
  const diagBox = ctx.el("div", { className: "factor-lintbar" });
  const astPanel = ctx.el("div", { className: "factor-ast muted" }, "Type an expression to see its parse tree.");
  const astDetails = ctx.el("details", { className: "card" },
    ctx.el("summary", { style: { cursor: "pointer" }, className: "card-title" }, "AST"),
    ctx.el("div", { className: "card-body" }, astPanel)
  );

  // latency badge / spinner placeholder in toolbar
  const evalStatus = ctx.el("span", { className: "factor-latency muted" }, "");

  // ---- toolbar buttons ----
  function setDialectBadge(d) {
    const map = { qlib: "badge--teal", func: "badge--blue", python: "badge--blue" };
    const label = { func: "Assay-Python", python: "Assay-Python", qlib: "qlib" };
    dialectBadge.className = "badge " + (map[d] || "badge--gray");
    dialectBadge.textContent = label[d] || d || "—";
  }

  const convertBtn = ctx.el("button", {
    className: "btn btn--sm", type: "button",
    title: "Convert between qlib and Assay-Python syntax (in-browser, no API call)",
    onClick: () => {
      const cur = editor.value;
      const d = lastDialect || localDialect(cur);
      editor.value = d === "qlib" ? toAssayPython(cur) : toQlib(cur);
      scheduleLint();
      editor.focus();
    },
  }, "⇄ Convert syntax");

  // History menu
  const historyMenu = ctx.el("div", { className: "factor-menu hidden", role: "menu" });
  function rebuildHistoryMenu() {
    const hist = loadHistory();
    historyMenu.replaceChildren();
    if (!hist.length) {
      historyMenu.appendChild(ctx.el("div", { className: "muted", style: { padding: "8px" } }, "No history yet."));
      return;
    }
    for (const h of hist) {
      historyMenu.appendChild(ctx.el("button", {
        type: "button", title: h,
        onClick: () => {
          editor.value = h;
          historyMenu.classList.add("hidden");
          scheduleLint();
          editor.focus();
        },
      }, h.length > 60 ? h.slice(0, 57) + "…" : h));
    }
  }
  const historyBtn = ctx.el("button", {
    className: "btn btn--sm", type: "button", "aria-haspopup": "menu",
    onClick: () => {
      const hidden = historyMenu.classList.contains("hidden");
      if (hidden) { rebuildHistoryMenu(); historyMenu.classList.remove("hidden"); }
      else historyMenu.classList.add("hidden");
    },
  }, "↺ History");
  const historyWrap = ctx.el("div", { className: "factor-menu-wrap" }, historyBtn, historyMenu);

  // Operator docs drawer
  let docsOpen = false;
  const docsDrawer = ctx.el("div", { className: "factor-drawer hidden" });
  docsDrawer.appendChild(buildOperatorDocs(ctx));
  const docsBtn = ctx.el("button", {
    className: "btn btn--sm", type: "button",
    onClick: () => {
      docsOpen = !docsOpen;
      docsDrawer.classList.toggle("hidden", !docsOpen);
    },
  }, "? Operator docs");

  const evalBtn = ctx.el("button", {
    className: "btn btn--primary", type: "button", title: "Evaluate (Cmd/Ctrl+Enter)",
    onClick: () => runEvaluate(),
  }, "Evaluate ▶");

  const toolbar = ctx.el("div", { className: "factor-toolbar" },
    ctx.el("span", { className: "factor-dialect" }, ctx.el("span", { className: "label" }, "Dialect"), dialectBadge),
    convertBtn,
    historyWrap,
    docsBtn,
    ctx.el("span", { className: "grow" }),
    evalStatus,
    evalBtn
  );

  // ---------------------------------------------------------------- config row ----
  const cfg = buildConfigRow(ctx, store, () => runEvaluate());
  cleanups.push(cfg.cleanup);

  // ---------------------------------------------------------------- results grid ----
  const cards = buildCards(ctx);

  // ---------------------------------------------------------------- summary panel ----
  const summaryBody = ctx.el("div", {});
  const summaryPanel = ctx.el("aside", { className: "card factor-summary" },
    ctx.el("div", { className: "card-head" }, ctx.el("span", { className: "card-title" }, "Factor Report")),
    summaryBody
  );
  renderSummaryIdle(summaryBody, ctx);

  // ---------------------------------------------------------------- assemble ----
  const page = ctx.el("div", { className: "page factor-page" },
    ctx.el("div", { className: "page-header" },
      ctx.el("h1", { className: "page-title" }, "Single Factor Test"),
      ctx.el("span", { className: "page-subtitle" }, "Write, lint and evaluate one factor against the live panel.")
    ),
    ctx.el("section", { className: "card factor-editor-shell" },
      toolbar,
      editor,
      diagBox,
      astDetails,
      docsDrawer
    ),
    ctx.el("section", { className: "card" }, cfg.node),
    ctx.el("div", { className: "factor-main" },
      ctx.el("div", { className: "factor-grid" }, ...cards.nodes),
      summaryPanel
    )
  );
  root.replaceChildren(page);

  // ---------------------------------------------------------------- lint ----
  function scheduleLint() {
    if (lintTimer) clearTimeout(lintTimer);
    lintTimer = setTimeout(doLint, LINT_DEBOUNCE_MS);
  }

  async function doLint() {
    const expr = editor.value.trim();
    if (!expr) {
      setDialectBadge(null);
      diagBox.replaceChildren();
      astPanel.replaceChildren(ctx.el("span", { className: "muted" }, "Type an expression to see its parse tree."));
      editor.classList.remove("is-error");
      return;
    }
    // optimistic local dialect while the request is in-flight
    lastDialect = localDialect(expr);
    setDialectBadge(lastDialect);
    if (lintController) lintController.abort();
    lintController = new AbortController();
    try {
      const res = await api.lint(expr, { signal: lintController.signal });
      lastDialect = res.dialect || lastDialect;
      setDialectBadge(lastDialect);
      renderDiagnostics(res.diagnostics, expr);
      renderAstPanel(res.ast);
    } catch (err) {
      if (err && err.name === "AbortError") return;
      // lint endpoint should not 5xx; on transport failure just clear
      diagBox.replaceChildren(ctx.el("span", { className: "muted" }, "Lint unavailable."));
    }
  }

  function renderDiagnostics(diag, expr) {
    diagBox.replaceChildren();
    if (!diag) { editor.classList.remove("is-error"); return; }
    const errors = diag.errors || [];
    const warnings = diag.warnings || [];
    editor.classList.toggle("is-error", errors.length > 0);
    if (!errors.length && !warnings.length) {
      diagBox.appendChild(ctx.el("span", { className: "badge badge--green" }, "✓ parses"));
      const fields = diag.stats && Array.isArray(diag.stats.fields) ? diag.stats.fields : null;
      if (fields && fields.length) {
        diagBox.appendChild(ctx.el("span", { className: "muted" }, "fields: " + fields.join(", ")));
      }
      return;
    }
    for (const d of [...errors, ...warnings]) {
      const isErr = d.severity === "error";
      const row = ctx.el("div", { className: "factor-diag", style: { width: "100%" } },
        ctx.el("span", { className: "badge " + (isErr ? "badge--red" : "badge--amber"), title: d.code }, d.code || (isErr ? "error" : "warning")),
        ctx.el("div", {},
          ctx.el("div", { className: "factor-diag-msg" },
            ctx.el("strong", {}, (d.title || "") + (d.title ? " — " : "")), d.message || ""),
          d.location && d.location.snippet
            ? ctx.el("pre", { className: "factor-diag-snippet" }, d.location.snippet)
            : null,
          d.suggestion ? ctx.el("div", { className: "muted", style: { marginTop: "2px" } }, "Fix: " + d.suggestion) : null
        )
      );
      diagBox.appendChild(row);
    }
  }

  function renderAstPanel(ast) {
    astPanel.replaceChildren();
    if (!ast) {
      astPanel.appendChild(ctx.el("span", { className: "muted" }, "No parse tree (expression has a syntax error)."));
      return;
    }
    const lines = renderAst(ast, ctx, "", true, true);
    lines.forEach((ln, i) => {
      astPanel.appendChild(ln);
      if (i < lines.length - 1) astPanel.appendChild(document.createTextNode("\n"));
    });
  }

  // ---------------------------------------------------------------- evaluate ----
  function setEvalRunning(on) {
    evalBtn.disabled = on;
    cfg.evalBtn.disabled = on;
    evalStatus.className = "factor-latency muted";
    evalStatus.textContent = on ? "Evaluating…" : "";
  }

  function runEvaluate() {
    const expr = editor.value.trim();
    if (!expr) { toast(ctx, "Enter an expression first.", true); return; }
    if (evalController) { evalController.abort(); evalController = null; }

    pushHistory(expr);
    setEvalRunning(true);
    lastReport = null;

    // reset all cards to skeletons
    cards.reset();
    renderSummaryLoading(summaryBody, ctx);

    const cv = cfg.values();
    const req = {
      expr,
      universe: cv.universe,
      period: cv.period,
      horizons: cv.horizons,
      execution: cv.execution,
    };
    if (cv.neutralize) req.neutralize = cv.neutralize;

    let sawError = false;

    evalController = api.evaluateStream(req, (ev) => {
      const { event, data } = ev;
      if (event === "eval.started") {
        evalStatus.textContent = "Computing…";
      } else if (event === "eval.ic_series") {
        cards.renderIcSeries(data);
        cards.renderHeatmap(data);
      } else if (event === "eval.decay") {
        cards.renderDecay(data);
      } else if (event === "eval.groups") {
        cards.renderGroups(data);
      } else if (event === "eval.complete") {
        lastReport = data;
        // fill any cards from the final report (covers fields missing in mid-stream)
        cards.renderFromReport(data);
        finishEval(data);
        evalController = null;
      }
    }, (err) => {
      sawError = true;
      handleEvalError(err);
      evalController = null;
    });
  }

  function finishEval(report) {
    setEvalRunning(false);
    const ms = report && Number.isFinite(report.duration_ms) ? Math.round(report.duration_ms) : null;
    if (ms !== null) {
      evalStatus.className = "factor-latency";
      evalStatus.textContent = ms + "ms";
    } else {
      evalStatus.textContent = "";
    }
    if (report && report.failure_mode) {
      renderSummaryError(summaryBody, report, ctx);
    } else {
      renderSummaryReport(summaryBody, report, ctx, { onSave: () => saveReport(report), onCompare: () => compareReport(report) });
    }
  }

  function handleEvalError(err) {
    setEvalRunning(false);
    evalStatus.className = "factor-latency neg";
    evalStatus.textContent = "failed";
    const status = err && err.status;
    const envelope = err && err.envelope;
    summaryBody.replaceChildren();
    if (status === 503) {
      summaryBody.appendChild(ctx.el("div", { className: "error-state" },
        ctx.el("div", { className: "error-state-title" }, "No data ingested"),
        ctx.el("div", { className: "muted", style: { marginTop: "8px" } },
          "The panel is empty. Ingest NASDAQ-100 prices first:"),
        ctx.el("pre", { className: "factor-json" }, "assay prepare-nasdaq100"),
        ctx.el("div", { className: "muted" }, err && err.message ? err.message : "")
      ));
      return;
    }
    summaryBody.appendChild(ctx.el("div", { className: "error-state" },
      ctx.el("div", { className: "error-state-title" }, "Evaluation failed"),
      ctx.el("div", { className: "factor-diag-msg", style: { marginTop: "8px" } },
        err && err.message ? err.message : String(err)),
      envelope && envelope.code ? ctx.el("div", { className: "muted", style: { marginTop: "4px" } }, "Code: " + envelope.code) : null
    ));
  }

  async function saveReport(report) {
    if (!report) return;
    try {
      const res = await api.librarySave(report);
      const id = (res && (res.factor_id || (res.saved && res.factor_id))) || report.factor_id;
      toast(ctx, "Saved to library" + (id ? " — " + id : ""), false);
    } catch (err) {
      toast(ctx, "Save failed: " + (err && err.message ? err.message : err), true);
    }
  }

  function compareReport(report) {
    // Pass the seed factor via the URL query (the hash route is path-only, so a
    // query in the hash would break the matcher). The library page can read
    // ?seed / ?mode from window.location.search.
    if (report && report.factor_id) {
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("mode", "matrix");
        url.searchParams.set("seed", report.factor_id);
        window.history.replaceState(null, "", url);
      } catch (_) { /* no history API */ }
    }
    router.navigate("#/library");
  }

  // ---------------------------------------------------------------- wiring ----
  editor.addEventListener("input", scheduleLint);
  editor.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      runEvaluate();
    }
  });

  // close menus on outside click
  const onDocClick = (e) => {
    if (!historyWrap.contains(e.target)) historyMenu.classList.add("hidden");
  };
  document.addEventListener("click", onDocClick);
  cleanups.push(() => document.removeEventListener("click", onDocClick));

  cleanups.push(() => {
    if (lintTimer) clearTimeout(lintTimer);
    if (lintController) lintController.abort();
    if (evalController) evalController.abort();
  });

  // ---------------------------------------------------------------- prefill ----
  const prefillId = ctx.params && ctx.params.id;
  if (prefillId) {
    editor.value = "Loading factor " + prefillId + "…";
    renderSummaryLoading(summaryBody, ctx);
    api.libraryGet(prefillId).then((report) => {
      editor.value = report.expr || "";
      scheduleLint();
      lastReport = report;
      cards.renderFromReport(report);
      if (report.failure_mode) renderSummaryError(summaryBody, report, ctx);
      else renderSummaryReport(summaryBody, report, ctx, {
        onSave: () => saveReport(report), onCompare: () => compareReport(report),
      });
      const ms = Number.isFinite(report.duration_ms) ? Math.round(report.duration_ms) : null;
      if (ms !== null) { evalStatus.className = "factor-latency"; evalStatus.textContent = ms + "ms"; }
    }).catch((err) => {
      editor.value = "";
      summaryBody.replaceChildren(ctx.el("div", { className: "error-state" },
        ctx.el("div", { className: "error-state-title" }, "Factor not found"),
        ctx.el("div", { className: "muted" }, "Could not load '" + prefillId + "': " + (err && err.message ? err.message : err))
      ));
      scheduleLint();
    });
  } else {
    editor.value = DEFAULT_EXPR;
    scheduleLint();
  }

  // expose cleanup (app re-renders fresh per route; transient listeners scoped here)
  return () => cleanups.forEach((fn) => { try { fn(); } catch (_) {} });
}

// =================================================================== config row ====

function buildConfigRow(ctx, store, onEvaluate) {
  const snap = store.get();
  const globalUniverse = snap.universe;
  const globalPeriod = snap.period;

  // Universe (overridable; pre-set to global)
  const uniSel = ctx.el("select", { className: "select", "aria-label": "Universe" },
    ctx.el("option", { value: globalUniverse, selected: true }, globalUniverse)
  );
  // Period overrides
  const startInput = ctx.el("input", { type: "date", className: "input input-date", value: globalPeriod[0], "aria-label": "Eval period start" });
  const endInput = ctx.el("input", { type: "date", className: "input input-date", value: globalPeriod[1], "aria-label": "Eval period end" });

  const execSel = ctx.el("select", { className: "select", "aria-label": "Execution" },
    ...EXECUTIONS.map((x) => ctx.el("option", { value: x }, x))
  );

  const horizonInputs = HORIZONS_ALL.map((h) =>
    ctx.el("input", { type: "checkbox", value: String(h), checked: true, "aria-label": h + " day horizon" })
  );
  const horizons = ctx.el("div", { className: "factor-horizons" },
    ...HORIZONS_ALL.map((h, i) => ctx.el("label", {}, horizonInputs[i], h + "d"))
  );

  const neutralSel = ctx.el("select", { className: "select", "aria-label": "Neutralize" },
    ...NEUTRALIZE.map((n) => ctx.el("option", { value: n.value }, n.label))
  );

  const uniStar = ctx.el("span", { className: "factor-override hidden", title: "This evaluation uses a different universe than your global setting." }, " *");
  const periodStar = ctx.el("span", { className: "factor-override hidden", title: "This evaluation uses a different period than your global setting." }, " *");

  function refreshStars() {
    uniStar.classList.toggle("hidden", uniSel.value === store.get("universe"));
    const gp = store.get("period");
    periodStar.classList.toggle("hidden", startInput.value === gp[0] && endInput.value === gp[1]);
  }
  uniSel.addEventListener("change", refreshStars);
  startInput.addEventListener("change", refreshStars);
  endInput.addEventListener("change", refreshStars);

  // keep in sync when global store changes (only if user hasn't overridden)
  const unsub = store.subscribe((s) => {
    if (uniSel.value === globalUniverse || !uniSel.dataset.touched) {
      // refresh option to reflect new global default
      if (!uniSel.dataset.touched) {
        uniSel.replaceChildren(ctx.el("option", { value: s.universe, selected: true }, s.universe));
      }
    }
    refreshStars();
  });
  uniSel.addEventListener("change", () => { uniSel.dataset.touched = "1"; });

  const evalBtn = ctx.el("button", { className: "btn btn--primary", type: "button", onClick: onEvaluate }, "Evaluate ▶");

  const node = ctx.el("div", { className: "factor-config" },
    field(ctx, "Universe", ctx.el("span", { className: "flex items-center" }, uniSel, uniStar)),
    field(ctx, "Period", ctx.el("span", { className: "flex items-center gap-1" }, startInput, ctx.el("span", { className: "muted" }, "–"), endInput, periodStar)),
    field(ctx, "Execution", execSel),
    field(ctx, "Horizons", horizons),
    field(ctx, "Neutralize", neutralSel),
    ctx.el("div", { className: "factor-config-field" }, ctx.el("span", { className: "label", style: { visibility: "hidden" } }, "."), evalBtn)
  );

  return {
    node,
    evalBtn,
    cleanup: unsub,
    values() {
      const hz = HORIZONS_ALL.filter((h, i) => horizonInputs[i].checked);
      return {
        universe: uniSel.value,
        period: [startInput.value, endInput.value],
        horizons: hz.length ? hz : HORIZONS_ALL,
        execution: execSel.value,
        neutralize: neutralSel.value || null,
      };
    },
  };
}

function field(ctx, label, control) {
  return ctx.el("div", { className: "factor-config-field" },
    ctx.el("span", { className: "label" }, label),
    control
  );
}

// =================================================================== cards ====

function cardShell(ctx, title, hintText) {
  const hint = ctx.el("span", { className: "factor-hint" }, hintText || "");
  const body = ctx.el("div", { className: "card-body" });
  const node = ctx.el("section", { className: "card factor-card" },
    ctx.el("div", { className: "card-head" },
      ctx.el("span", { className: "card-title" }, title),
      hint
    ),
    body
  );
  return { node, body, hint };
}

function skeleton(ctx) {
  return ctx.el("div", { className: "skeleton skeleton-chart" });
}

function buildCards(ctx) {
  const { charts } = ctx;
  const c1 = cardShell(ctx, "IC & RankIC time series", "Run an evaluation to populate.");
  const c2 = cardShell(ctx, "IC decay by horizon", "");
  const c3 = cardShell(ctx, "Quintile returns (Q1→Q5)", "");
  const c4 = cardShell(ctx, "IC calendar heatmap", "");
  const c5 = cardShell(ctx, "Turnover", "");
  const c6 = cardShell(ctx, "Return distribution", "");

  // Optional cards default hidden until data is present in the final report.
  c5.node.classList.add("hidden");
  c6.node.classList.add("hidden");

  function reset() {
    for (const c of [c1, c2, c3, c4]) c.body.replaceChildren(skeleton(ctx));
    c5.body.replaceChildren();
    c6.body.replaceChildren();
    c5.node.classList.add("hidden");
    c6.node.classList.add("hidden");
    c1.hint.textContent = "";
    c2.hint.textContent = "";
    c3.hint.textContent = "";
    c4.hint.textContent = "";
  }

  function renderIcSeries(data) {
    const ic = data.ic || [];
    const rankIc = data.rank_ic || [];
    const dates = data.dates || [];
    c1.body.replaceChildren();
    if (!ic.some(Number.isFinite) && !rankIc.some(Number.isFinite)) {
      c1.body.appendChild(emptyChart(ctx, "No IC series."));
      c1.hint.textContent = "";
      return;
    }
    const series = [];
    if (ic.length) series.push({ name: "IC", values: ic, color: "#2D5BE3" });
    if (rankIc.length) series.push({ name: "RankIC", values: rankIc, color: "#0E8A7E" });
    c1.body.appendChild(charts.legend(series));
    c1.body.appendChild(chartWrap(charts.lineChart({
      series, dates, height: 240,
      bands: [{ from: -0.02, to: 0.02, color: "rgba(136,146,170,0.10)" }],
    })));
    const meanIc = Number.isFinite(data.ic_mean) ? data.ic_mean : mean(ic);
    c1.hint.textContent = describeIc(meanIc, ic);
  }

  function renderDecay(data) {
    const byH = data.ic_by_horizon || {};
    renderDecayBars(byH, data.halflife);
  }

  function renderDecayBars(byH, halflife) {
    c2.body.replaceChildren();
    const keys = Object.keys(byH).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
    const values = keys.map((k) => Number(byH[String(k)] ?? byH[k]));
    if (!keys.length || !values.some(Number.isFinite)) {
      c2.body.appendChild(emptyChart(ctx, "No decay data."));
      c2.hint.textContent = "";
      return;
    }
    const labels = keys.map((k) => k + "d");
    c2.body.appendChild(chartWrap(charts.barChart({ labels, values, valueLabels: true, height: 240 })));
    c2.hint.textContent = Number.isFinite(halflife)
      ? `Signal half-life: ${halflife} days.`
      : "IC across forward horizons.";
  }

  function renderGroups(data) {
    const q = data.quintile_returns;
    renderGroupBars(q);
  }

  function renderGroupBars(q) {
    c3.body.replaceChildren();
    const arr = toQuintileArray(q);
    if (!arr || !arr.length) { c3.body.appendChild(emptyChart(ctx, "No group returns.")); c3.hint.textContent = ""; return; }
    const labels = arr.map((_, i) => "Q" + (i + 1));
    // Q1 red (short), Q5 green (long), middle gray
    const colors = arr.map((_, i) => i === 0 ? "#C0392B" : i === arr.length - 1 ? "#1E7B4B" : "#8892AA");
    c3.body.appendChild(chartWrap(charts.barChart({ labels, values: arr, colors, valueLabels: true, height: 240 })));
    const spread = arr[arr.length - 1] - arr[0];
    const monotonic = isMonotonic(arr);
    c3.hint.textContent = monotonic
      ? `Monotonic long-short spread of ${ctx.fmt(spread, 4)}.`
      : `Non-monotonic (spread ${ctx.fmt(spread, 4)}) — factor may be non-linear.`;
  }

  function renderHeatmap(data) {
    const ic = data.ic || [];
    const dates = data.dates || [];
    c4.body.replaceChildren();
    if (!dates.length || !ic.some(Number.isFinite)) { c4.body.appendChild(emptyChart(ctx, "No IC heatmap data.")); c4.hint.textContent = ""; return; }
    c4.body.appendChild(chartWrap(charts.calendarHeatmap({ dates, values: ic, height: 160 })));
    c4.hint.textContent = "Monthly-mean IC — green positive, red negative.";
  }

  function renderTurnover(report) {
    const t = report.turnover_1d;
    if (!Number.isFinite(t)) { c5.node.classList.add("hidden"); return; }
    c5.node.classList.remove("hidden");
    c5.body.replaceChildren(
      ctx.el("div", { className: "factor-metrics" },
        metricNode("1-day turnover", ctx.pct(t, 1), ctx),
        metricNode("Implied retention", ctx.pct(Math.max(0, 1 - t), 1), ctx)
      ),
      ctx.el("div", { className: "muted", style: { marginTop: "8px" } },
        t < 0.2 ? "Low turnover — friendly to longer holding periods."
                : t > 0.5 ? "High turnover — transaction costs may dominate."
                          : "Moderate turnover.")
    );
  }

  function renderDistribution(report) {
    const arr = toQuintileArray(report.quintile_returns);
    if (!arr || !arr.length) { c6.node.classList.add("hidden"); return; }
    c6.node.classList.remove("hidden");
    const labels = arr.map((_, i) => "Q" + (i + 1));
    const colors = arr.map((_, i) => i === 0 ? "#C0392B" : i === arr.length - 1 ? "#1E7B4B" : "#8892AA");
    c6.body.replaceChildren(
      chartWrap(charts.barChart({ labels, values: arr, colors, valueLabels: true, height: 200 })),
      ctx.el("div", { className: "muted", style: { marginTop: "4px" } },
        "Mean forward return per quintile (return-distribution detail not exposed by the API).")
    );
  }

  // Fill every card from a complete FactorReport (handles non-streamed loads too).
  function renderFromReport(report) {
    if (!report) return;
    if ((report.ic_series && report.ic_series.length) || (report.rank_ic_series && report.rank_ic_series.length)) {
      renderIcSeries({
        ic: report.ic_series || [], rank_ic: report.rank_ic_series || [],
        dates: report.dates || [], ic_mean: report.ic,
      });
      renderHeatmap({ ic: report.ic_series || [], dates: report.dates || [] });
    } else {
      if (!c1.body.querySelector("svg")) { c1.body.replaceChildren(emptyChart(ctx, "No IC series.")); }
      if (!c4.body.querySelector("svg")) { c4.body.replaceChildren(emptyChart(ctx, "No heatmap data.")); }
    }
    if (report.ic_by_horizon && Object.keys(report.ic_by_horizon).length) {
      renderDecayBars(report.ic_by_horizon, report.decay_halflife_days);
    } else if (!c2.body.querySelector("svg")) {
      c2.body.replaceChildren(emptyChart(ctx, "No decay data."));
    }
    if (report.quintile_returns && toQuintileArray(report.quintile_returns)) {
      renderGroupBars(report.quintile_returns);
    } else if (!c3.body.querySelector("svg")) {
      c3.body.replaceChildren(emptyChart(ctx, "No group returns."));
    }
    renderTurnover(report);
    renderDistribution(report);
  }

  return {
    nodes: [c1.node, c2.node, c3.node, c4.node, c5.node, c6.node],
    reset,
    renderIcSeries, renderDecay, renderGroups, renderHeatmap, renderFromReport,
  };
}

function emptyChart(ctx, msg) {
  return ctx.el("div", { className: "empty-state", style: { minHeight: "200px" } },
    ctx.el("div", { className: "muted" }, msg));
}

// =================================================================== summary panel ====

function renderSummaryIdle(body, ctx) {
  body.replaceChildren(
    ctx.el("div", { className: "factor-summary-section" },
      ctx.el("div", { className: "muted" }, "Write an expression and press Evaluate (or Cmd/Ctrl+Enter) to compute its signal report.")
    )
  );
}

function renderSummaryLoading(body, ctx) {
  body.replaceChildren(
    ...[0, 1, 2].map(() => ctx.el("div", { className: "factor-summary-section" },
      ctx.el("div", { className: "skeleton skeleton-line", style: { width: "60%" } }),
      ctx.el("div", { className: "skeleton skeleton-line", style: { width: "90%" } }),
      ctx.el("div", { className: "skeleton skeleton-line", style: { width: "75%" } })
    ))
  );
}

function renderSummaryError(body, report, ctx) {
  body.replaceChildren();
  const diag = report.diagnostics || {};
  const errs = (diag.errors && diag.errors.length ? diag.errors : diag.warnings) || [];
  const primary = errs[0] || null;

  const head = ctx.el("div", { className: "factor-diag-card-head" },
    ctx.el("span", { className: "factor-diag-card-title" },
      "⚠ " + (primary ? primary.title : (report.failure_mode || "Factor failed"))),
    primary && primary.code ? ctx.el("span", { className: "badge badge--red" }, primary.code) : ctx.el("span", { className: "badge badge--red" }, report.failure_mode || "")
  );
  const card = ctx.el("div", { className: "factor-diag-card" }, head);
  if (primary) {
    if (primary.message) card.appendChild(ctx.el("div", { className: "factor-diag-msg", style: { marginTop: "8px" } }, primary.message));
    if (primary.location && primary.location.snippet) {
      card.appendChild(ctx.el("pre", { className: "factor-diag-snippet" }, primary.location.snippet));
    }
    if (primary.suggestion) {
      card.appendChild(ctx.el("div", { style: { marginTop: "8px" } },
        ctx.el("strong", {}, "Fix: "), primary.suggestion));
    }
  } else {
    card.appendChild(ctx.el("div", { className: "factor-diag-msg", style: { marginTop: "8px" } },
      "failure_mode: " + report.failure_mode));
    if (report.suggestion) card.appendChild(ctx.el("div", { style: { marginTop: "8px" } }, ctx.el("strong", {}, "Fix: "), report.suggestion));
  }
  body.appendChild(card);

  // Still show eval context + full JSON below the diagnostic.
  body.appendChild(contextSection(ctx, report));
  body.appendChild(jsonSection(ctx, report));
}

function renderSummaryReport(body, report, ctx, { onSave, onCompare }) {
  body.replaceChildren();
  if (!report) { renderSummaryIdle(body, ctx); return; }

  // Expression
  body.appendChild(ctx.el("div", { className: "factor-summary-section" },
    ctx.el("div", { className: "label" }, "Expression"),
    ctx.el("div", { className: "factor-expr-box mt-2" }, report.expr || "—"),
    report.expr_canonical && report.expr_canonical !== report.expr
      ? ctx.el("div", { className: "muted", style: { marginTop: "4px", fontFamily: "var(--font-mono)", fontSize: "12px" } }, "≡ " + report.expr_canonical)
      : null
  ));

  // Signal quality
  body.appendChild(ctx.el("div", { className: "factor-summary-section" },
    ctx.el("div", { className: "label" }, "Signal quality"),
    ctx.el("div", { className: "factor-metrics mt-2" },
      metricNode("IC", ctx.fmt(report.ic, 3), ctx),
      metricNode("ICIR", ctx.fmt(report.icir, 2), ctx),
      metricNode("RankIC", ctx.fmt(report.rank_ic, 3), ctx),
      metricNode("RankICIR", ctx.fmt(report.rank_icir, 2), ctx)
    ),
    ctx.el("div", { className: "flex items-center gap-2 mt-2" },
      ctx.el("span", { className: "label" }, "Decay half-life"),
      decayBadge(report.decay_halflife_days, ctx)
    )
  ));

  // Diagnostics
  body.appendChild(ctx.el("div", { className: "factor-summary-section" },
    ctx.el("div", { className: "label" }, "Diagnostics"),
    ctx.el("dl", { className: "factor-kv mt-2" },
      ctx.el("dt", {}, "Look-ahead"),
      ctx.el("dd", {}, report.lookahead_detected
        ? ctx.el("span", { className: "badge badge--red" }, "detected")
        : ctx.el("span", { className: "badge badge--green" }, "✓ clean")),
      ctx.el("dt", {}, "Redundancy"),
      ctx.el("dd", {}, redundancyBadge(report.redundancy_score, ctx),
        report.most_similar_factor ? ctx.el("span", { className: "muted", style: { marginLeft: "6px" } }, "→ " + report.most_similar_factor) : null),
      ctx.el("dt", {}, "Turnover (1d)"),
      ctx.el("dd", {}, report.turnover_1d != null ? ctx.pct(report.turnover_1d, 1) : "—"),
      ctx.el("dt", {}, "Failure"),
      ctx.el("dd", {}, report.failure_mode
        ? ctx.el("span", { className: "badge badge--amber" }, report.failure_mode)
        : ctx.el("span", { className: "badge badge--green" }, "None"))
    )
  ));

  // Suggestion
  if (report.suggestion) {
    body.appendChild(ctx.el("div", { className: "factor-summary-section" },
      ctx.el("div", { className: "label" }, "Suggestion"),
      ctx.el("div", { className: "mt-2", style: { fontSize: "13px" } }, report.suggestion)
    ));
  }

  // Evaluation context + lineage
  body.appendChild(contextSection(ctx, report));

  // Actions
  body.appendChild(ctx.el("div", { className: "factor-summary-section" },
    ctx.el("div", { className: "flex gap-2 wrap" },
      ctx.el("button", { className: "btn btn--primary btn--sm", type: "button", onClick: onSave }, "Save to library"),
      ctx.el("button", { className: "btn btn--sm", type: "button", onClick: onCompare }, "Compare with library →")
    )
  ));

  // Full JSON
  body.appendChild(jsonSection(ctx, report));
}

function contextSection(ctx, report) {
  const lineage = report.lineage || {};
  const period = report.eval_period || [];
  return ctx.el("div", { className: "factor-summary-section" },
    ctx.el("div", { className: "label" }, "Evaluation context"),
    ctx.el("dl", { className: "factor-kv mt-2" },
      ctx.el("dt", {}, "Universe"),
      ctx.el("dd", {}, (report.universe_id || "—") + (report.n_symbols ? "  ·  " + report.n_symbols + " symbols" : "")),
      ctx.el("dt", {}, "Period"),
      ctx.el("dd", {}, period.length === 2 ? period[0] + " → " + period[1] : "—"),
      ctx.el("dt", {}, "Dates"),
      ctx.el("dd", {}, report.n_dates != null ? ctx.fmtInt(report.n_dates) : "—"),
      ctx.el("dt", {}, "Execution"),
      ctx.el("dd", {}, report.execution || "—"),
      ctx.el("dt", {}, "Neutralize"),
      ctx.el("dd", {}, report.neutralize || "none"),
      ctx.el("dt", {}, "Duration"),
      ctx.el("dd", {}, Number.isFinite(report.duration_ms) ? Math.round(report.duration_ms) + "ms" : "—"),
      lineage && (lineage.snapshot || lineage.source || lineage.snapshot_id) ? ctx.el("dt", {}, "Lineage") : null,
      lineage && (lineage.snapshot || lineage.source || lineage.snapshot_id)
        ? ctx.el("dd", {}, [lineage.snapshot || lineage.snapshot_id, lineage.source].filter(Boolean).join("  ·  ") || "—")
        : null
    )
  );
}

function jsonSection(ctx, report) {
  const pre = ctx.el("pre", { className: "factor-json" }, safeJson(report));
  const copyBtn = ctx.el("button", { className: "btn btn--sm", type: "button",
    onClick: () => {
      try {
        navigator.clipboard.writeText(safeJson(report));
        toast(ctx, "JSON copied", false);
      } catch (_) { toast(ctx, "Copy unavailable", true); }
    } }, "Copy");
  return ctx.el("details", { className: "factor-summary-section" },
    ctx.el("summary", { style: { cursor: "pointer" }, className: "label" }, "Full JSON"),
    ctx.el("div", { className: "flex justify-between items-center mt-2" },
      ctx.el("span", { className: "muted", style: { fontSize: "12px" } }, "FactorReport"),
      copyBtn),
    pre
  );
}

function safeJson(obj) {
  try { return JSON.stringify(obj, null, 2); } catch (_) { return String(obj); }
}

// =================================================================== operator docs ====

function buildOperatorDocs(ctx) {
  const wrap = ctx.el("div", {});
  wrap.appendChild(ctx.el("h4", {}, "Operator reference"));
  wrap.appendChild(ctx.el("div", { className: "muted", style: { fontSize: "12px", marginBottom: "8px" } },
    "Assay-Python operators. Use ⇄ Convert to translate qlib syntax. The parser validates the full set — this is a curated subset; more are available."));
  for (const sec of OPERATOR_DOCS) {
    wrap.appendChild(ctx.el("h4", { style: { marginTop: "8px" } }, sec.group));
    const tbl = ctx.el("table", {});
    const tb = ctx.el("tbody", {});
    for (const [sig, desc] of sec.ops) {
      tb.appendChild(ctx.el("tr", {},
        ctx.el("td", {}, sig),
        ctx.el("td", { className: "muted" }, desc)
      ));
    }
    tbl.appendChild(tb);
    wrap.appendChild(tbl);
  }
  // syntax-bridge cheat sheet
  wrap.appendChild(ctx.el("h4", { style: { marginTop: "12px" } }, "qlib ↔ Assay"));
  const bridge = [
    ["$close", "close"], ["Ref(x, d)", "ts_delay(x, d)"], ["Mean(x, d)", "ts_mean(x, d)"],
    ["Std(x, d)", "ts_std(x, d)"], ["Corr(x, y, d)", "ts_corr(x, y, d)"], ["EMA(x, d)", "ts_ema(x, d)"],
    ["Rank(x)", "cs_rank(x)"], ["Delta(x, d)", "ts_delta(x, d)"], ["Sum(x, d)", "ts_sum(x, d)"],
  ];
  const btbl = ctx.el("table", {});
  const btb = ctx.el("tbody", {});
  for (const [a, b] of bridge) {
    btb.appendChild(ctx.el("tr", {}, ctx.el("td", {}, a), ctx.el("td", {}, "→ " + b)));
  }
  btbl.appendChild(btb);
  wrap.appendChild(btbl);
  return wrap;
}

// =================================================================== toast ====

let _toastTimer = null;
function toast(ctx, msg, isErr) {
  let node = document.getElementById("factor-toast");
  if (!node) {
    node = ctx.el("div", { id: "factor-toast", className: "factor-toast", role: "status", "aria-live": "polite" });
    document.body.appendChild(node);
  }
  node.className = "factor-toast" + (isErr ? " factor-toast--err" : "");
  node.textContent = msg;
  // force reflow to restart transition
  void node.offsetWidth;
  node.classList.add("is-on");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => node.classList.remove("is-on"), 2600);
}

// =================================================================== math utils ====

function mean(arr) {
  const fin = (arr || []).filter((v) => Number.isFinite(v));
  if (!fin.length) return NaN;
  return fin.reduce((a, b) => a + b, 0) / fin.length;
}

function describeIc(meanIc, ic) {
  if (!Number.isFinite(meanIc)) return "";
  const sign = meanIc >= 0 ? "positive" : "negative";
  const fin = (ic || []).filter((v) => Number.isFinite(v));
  if (!fin.length) return `Mean IC ${meanIc.toFixed(3)}.`;
  const posFrac = fin.filter((v) => v > 0).length / fin.length;
  const stable = Math.abs(posFrac - 0.5) > 0.15;
  return `Mean IC ${meanIc.toFixed(3)} (${sign}). ${stable ? "Direction is consistent." : "Sign flips across the period."}`;
}

// quintile_returns may be a list [Q1..Q5] (report/stream) or a {"Q1":..} dict.
function toQuintileArray(q) {
  if (!q) return null;
  if (Array.isArray(q)) {
    const arr = q.map(Number).filter((v) => Number.isFinite(v));
    return arr.length ? arr : null;
  }
  if (typeof q === "object") {
    const keys = Object.keys(q).filter((k) => /^Q\d+$/.test(k)).sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
    if (!keys.length) return null;
    const arr = keys.map((k) => Number(q[k])).filter((v) => Number.isFinite(v));
    return arr.length ? arr : null;
  }
  return null;
}

function isMonotonic(arr) {
  if (!arr || arr.length < 2) return false;
  let inc = true, dec = true;
  for (let i = 1; i < arr.length; i++) {
    if (arr[i] < arr[i - 1]) inc = false;
    if (arr[i] > arr[i - 1]) dec = false;
  }
  return inc || dec;
}
