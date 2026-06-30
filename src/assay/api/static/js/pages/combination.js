// pages/combination.js — Factor Combination (design-doc §6.3).
//
// Blends several factors (selected from the library / Alpha catalogs or typed
// inline) into one composite alpha: standardise → orient → fit weights on a TRAIN
// window → (optionally) pick the scheme on a VALIDATION window → score the
// composite's IC / RankIC / ICIR out-of-sample on a TEST window. Renders the chosen
// method, the composite weights (signed bar chart + table), the train/val/test
// scorecard, the validation selection scores, and any factors that failed to
// evaluate.
//
// Contract: export render(root, ctx); ctx = {api, store, charts, el, t, ...}.
// POST /v1/combination (api.combineFactors) — blocking, NaN-safe JSON.

const STYLE_ID = "combo-page-style";
// Fallback method list if GET /v1/combination/methods is unavailable; otherwise the
// dropdown is populated live (so learned models appear only when their lib is installed).
const FALLBACK_METHODS = [
  { name: "equal", kind: "analytic", available: true },
  { name: "icir_weight", kind: "analytic", available: true },
  { name: "ic_weight", kind: "analytic", available: true },
  { name: "ols", kind: "analytic", available: true },
  { name: "ridge", kind: "analytic", available: true },
  { name: "nnls", kind: "analytic", available: true },
  { name: "max_icir", kind: "analytic", available: true },
];
const KIND_ORDER = ["analytic", "linear", "tree", "boost", "neural"];
const STANDARDIZE = ["zscore", "rank"];
const DEFAULT_FACTORS = ["ts_mean(close, 5)", "rank(close)", "delta(close, 10)", "-1 * ts_std(close, 20)"];

let el = (tag) => document.createElement(tag);

// ----------------------------------------------------------------- styles ----
function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const css = `
.cb-page { display: flex; flex-direction: column; gap: var(--sp-4); }
.cb-factors { width: 100%; min-height: 96px; resize: vertical; font-family: var(--font-mono);
  font-size: 13px; line-height: 1.5; padding: var(--sp-3); border: 1px solid var(--border);
  border-radius: var(--radius-card); background: var(--gray-1); color: var(--text); }
.cb-factors:focus-visible { outline: none; box-shadow: var(--focus-ring); border-color: var(--blue); }
.cb-form { display: flex; flex-direction: column; gap: var(--sp-3); }
.cb-row { display: flex; align-items: flex-end; gap: var(--sp-3); flex-wrap: wrap; }
.cb-field { display: flex; flex-direction: column; gap: var(--sp-1); }
.cb-field > .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-muted); }
.cb-num { width: 96px; font-family: var(--font-mono); }
.cb-fieldset { border: 1px solid var(--border); border-radius: var(--radius-card); padding: var(--sp-3); }
.cb-fieldset > legend { font-size: 12px; font-weight: 600; padding: 0 var(--sp-1); }
.cb-hint { font-size: 12px; color: var(--text-muted); }
.cb-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-3); }
@media (max-width: 900px) { .cb-kpis { grid-template-columns: repeat(2, 1fr); } }
.cb-kpi { display: flex; flex-direction: column; gap: 2px; padding: var(--sp-2) 0; }
.cb-kpi .label { font-size: 11px; color: var(--text-muted); }
.cb-kpi .val { font-family: var(--font-mono); font-size: 20px; font-weight: 600; }
.cb-kpi .val.pos { color: var(--green, #1E7B4B); } .cb-kpi .val.neg { color: var(--red, #C0392B); }
.cb-grid2 { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: var(--sp-4); align-items: start; }
@media (max-width: 1100px) { .cb-grid2 { grid-template-columns: minmax(0, 1fr); } }
.cb-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.cb-table th, .cb-table td { padding: 5px 10px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--border); }
.cb-table th:first-child, .cb-table td:first-child { text-align: left; }
.cb-table thead th { font-weight: 600; color: var(--text-muted); }
.cb-table tbody tr:hover { background: var(--gray-1); }
.cb-mono { font-family: var(--font-mono); }
.cb-err { border: 1px solid #E8B5AE; background: #FCF4F3; border-radius: var(--radius-card); padding: var(--sp-3); }
.cb-err-title { font-weight: 600; color: var(--red); }
.cb-json { font-family: var(--font-mono); font-size: 12px; white-space: pre-wrap; word-break: break-word;
  max-height: 360px; overflow: auto; background: var(--gray-1); padding: var(--sp-2); border-radius: var(--radius-badge); margin: var(--sp-2) 0 0; }
.cb-latency { font-family: var(--font-mono); font-size: 12px; }
.cb-pos { color: var(--green, #1E7B4B); } .cb-neg { color: var(--red, #C0392B); }
.cb-chosen { display: inline-flex; align-items: center; gap: 6px; font-family: var(--font-mono);
  font-size: 13px; background: var(--blue, #2D5BE3); color: #fff; padding: 3px 10px; border-radius: var(--radius-badge); }
`;
  document.head.appendChild(el("style", { id: STYLE_ID }, css));
}

// ----------------------------------------------------------------- helpers ----
function field(ctx, label, control) {
  return ctx.el("div", { className: "cb-field" }, ctx.el("span", { className: "label" }, label), control);
}
function iso(d) { return d.toISOString().slice(0, 10); }
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
function autoSplits([s, e]) {
  const sd = new Date(s), ed = new Date(e);
  const span = ed.getTime() - sd.getTime();
  if (!(span > 0)) return { train: [s, s], val: [s, s], test: [s, e] };
  const p1 = new Date(sd.getTime() + span * 0.6);
  const p2 = new Date(sd.getTime() + span * 0.8);
  return {
    train: [iso(sd), iso(p1)],
    val: [iso(addDays(p1, 1)), iso(p2)],
    test: [iso(addDays(p2, 1)), iso(ed)],
  };
}
function parseFactors(text) {
  return String(text || "").split("\n").map((l) => l.trim()).filter(Boolean);
}
function parseHorizons(text) {
  const xs = String(text || "").split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => Number.isFinite(n) && n >= 1);
  return xs.length ? Array.from(new Set(xs)).sort((a, b) => a - b) : [1];
}
function safeJson(o) { try { return JSON.stringify(o, null, 2); } catch (_) { return String(o); } }
function signTone(v) { return !Number.isFinite(v) ? "" : v > 0 ? "pos" : v < 0 ? "neg" : ""; }

// =================================================================== render ====
export function render(root, ctx) {
  el = ctx.el;
  injectStyle();
  const { api, store } = ctx;
  const cleanups = [];
  let controller = null;

  const snap = store.get();
  const globalPeriod = snap.period;

  // ---- factor list ----
  const factorsTa = ctx.el("textarea", {
    className: "cb-factors", spellcheck: "false", autocomplete: "off",
    placeholder: ctx.t("combo.factorsPlaceholder"),
  });
  factorsTa.value = DEFAULT_FACTORS.join("\n");
  const libStatus = ctx.el("span", { className: "cb-hint" }, "");
  const loadLibBtn = ctx.el("button", { className: "btn btn--sm", type: "button",
    onClick: () => addLibraryFactors() }, ctx.t("combo.loadLibrary"));

  async function addLibraryFactors() {
    loadLibBtn.disabled = true;
    try {
      const res = await api.libraryList({ sort_by: "rank_icir", limit: 10, universe: uniSel.value });
      const ids = (res && res.factors ? res.factors : []).map((f) => f.factor_id).filter(Boolean);
      const existing = new Set(parseFactors(factorsTa.value));
      const add = ids.map((id) => `lib:${id}`).filter((s) => !existing.has(s));
      if (add.length) factorsTa.value = (factorsTa.value.trim() + "\n" + add.join("\n")).trim();
      libStatus.textContent = ctx.t("combo.loaded", { n: add.length });
    } catch (err) {
      libStatus.textContent = (err && err.message) ? err.message : "—";
    } finally {
      loadLibBtn.disabled = false;
    }
  }

  // ---- core controls ----
  const uniSel = ctx.el("select", { className: "select" },
    ctx.el("option", { value: snap.universe, selected: true }, snap.universe));
  const unsub = store.subscribe((s) => {
    if (!uniSel.dataset.touched) uniSel.replaceChildren(ctx.el("option", { value: s.universe, selected: true }, s.universe));
  });
  uniSel.addEventListener("change", () => { uniSel.dataset.touched = "1"; });
  cleanups.push(unsub);

  const dateInput = (v) => ctx.el("input", { type: "date", className: "input input-date", value: v });
  const sp = autoSplits(globalPeriod);
  const trainStart = dateInput(sp.train[0]), trainEnd = dateInput(sp.train[1]);
  const valStart = dateInput(sp.val[0]), valEnd = dateInput(sp.val[1]);
  const testStart = dateInput(sp.test[0]), testEnd = dateInput(sp.test[1]);
  const rangeRow = (label, a, b) => field(ctx, label,
    ctx.el("span", { className: "flex items-center gap-1" }, a, ctx.el("span", { className: "muted" }, "–"), b));

  const autofillBtn = ctx.el("button", { className: "btn btn--sm", type: "button", onClick: () => {
    const s = autoSplits(store.get().period);
    trainStart.value = s.train[0]; trainEnd.value = s.train[1];
    valStart.value = s.val[0]; valEnd.value = s.val[1];
    testStart.value = s.test[0]; testEnd.value = s.test[1];
  } }, ctx.t("combo.autofill"));

  const methodSel = ctx.el("select", { className: "select" });
  function populateMethods(methods) {
    // "auto" first, then methods grouped by kind (unavailable models disabled).
    const groups = new Map();
    for (const m of methods) {
      if (!groups.has(m.kind)) groups.set(m.kind, []);
      groups.get(m.kind).push(m);
    }
    const kinds = [...KIND_ORDER.filter((k) => groups.has(k)), ...[...groups.keys()].filter((k) => !KIND_ORDER.includes(k))];
    const children = [ctx.el("option", { value: "auto", selected: true }, ctx.t("combo.m_auto"))];
    for (const kind of kinds) {
      const og = ctx.el("optgroup", { label: ctx.t("combo.kind_" + kind) || kind });
      for (const m of groups.get(kind)) {
        const label = (ctx.t("combo.m_" + m.name) || m.name) + (m.available ? "" : ` (${ctx.t("combo.unavailable")})`);
        og.appendChild(ctx.el("option", { value: m.name, disabled: !m.available }, label));
      }
      children.push(og);
    }
    methodSel.replaceChildren(...children);
    methodSel.value = "auto";
  }
  populateMethods(FALLBACK_METHODS);
  api.combinationMethods().then((res) => {
    if (res && Array.isArray(res.methods) && res.methods.length) populateMethods(res.methods);
  }).catch(() => {});
  const stdSel = ctx.el("select", { className: "select" },
    ...STANDARDIZE.map((s) => ctx.el("option", { value: s }, s)));
  const horizonsInput = ctx.el("input", { type: "text", className: "input cb-num", value: "1, 5, 10", style: { width: "120px" } });
  const ridgeInput = ctx.el("input", { type: "number", className: "input cb-num", value: "10", min: "0", step: "1" });
  const embargoInput = ctx.el("input", { type: "number", className: "input cb-num", value: "", min: "0", step: "1",
    placeholder: ctx.t("combo.embargoAuto") });

  const runStatus = ctx.el("span", { className: "cb-latency muted" }, "");
  const runBtn = ctx.el("button", { className: "btn btn--primary", type: "button", onClick: () => run() },
    ctx.t("combo.run") + " ▶");

  const form = ctx.el("div", { className: "cb-form" },
    ctx.el("div", { className: "cb-row" },
      field(ctx, ctx.t("combo.universe"), uniSel),
      field(ctx, ctx.t("combo.method"), methodSel),
      field(ctx, ctx.t("combo.standardize"), stdSel),
      field(ctx, ctx.t("combo.horizons"), horizonsInput),
      field(ctx, ctx.t("combo.ridge"), ridgeInput),
      field(ctx, ctx.t("combo.embargo"), embargoInput)),
    ctx.el("fieldset", { className: "cb-fieldset" },
      ctx.el("legend", {}, ctx.t("combo.splits")),
      ctx.el("div", { className: "cb-row" },
        rangeRow(ctx.t("combo.train"), trainStart, trainEnd),
        rangeRow(ctx.t("combo.val"), valStart, valEnd),
        rangeRow(ctx.t("combo.test"), testStart, testEnd),
        autofillBtn)),
    ctx.el("div", { className: "cb-row", style: { justifyContent: "flex-end", alignItems: "center" } },
      runStatus, runBtn));

  const resultsBody = ctx.el("div", {});

  const page = ctx.el("div", { className: "page cb-page" },
    ctx.el("div", { className: "page-header" },
      ctx.el("h1", { className: "page-title" }, ctx.t("combo.title")),
      ctx.el("span", { className: "page-subtitle" }, ctx.t("combo.subtitle"))),
    ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-head" },
        ctx.el("div", { className: "flex items-center", style: { justifyContent: "space-between", width: "100%", gap: "8px" } },
          ctx.el("span", { className: "card-title" }, ctx.t("combo.factors")),
          ctx.el("span", { className: "flex items-center gap-1" }, libStatus, loadLibBtn))),
      ctx.el("div", { className: "card-body" },
        factorsTa,
        ctx.el("div", { className: "cb-hint", style: { marginTop: "6px" } }, ctx.t("combo.factorsHint")))),
    ctx.el("section", { className: "card" }, ctx.el("div", { className: "card-body" }, form)),
    resultsBody);
  root.replaceChildren(page);
  renderIdle();

  // ---------------------------------------------------------------- run ----
  function buildBody() {
    const body = {
      factors: parseFactors(factorsTa.value),
      train: [trainStart.value, trainEnd.value],
      val: [valStart.value, valEnd.value],
      test: [testStart.value, testEnd.value],
      universe: uniSel.value,
      horizons: parseHorizons(horizonsInput.value),
      method: methodSel.value,
      standardize: stdSel.value,
      ridge_lambda: Number(ridgeInput.value) || 0,
    };
    const emb = embargoInput.value.trim();
    if (emb !== "") body.embargo = Number(emb) || 0;
    return body;
  }

  function setRunning(on) {
    runBtn.disabled = on;
    runStatus.className = "cb-latency muted";
    runStatus.textContent = on ? ctx.t("combo.running") : "";
  }

  async function run() {
    const factors = parseFactors(factorsTa.value);
    if (!factors.length) { toast(ctx, ctx.t("combo.enterFactors"), true); return; }
    if (controller) controller.abort();
    controller = new AbortController();
    setRunning(true);
    renderLoading();
    const t0 = performance.now();
    try {
      const out = await api.combineFactors(buildBody(), { signal: controller.signal });
      const ms = Math.round(performance.now() - t0);
      runStatus.className = "cb-latency";
      runStatus.textContent = ctx.t("combo.ms", { ms });
      renderResult(out);
    } catch (err) {
      if (err && err.name === "AbortError") return;
      runStatus.className = "cb-latency cb-neg";
      runStatus.textContent = ctx.t("combo.failed");
      renderError(err);
    } finally {
      setRunning(false);
      controller = null;
    }
  }

  // ---------------------------------------------------------------- results ----
  function renderIdle() {
    resultsBody.replaceChildren(ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-body" },
        ctx.el("div", { className: "empty-state", style: { minHeight: "120px" } },
          ctx.el("div", { className: "muted" }, ctx.t("combo.idle"))))));
  }
  function renderLoading() {
    resultsBody.replaceChildren(ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-body" }, ctx.el("div", { className: "skeleton skeleton-chart" }))));
  }
  function renderError(err) {
    const msg = (err && err.message) ? err.message : String(err);
    resultsBody.replaceChildren(ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-body" },
        ctx.el("div", { className: "cb-err" },
          ctx.el("div", { className: "cb-err-title" }, ctx.t("combo.failTitle")),
          ctx.el("div", { style: { marginTop: "8px" } }, msg)))));
  }

  function renderResult(out) {
    if (!out) { renderIdle(); return; }
    if (out.failure) {
      resultsBody.replaceChildren(ctx.el("section", { className: "card" },
        ctx.el("div", { className: "card-body" },
          ctx.el("div", { className: "cb-err" },
            ctx.el("div", { className: "cb-err-title" }, ctx.t("combo.failTitle")),
            ctx.el("div", { style: { marginTop: "8px" } }, out.detail || ctx.t("combo.noData"))))));
      return;
    }

    const head = ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-head" },
        ctx.el("div", { className: "flex items-center", style: { gap: "10px", flexWrap: "wrap" } },
          ctx.el("span", { className: "card-title" }, ctx.t("combo.chosen")),
          ctx.el("span", { className: "cb-chosen" }, ctx.t("combo.m_" + out.method) || out.method),
          ctx.el("span", { className: "cb-hint" },
            `${out.standardize} · h=${out.horizon} · ${ctx.t("combo.turnover")}: ${fmtNum(out.diagnostics && out.diagnostics.composite_turnover_1d, 3)}`))),
      ctx.el("div", { className: "card-body" }, buildScorecard(out)));

    const weightsCard = ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-head" }, ctx.el("span", { className: "card-title" }, ctx.t("combo.weightsTitle"))),
      ctx.el("div", { className: "card-body" }, buildWeights(out)));

    const sideCards = [];
    if (out.selection) sideCards.push(buildSelection(out));
    if (out.dropped && out.dropped.length) sideCards.push(buildDropped(out));

    resultsBody.replaceChildren(
      head,
      ctx.el("div", { className: "cb-grid2" }, weightsCard, ctx.el("div", { style: { display: "flex", flexDirection: "column", gap: "var(--sp-4)" } }, ...sideCards)),
      jsonDetails(out));
  }

  function buildScorecard(out) {
    const rows = [["combo.train", out.train], ["combo.val", out.val], ["combo.test", out.test]];
    const thead = ctx.el("thead", {}, ctx.el("tr", {},
      ...["combo.split", "Information Coefficient", "ICIR", "RankIC", "RankICIR", "combo.nDates"]
        .map((k, i) => ctx.el("th", {}, i === 1 ? "IC" : (k.startsWith("combo.") ? ctx.t(k) : k)))));
    const tbody = ctx.el("tbody", {});
    for (const [label, m] of rows) {
      m = m || {};
      tbody.appendChild(ctx.el("tr", {},
        ctx.el("td", {}, ctx.t(label)),
        cell(m.ic, 4, true), cell(m.icir, 3, true), cell(m.rank_ic, 4, true), cell(m.rank_icir, 3, true),
        ctx.el("td", { className: "cb-mono" }, m.n_dates != null ? String(m.n_dates) : "—")));
    }
    return ctx.el("table", { className: "cb-table" }, thead, tbody);
  }

  function cell(v, dp, tone) {
    const n = Number(v);
    const cls = "cb-mono" + (tone ? " " + (signTone(n) === "pos" ? "cb-pos" : signTone(n) === "neg" ? "cb-neg" : "") : "");
    return ctx.el("td", { className: cls }, fmtNum(v, dp));
  }

  function buildWeights(out) {
    const names = out.factor_names || [];
    const w = out.weights || {};
    const orient = out.orientation || {};
    const ic = out.per_factor_train_ic || {};
    // signed weight bar chart
    const values = names.map((n) => Number(w[n]));
    const colors = values.map((v) => (v >= 0 ? "#2D5BE3" : "#C0392B"));
    const labels = names.map((n) => (n.length > 14 ? n.slice(0, 13) + "…" : n));
    const chart = ctx.charts && ctx.charts.barChart
      ? ctx.charts.barChart({ labels, values, colors, dates: names, height: 220, width: 560,
          interactive: true, valueFmt: (v) => fmtNum(v, 3) })
      : null;

    const weightHdr = out.weight_kind === "importance" ? ctx.t("combo.importance") : ctx.t("combo.thWeight");
    const thead = ctx.el("thead", {}, ctx.el("tr", {},
      ctx.el("th", {}, ctx.t("combo.thFactor")), ctx.el("th", {}, ctx.t("combo.thOrient")),
      ctx.el("th", {}, weightHdr), ctx.el("th", {}, ctx.t("combo.thTrainIC"))));
    const tbody = ctx.el("tbody", {});
    for (const n of names) {
      const o = Number(orient[n]);
      tbody.appendChild(ctx.el("tr", {},
        ctx.el("td", { className: "cb-mono", title: n }, n),
        ctx.el("td", {}, o < 0 ? "↓ −1" : "↑ +1"),
        cell(w[n], 3, true),
        cell(ic[n], 4, true)));
    }
    const tableWrap = ctx.el("div", { style: { marginTop: "var(--sp-3)" } },
      ctx.el("table", { className: "cb-table" }, thead, tbody));
    return ctx.el("div", {}, chart ? wrap(chart) : null, tableWrap);
  }

  function buildSelection(out) {
    const sel = out.selection || {};
    const thead = ctx.el("thead", {}, ctx.el("tr", {}, ctx.el("th", {}, ctx.t("combo.method")), ctx.el("th", {}, "ICIR")));
    const tbody = ctx.el("tbody", {});
    const entries = Object.entries(sel).sort((a, b) => (Number(b[1]) || -Infinity) - (Number(a[1]) || -Infinity));
    for (const [m, v] of entries) {
      const isChosen = m === out.method;
      tbody.appendChild(ctx.el("tr", { style: isChosen ? { fontWeight: "600" } : {} },
        ctx.el("td", {}, (ctx.t("combo.m_" + m) || m) + (isChosen ? " ✓" : "")),
        cell(v, 3, true)));
    }
    return ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-head" }, ctx.el("span", { className: "card-title" }, ctx.t("combo.selTitle"))),
      ctx.el("div", { className: "card-body" }, ctx.el("table", { className: "cb-table" }, thead, tbody)));
  }

  function buildDropped(out) {
    const body = ctx.el("div", { className: "card-body" });
    for (const d of out.dropped) {
      body.appendChild(ctx.el("div", { className: "cb-hint", style: { fontFamily: "var(--font-mono)" } },
        `${d.name} — ${d.failure_mode || "drop"}`));
    }
    return ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-head" }, ctx.el("span", { className: "card-title" }, ctx.t("combo.droppedTitle"))),
      body);
  }

  function jsonDetails(out) {
    return ctx.el("details", { className: "card" },
      ctx.el("summary", { style: { cursor: "pointer", padding: "var(--sp-3)" }, className: "card-title" }, ctx.t("combo.fullJson")),
      ctx.el("div", { className: "card-body" }, ctx.el("pre", { className: "cb-json" }, safeJson(out))));
  }

  return () => { if (controller) controller.abort(); cleanups.forEach((fn) => { try { fn(); } catch (_) {} }); };
}

// ----------------------------------------------------------------- small utils ----
function wrap(node) {
  const w = el("div", { className: "chart-wrap" });
  if (node) w.appendChild(node);
  return w;
}
function fmtNum(v, dp) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(dp == null ? 2 : dp) : "—";
}

let _toastTimer = null;
function toast(ctx, msg, isErr) {
  let node = document.getElementById("factor-toast");
  if (!node) {
    node = ctx.el("div", { id: "factor-toast", className: "factor-toast", role: "status", "aria-live": "polite" });
    document.body.appendChild(node);
  }
  node.className = "factor-toast" + (isErr ? " factor-toast--err" : "");
  node.textContent = msg;
  void node.offsetWidth;
  node.classList.add("is-on");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => node.classList.remove("is-on"), 2600);
}
