// pages/portfolio.js — Portfolio Backtest (portfolio design-doc Phase 5).
//
// Turns a factor expression into an achievable *net* return: builds a portfolio,
// rebalances on a schedule, applies real constraints (A-share ±price-limit + T+1)
// and trading costs (commission / stamp duty / transfer fee / slippage), then
// marks-to-market daily. Renders the resulting PortfolioReport — NAV vs benchmark,
// KPIs (Sharpe / drawdown / turnover / cost drag), monthly returns and A-share
// execution stats.
//
// Contract: export render(root, ctx); ctx = {api, store, charts, el, t, lightbox...}.
// POST /v1/portfolio/backtest (api.portfolioBacktest) — blocking, NaN-safe JSON.
//
// Zero deps. Charts are hand-rolled SVG (ctx.charts); cards are zoomable via
// ctx.zoomButton (lightbox.js).

const STYLE_ID = "portfolio-page-style";
const HISTORY_KEY = "assay_expr_history"; // shared with the Single Factor Test
const DEFAULT_EXPR = "Sub($open, $close)";

// universe -> market (mirrors AssayService._UNIVERSE_MARKET). Drives A-share rule
// forcing, fee presets and the long-only constraint.
const A_UNIVERSES = new Set(["CSI300", "CSI500", "CSI1000", "CSI800"]);
function marketForUniverse(u) {
  const x = String(u || "").toUpperCase();
  if (A_UNIVERSES.has(x)) return "A";
  if (x === "HSI") return "HK";
  return "US";
}

// Section-6 cost/limit defaults (mirror PortfolioBacktestConfig.preset). The fee
// inputs prefill from here and refresh when the market changes.
const FEE_PRESETS = {
  A:  { commission_rate: 0.0003, stamp_duty_rate: 0.0005, transfer_fee_rate: 0.00002, commission_min: 5, slippage_k: 0.20 },
  US: { commission_rate: 0.0005, stamp_duty_rate: 0.0,    transfer_fee_rate: 0.00001, commission_min: 0, slippage_k: 0.10 },
  HK: { commission_rate: 0.0004, stamp_duty_rate: 0.0013, transfer_fee_rate: 0.00003, commission_min: 0, slippage_k: 0.15 },
};

const REBALANCE_TYPES = ["daily", "weekly", "monthly", "quarterly"];
const WEIGHT_METHODS = ["equal", "signal_prop", "quintile"];
const EXECUTIONS = ["next_open", "next_close"];

// `el` shim — rebound from ctx in render().
let el = (tag) => document.createElement(tag);

// ----------------------------------------------------------------- styles ----

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const css = `
.pf-page { display: flex; flex-direction: column; gap: var(--sp-4); }
.pf-editor { width: 100%; min-height: 64px; resize: vertical;
  font-family: var(--font-mono); font-size: 14px; line-height: 1.5;
  padding: var(--sp-3); border: 1px solid var(--border); border-radius: var(--radius-card);
  background: var(--gray-1); color: var(--text); }
.pf-editor:focus-visible { outline: none; box-shadow: var(--focus-ring); border-color: var(--blue); }
.pf-form { display: flex; flex-direction: column; gap: var(--sp-3); }
.pf-row { display: flex; align-items: flex-end; gap: var(--sp-3); flex-wrap: wrap; }
.pf-field { display: flex; flex-direction: column; gap: var(--sp-1); }
.pf-field > .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-muted); }
.pf-field .input, .pf-field .select { min-width: 0; }
.pf-num { width: 110px; font-family: var(--font-mono); }
.pf-fieldset { border: 1px solid var(--border); border-radius: var(--radius-card); padding: var(--sp-3); }
.pf-fieldset > legend { font-size: 12px; font-weight: 600; padding: 0 var(--sp-1); display: flex; align-items: center; gap: var(--sp-2); }
.pf-forced { font-size: 11px; color: var(--amber); font-weight: 600; }
.pf-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-3); }
@media (max-width: 900px) { .pf-kpis { grid-template-columns: repeat(2, 1fr); } }
.pf-kpi { display: flex; flex-direction: column; gap: 2px; padding: var(--sp-2) 0; }
.pf-kpi .label { font-size: 11px; color: var(--text-muted); }
.pf-kpi .val { font-family: var(--font-mono); font-size: 20px; font-weight: 600; }
.pf-kpi .val.pos { color: var(--green, #1E7B4B); }
.pf-kpi .val.neg { color: var(--red, #C0392B); }
.pf-grid2 { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: var(--sp-4); align-items: start; }
@media (max-width: 1100px) { .pf-grid2 { grid-template-columns: minmax(0, 1fr); } }
.pf-card-titlerow { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); width: 100%; }
.pf-tabs { display: inline-flex; gap: 2px; flex-wrap: wrap; }
.pf-tab { border: none; background: none; cursor: pointer; font-size: 13px; padding: 4px 10px;
  border-radius: var(--radius-badge); color: var(--text-muted); font-weight: 500; }
.pf-tab:hover { background: var(--gray-1); color: var(--text); }
.pf-tab.is-active { background: var(--blue, #2D5BE3); color: #fff; }
.pf-kv { display: grid; grid-template-columns: auto 1fr; gap: 4px var(--sp-3); font-size: 13px; }
.pf-kv dt { color: var(--text-muted); }
.pf-kv dd { margin: 0; font-family: var(--font-mono); }
.pf-json { font-family: var(--font-mono); font-size: 12px; white-space: pre-wrap; word-break: break-word;
  max-height: 360px; overflow: auto; background: var(--gray-1); padding: var(--sp-2); border-radius: var(--radius-badge); margin: var(--sp-2) 0 0; }
.pf-err { border: 1px solid #E8B5AE; background: #FCF4F3; border-radius: var(--radius-card); padding: var(--sp-3); }
.pf-err-title { font-weight: 600; color: var(--red); }
.pf-latency { font-family: var(--font-mono); font-size: 12px; }
.pf-hint { font-size: 12px; color: var(--text-muted); }
.pf-table-wrap { overflow: auto; max-height: 460px; border: 1px solid var(--border); border-radius: var(--radius-card); }
.pf-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.pf-table th, .pf-table td { padding: 4px 10px; text-align: left; white-space: nowrap; border-bottom: 1px solid var(--border); }
.pf-table thead th { position: sticky; top: 0; background: var(--bg); z-index: 1; font-weight: 600; color: var(--text-muted); }
.pf-table tbody tr:hover { background: var(--gray-1); }
.pf-mono { font-family: var(--font-mono); }
/* toast (shared visual with the factor page; defined here so it styles even when
   the factor page hasn't been visited) */
.factor-toast { position: fixed; bottom: var(--sp-6, 32px); left: 50%; transform: translateX(-50%);
  background: var(--navy, #1f2937); color: #fff; padding: var(--sp-2, 8px) var(--sp-4, 16px);
  border-radius: var(--radius-btn, 8px); font-size: 13px; z-index: 120; opacity: 0; transition: opacity .15s ease; }
.factor-toast.is-on { opacity: 1; }
.factor-toast--err { background: var(--red, #C0392B); }
`;
  document.head.appendChild(el("style", { id: STYLE_ID }, css));
}

// ----------------------------------------------------------------- helpers ----

function field(ctx, label, control) {
  return ctx.el("div", { className: "pf-field" }, ctx.el("span", { className: "label" }, label), control);
}

function numInput(ctx, value, step) {
  return ctx.el("input", { type: "number", className: "input pf-num", value: String(value), step: String(step), min: "0" });
}

function kpi(ctx, label, value, tone) {
  const v = ctx.el("span", { className: "val" + (tone ? " " + tone : "") }, value);
  return ctx.el("div", { className: "pf-kpi" }, ctx.el("span", { className: "label" }, label), v);
}

function lastHistoryExpr() {
  try {
    const arr = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(arr) && typeof arr[0] === "string" ? arr[0] : "";
  } catch (_) { return ""; }
}

// =================================================================== render ====

export function render(root, ctx) {
  el = ctx.el;
  injectStyle();

  const { api, store } = ctx;
  const cleanups = [];
  let controller = null;

  const snap = store.get();
  const globalUniverse = snap.universe;
  const globalPeriod = snap.period;

  // ---- expression editor ----
  const editor = ctx.el("textarea", {
    className: "pf-editor mono", spellcheck: "false", autocomplete: "off", rows: "2",
    "aria-label": ctx.t("pf.exprAria"), placeholder: ctx.t("pf.exprPlaceholder"),
  });
  // Prefill: a seed handed over from the Single Factor Test (sessionStorage) wins,
  // then the most recent factor history, then a sensible default.
  let seedExpr = "";
  try { seedExpr = sessionStorage.getItem("assay_portfolio_seed") || ""; sessionStorage.removeItem("assay_portfolio_seed"); } catch (_) {}
  editor.value = seedExpr || lastHistoryExpr() || DEFAULT_EXPR;

  // ---- core controls ----
  const uniSel = ctx.el("select", { className: "select", "aria-label": ctx.t("pf.universe") },
    ctx.el("option", { value: globalUniverse, selected: true }, globalUniverse));
  const startInput = ctx.el("input", { type: "date", className: "input input-date", value: globalPeriod[0] });
  const endInput = ctx.el("input", { type: "date", className: "input input-date", value: globalPeriod[1] });

  const rebalSel = ctx.el("select", { className: "select" },
    ...REBALANCE_TYPES.map((r) => ctx.el("option", { value: r, selected: r === "monthly" }, ctx.t("pf.rebal." + r))));
  const weightSel = ctx.el("select", { className: "select" },
    ...WEIGHT_METHODS.map((w) => ctx.el("option", { value: w, selected: w === "equal" }, ctx.t("pf.weight." + w))));
  const execSel = ctx.el("select", { className: "select" },
    ...EXECUTIONS.map((x) => ctx.el("option", { value: x }, x)));

  const longShortSel = ctx.el("select", { className: "select" },
    ctx.el("option", { value: "long", selected: true }, ctx.t("pf.longOnly")),
    ctx.el("option", { value: "ls" }, ctx.t("pf.longShortOpt")));

  const topNInput = ctx.el("input", { type: "number", className: "input pf-num", value: "1", min: "1", max: "3", step: "1" });
  const topNField = field(ctx, ctx.t("pf.topQuantiles"), topNInput);
  const maxWInput = numInput(ctx, 0.05, 0.01);
  // Initial capital — drives the absolute Portfolio Value chart & the operations
  // list amounts (the backtest itself is scale-free; this is a display multiplier).
  const capitalInput = ctx.el("input", { type: "number", className: "input pf-num", value: "1000000", min: "1", step: "100000",
    style: { width: "130px" }, "aria-label": ctx.t("pf.initialCapital") });

  // ---- A-share rules fieldset (forced when market === 'A') ----
  const aRulesChk = ctx.el("input", { type: "checkbox", checked: true });
  const aForcedTag = ctx.el("span", { className: "pf-forced" }, ctx.t("pf.forcedA"));
  const priceLimitSel = ctx.el("select", { className: "select" },
    ctx.el("option", { value: "0.10", selected: true }, "±10% " + ctx.t("pf.limitMain")),
    ctx.el("option", { value: "0.20" }, "±20% " + ctx.t("pf.limitStar")));
  const tPlus1Chk = ctx.el("input", { type: "checkbox", checked: true });
  const aFieldset = ctx.el("fieldset", { className: "pf-fieldset" },
    ctx.el("legend", {}, ctx.el("label", { className: "flex items-center", style: { gap: "6px" } }, aRulesChk, ctx.t("pf.aRules")), aForcedTag),
    ctx.el("div", { className: "pf-row" },
      field(ctx, ctx.t("pf.priceLimit"), priceLimitSel),
      field(ctx, ctx.t("pf.tPlus1"), ctx.el("label", { className: "flex items-center", style: { gap: "6px", height: "32px" } }, tPlus1Chk, ctx.t("pf.tPlus1Hint")))
    )
  );

  // ---- fees fieldset ----
  const feeCommission = numInput(ctx, FEE_PRESETS.A.commission_rate, 0.0001);
  const feeStamp = numInput(ctx, FEE_PRESETS.A.stamp_duty_rate, 0.0001);
  const feeTransfer = numInput(ctx, FEE_PRESETS.A.transfer_fee_rate, 0.00001);
  const feeMin = numInput(ctx, FEE_PRESETS.A.commission_min, 1);
  const feeSlippage = numInput(ctx, FEE_PRESETS.A.slippage_k, 0.05);
  const feeFieldset = ctx.el("fieldset", { className: "pf-fieldset" },
    ctx.el("legend", {}, ctx.t("pf.fees")),
    ctx.el("div", { className: "pf-row" },
      field(ctx, ctx.t("pf.commission"), feeCommission),
      field(ctx, ctx.t("pf.stampDuty"), feeStamp),
      field(ctx, ctx.t("pf.transferFee"), feeTransfer),
      field(ctx, ctx.t("pf.commissionMin"), feeMin),
      field(ctx, ctx.t("pf.slippageK"), feeSlippage)
    )
  );

  // ---- market-driven UI sync ----
  function currentMarket() { return marketForUniverse(uniSel.value); }
  function applyMarket(opts = {}) {
    const m = currentMarket();
    const isA = m === "A";
    // A-share is long-only and price-limit/T+1 forced.
    longShortSel.disabled = isA;
    if (isA) longShortSel.value = "long";
    aRulesChk.checked = isA;
    aRulesChk.disabled = isA; // forced on for A; off (and irrelevant) elsewhere
    aForcedTag.style.display = isA ? "" : "none";
    aFieldset.style.display = isA ? "" : "none";
    // refresh fee inputs to the market preset unless the user is mid-edit (initial only)
    if (opts.resetFees !== false) {
      const p = FEE_PRESETS[m] || FEE_PRESETS.US;
      feeCommission.value = String(p.commission_rate);
      feeStamp.value = String(p.stamp_duty_rate);
      feeTransfer.value = String(p.transfer_fee_rate);
      feeMin.value = String(p.commission_min);
      feeSlippage.value = String(p.slippage_k);
    }
  }
  uniSel.addEventListener("change", () => applyMarket());
  weightSel.addEventListener("change", () => {
    topNField.style.display = weightSel.value === "quintile" ? "" : "none";
  });

  // keep the universe option synced to the global selector (until user overrides)
  const unsub = store.subscribe((s) => {
    if (!uniSel.dataset.touched) {
      uniSel.replaceChildren(ctx.el("option", { value: s.universe, selected: true }, s.universe));
      applyMarket();
    }
  });
  uniSel.addEventListener("change", () => { uniSel.dataset.touched = "1"; });
  cleanups.push(unsub);

  // ---- run button + status + options ----
  const tradeLogChk = ctx.el("input", { type: "checkbox", checked: true });
  const tradeLogLabel = ctx.el("label", { className: "flex items-center", style: { gap: "6px", fontSize: "13px" } },
    tradeLogChk, ctx.t("pf.recordTrades"));
  const runStatus = ctx.el("span", { className: "pf-latency muted" }, "");
  const runBtn = ctx.el("button", { className: "btn btn--primary", type: "button", onClick: () => runBacktest() },
    ctx.t("pf.run") + " ▶");

  const form = ctx.el("div", { className: "pf-form" },
    ctx.el("div", { className: "pf-row" },
      field(ctx, ctx.t("pf.universe"), uniSel),
      field(ctx, ctx.t("pf.period"), ctx.el("span", { className: "flex items-center gap-1" }, startInput, ctx.el("span", { className: "muted" }, "–"), endInput)),
      field(ctx, ctx.t("pf.rebalance"), rebalSel),
      field(ctx, ctx.t("pf.weightMethod"), weightSel),
      topNField,
      field(ctx, ctx.t("pf.direction"), longShortSel),
      field(ctx, ctx.t("pf.execution"), execSel),
      field(ctx, ctx.t("pf.maxWeight"), maxWInput),
      field(ctx, ctx.t("pf.initialCapital"), capitalInput)
    ),
    aFieldset,
    feeFieldset,
    ctx.el("div", { className: "pf-row", style: { justifyContent: "flex-end", alignItems: "center" } },
      tradeLogLabel, ctx.el("span", { className: "grow", style: { flex: "1" } }), runStatus, runBtn)
  );

  // ---- results containers ----
  const resultsBody = ctx.el("div", {});

  const page = ctx.el("div", { className: "page pf-page" },
    ctx.el("div", { className: "page-header" },
      ctx.el("h1", { className: "page-title" }, ctx.t("pf.title")),
      ctx.el("span", { className: "page-subtitle" }, ctx.t("pf.subtitle"))
    ),
    ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-head" }, ctx.el("span", { className: "card-title" }, ctx.t("pf.expression"))),
      ctx.el("div", { className: "card-body" }, editor)
    ),
    ctx.el("section", { className: "card" }, ctx.el("div", { className: "card-body" }, form)),
    resultsBody
  );
  root.replaceChildren(page);

  // initial UI state
  applyMarket();
  topNField.style.display = "none";
  renderIdle();

  // optional prefill from a library factor id (#/portfolio/:id)
  const prefillId = ctx.params && ctx.params.id;
  if (prefillId) {
    api.libraryGet(prefillId).then((rep) => { if (rep && rep.expr) editor.value = rep.expr; }).catch(() => {});
  }

  // ---------------------------------------------------------------- run ----
  function buildRequest() {
    const market = currentMarket();
    const expr = editor.value.trim();
    const req = {
      expr,
      market,
      universe: uniSel.value,
      period_start: startInput.value,
      period_end: endInput.value,
      rebalance_type: rebalSel.value,
      weight_method: weightSel.value,
      long_short: market !== "A" && longShortSel.value === "ls",
      execution_price: execSel.value,
      max_single_weight: Number(maxWInput.value) || 0.05,
      commission_rate: Number(feeCommission.value),
      stamp_duty_rate: Number(feeStamp.value),
      transfer_fee_rate: Number(feeTransfer.value),
      commission_min: Number(feeMin.value),
      slippage_k: Number(feeSlippage.value),
      // operations list (操作清单): include the trade log when requested
      save_trade_log: tradeLogChk.checked,
      save_position_log: false,
    };
    if (weightSel.value === "quintile") req.quintile_long_n = Number(topNInput.value) || 1;
    if (market === "A") {
      req.enforce_limit_price = aRulesChk.checked;
      req.t_plus_1 = tPlus1Chk.checked;
      req.price_limit_pct = Number(priceLimitSel.value);
    }
    return req;
  }

  function setRunning(on) {
    runBtn.disabled = on;
    runStatus.className = "pf-latency muted";
    runStatus.textContent = on ? ctx.t("pf.running") : "";
  }

  async function runBacktest() {
    const expr = editor.value.trim();
    if (!expr) { toast(ctx, ctx.t("pf.enterExpr"), true); return; }
    if (startInput.value && endInput.value && startInput.value > endInput.value) {
      toast(ctx, ctx.t("pf.badPeriod"), true); return;
    }
    if (controller) controller.abort();
    controller = new AbortController();
    setRunning(true);
    renderLoading();
    const t0 = performance.now();
    try {
      const report = await api.portfolioBacktest(buildRequest(), { signal: controller.signal });
      const ms = Math.round(performance.now() - t0);
      runStatus.className = "pf-latency";
      runStatus.textContent = ctx.t("pf.ms", { ms });
      renderReport(report);
    } catch (err) {
      if (err && err.name === "AbortError") return;
      runStatus.className = "pf-latency neg";
      runStatus.textContent = ctx.t("pf.failed");
      renderError(err);
    } finally {
      setRunning(false);
      controller = null;
    }
  }

  // ---------------------------------------------------------------- render results ----
  function renderIdle() {
    resultsBody.replaceChildren(ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-body" },
        ctx.el("div", { className: "empty-state", style: { minHeight: "120px" } },
          ctx.el("div", { className: "muted" }, ctx.t("pf.idle"))))));
  }

  function renderLoading() {
    resultsBody.replaceChildren(ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-body" },
        ctx.el("div", { className: "skeleton skeleton-chart" }))));
  }

  function renderError(err) {
    const msg = err && err.message ? err.message : String(err);
    resultsBody.replaceChildren(ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-body" },
        ctx.el("div", { className: "pf-err" },
          ctx.el("div", { className: "pf-err-title" }, ctx.t("pf.failedTitle")),
          ctx.el("div", { style: { marginTop: "8px" } }, msg),
          err && err.envelope && err.envelope.code ? ctx.el("div", { className: "muted", style: { marginTop: "4px" } }, ctx.t("pf.codeLabel") + err.envelope.code) : null))));
  }

  function signTone(v) { return !Number.isFinite(v) ? "" : v > 0 ? "pos" : v < 0 ? "neg" : ""; }

  function renderReport(r) {
    if (!r) { renderIdle(); return; }

    // KPI grid
    const kpis = ctx.el("div", { className: "pf-kpis" },
      kpi(ctx, ctx.t("pf.kTotalReturn"), ctx.pct(r.total_return, 2), signTone(r.total_return)),
      kpi(ctx, ctx.t("pf.kAnnualReturn"), ctx.pct(r.annual_return, 2), signTone(r.annual_return)),
      kpi(ctx, ctx.t("pf.kSharpe"), ctx.fmt(r.sharpe, 2), signTone(r.sharpe)),
      kpi(ctx, ctx.t("pf.kMaxDD"), ctx.pct(r.max_drawdown != null ? -Math.abs(r.max_drawdown) : r.max_drawdown, 2), "neg"),
      kpi(ctx, ctx.t("pf.kSortino"), ctx.fmt(r.sortino, 2)),
      kpi(ctx, ctx.t("pf.kCalmar"), ctx.fmt(r.calmar, 2)),
      kpi(ctx, ctx.t("pf.kIR"), ctx.fmt(r.information_ratio, 2)),
      kpi(ctx, ctx.t("pf.kExcess"), ctx.pct(r.excess_return, 2), signTone(r.excess_return)),
      kpi(ctx, ctx.t("pf.kTurnover"), ctx.pct(r.annual_turnover, 0)),
      kpi(ctx, ctx.t("pf.kCostDrag"), ctx.pct(r.cost_drag, 2), "neg"),
      kpi(ctx, ctx.t("pf.kHoldDays"), ctx.fmt(r.avg_holding_days, 1)),
      kpi(ctx, ctx.t("pf.kBeta"), ctx.fmt(r.beta, 2))
    );
    const kpiCard = ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-head" }, ctx.el("span", { className: "card-title" }, ctx.t("pf.summary")),
        ctx.el("span", { className: "pf-hint" }, ctx.t("pf.summaryHint", { days: r.n_trading_days ?? "—", rebal: r.n_rebalances ?? "—" }))),
      ctx.el("div", { className: "card-body" }, kpis));

    const capital = Number(capitalInput.value) || 0;

    // Charts: one tabbed panel (Portfolio value / NAV vs benchmark / Daily / Monthly)
    const chartsCard = buildChartTabs(r, capital);

    const statsCard = ctx.el("section", { className: "card" },
      ctx.el("div", { className: "card-head" }, ctx.el("span", { className: "card-title" }, ctx.t("pf.execTitle"))),
      ctx.el("div", { className: "card-body" }, buildStats(r)));

    resultsBody.replaceChildren(
      kpiCard,
      ctx.el("div", { className: "pf-grid2" }, chartsCard, statsCard),
      buildTradeCard(r, capital),
      jsonDetails(r)
    );
  }

  // Tabbed chart panel — switches the rendered chart in-place; the ⤢ zoom button
  // clones whichever chart is currently active.
  function buildChartTabs(r, capital) {
    const tabs = [
      { key: "pv", label: ctx.t("pf.pvTitle"), render: (b) => renderPortfolioValue(b, r, capital) },
      { key: "nav", label: ctx.t("pf.navTitle"), render: (b) => renderNav(b, r) },
      { key: "daily", label: ctx.t("pf.dailyTitle"), render: (b) => renderDaily(b, r) },
      { key: "monthly", label: ctx.t("pf.monthlyTitle"), render: (b) => renderMonthly(b, r) },
    ];
    let active = tabs[0];
    const body = ctx.el("div", { className: "card-body" });
    const tabBtns = tabs.map((tb) => ctx.el("button", { className: "pf-tab", type: "button",
      onClick: () => { active = tb; select(); } }, tb.label));
    function select() {
      tabBtns.forEach((b, i) => b.classList.toggle("is-active", tabs[i] === active));
      body.replaceChildren();
      active.render(body);
    }
    let zb = null;
    if (ctx.zoomButton) {
      zb = ctx.zoomButton(ctx, () => body, () => active.label);
      zb.style.display = "none";
      new MutationObserver(() => { zb.style.display = body.querySelector("svg") ? "" : "none"; })
        .observe(body, { childList: true, subtree: true });
    }
    const head = ctx.el("div", { className: "card-head" },
      ctx.el("div", { className: "pf-card-titlerow" }, ctx.el("div", { className: "pf-tabs" }, ...tabBtns), zb));
    select();
    return ctx.el("section", { className: "card" }, head, body);
  }

  function renderDaily(body, r) {
    body.replaceChildren();
    const nav = r.nav_series || [];
    const dates = r.nav_dates || [];
    if (nav.length < 2) { body.appendChild(empty(ctx, ctx.t("pf.noDaily"))); return; }
    const rets = nav.map((v, i) => {
      if (i === 0 || !Number.isFinite(v) || !Number.isFinite(nav[i - 1]) || nav[i - 1] === 0) return NaN;
      return v / nav[i - 1] - 1;
    });
    const series = [{ name: ctx.t("pf.dailyReturn"), values: rets, color: "#2D5BE3" }];
    body.appendChild(wrap(ctx.charts.lineChart({ series, dates, height: 280, width: 900,
      interactive: true, valueFmt: (v) => ctx.pct(v, 2) })));
  }

  function renderPortfolioValue(body, r, capital) {
    body.replaceChildren();
    const nav = r.nav_series || [];
    const dates = r.nav_dates || [];
    if (!nav.some(Number.isFinite) || !(capital > 0)) { body.appendChild(empty(ctx, ctx.t("pf.noNav"))); return; }
    const values = nav.map((v) => (Number.isFinite(v) ? v * capital : NaN));
    const series = [{ name: ctx.t("pf.portfolioValue"), values, color: "#0E8A7E" }];
    const bench = r.benchmark_series || [];
    if (bench.some(Number.isFinite)) series.push({ name: ctx.t("pf.benchmark"), values: bench.map((v) => (Number.isFinite(v) ? v * capital : NaN)), color: "#8892AA" });
    const fin = values.filter(Number.isFinite);
    const peak = fin.length ? Math.max(...fin) : capital;
    const end = fin.length ? fin[fin.length - 1] : capital;
    body.appendChild(ctx.charts.legend(series));
    body.appendChild(wrap(ctx.charts.lineChart({ series, dates, height: 280, width: 900,
      yDomain: tightDomain(series.map((s) => s.values)), interactive: true, valueFmt: (v) => fmtMoney(v) })));
    body.appendChild(ctx.el("div", { className: "pf-hint", style: { marginTop: "4px" } },
      ctx.t("pf.pvHint", { end: fmtMoney(end), peak: fmtMoney(peak) })));
  }

  function buildTradeCard(r, capital) {
    const trades = r.trade_log || [];
    const body = ctx.el("div", { className: "card-body" });
    const head = ctx.el("div", { className: "card-head" },
      ctx.el("div", { className: "pf-card-titlerow" },
        ctx.el("span", { className: "card-title" }, ctx.t("pf.tradesTitle")),
        trades.length ? ctx.el("button", { className: "btn btn--sm", type: "button",
          onClick: () => downloadTradesCsv(trades) }, ctx.t("pf.downloadCsv")) : null));
    if (!trades.length) {
      body.appendChild(empty(ctx, ctx.t("pf.noTrades")));
      return ctx.el("section", { className: "card" }, head, body);
    }
    const CAP = 500; // cap DOM rows; full set still available via CSV
    const shown = trades.slice(-CAP).reverse(); // most recent first
    body.appendChild(ctx.el("div", { className: "pf-hint", style: { marginBottom: "6px" } },
      trades.length > CAP ? ctx.t("pf.tradesCapped", { shown: CAP, total: trades.length }) : ctx.t("pf.tradesCount", { total: trades.length })));
    body.appendChild(buildTradeTable(shown, capital));
    return ctx.el("section", { className: "card" }, head, body);
  }

  function buildTradeTable(rows, capital) {
    const t = ctx.t;
    const thead = ctx.el("thead", {}, ctx.el("tr", {},
      ...["pf.thDate", "pf.thSymbol", "pf.thSide", "pf.thWeight", "pf.thPrice", "pf.thAmount", "pf.thCost", "pf.thBlocked"].map((k) => ctx.el("th", {}, t(k)))));
    const tbody = ctx.el("tbody", {});
    for (const tr of rows) {
      const isBuy = tr.side === "buy";
      const w = Number(tr.exec_w);
      const amount = (capital > 0 && Number.isFinite(w)) ? Math.abs(w) * capital : null;
      tbody.appendChild(ctx.el("tr", {},
        ctx.el("td", {}, tr.date || "—"),
        ctx.el("td", { className: "pf-mono" }, tr.symbol || "—"),
        ctx.el("td", {}, ctx.el("span", { className: "badge " + (isBuy ? "badge--green" : "badge--red") }, isBuy ? t("pf.buy") : t("pf.sell"))),
        ctx.el("td", { className: "pf-mono" }, ctx.pct(w, 2)),
        ctx.el("td", { className: "pf-mono" }, ctx.fmt(tr.price, 2)),
        ctx.el("td", { className: "pf-mono" }, amount != null ? fmtMoney(amount) : "—"),
        ctx.el("td", { className: "pf-mono" }, (capital > 0 && Number.isFinite(tr.cost)) ? fmtMoney(tr.cost * capital) : ctx.fmt(tr.cost, 6)),
        ctx.el("td", {}, tr.blocked_reason ? ctx.el("span", { className: "badge badge--amber" }, tr.blocked_reason) : "—")));
    }
    return ctx.el("div", { className: "pf-table-wrap" }, ctx.el("table", { className: "pf-table" }, thead, tbody));
  }

  function renderNav(body, r) {
    body.replaceChildren();
    const nav = r.nav_series || [];
    const dates = r.nav_dates || [];
    if (!nav.some(Number.isFinite)) {
      body.appendChild(empty(ctx, ctx.t("pf.noNav")));
      return;
    }
    const series = [{ name: ctx.t("pf.strategy"), values: nav, color: "#2D5BE3" }];
    const bench = r.benchmark_series || [];
    if (bench.some(Number.isFinite)) series.push({ name: ctx.t("pf.benchmark"), values: bench, color: "#8892AA" });
    body.appendChild(ctx.charts.legend(series));
    body.appendChild(wrap(ctx.charts.lineChart({ series, dates, height: 280, width: 900,
      yDomain: tightDomain(series.map((s) => s.values)), interactive: true, valueFmt: (v) => ctx.fmt(v, 3) })));
  }

  function renderMonthly(body, r) {
    body.replaceChildren();
    const mr = r.monthly_returns || {};
    const keys = Object.keys(mr).sort();
    const values = keys.map((k) => Number(mr[k]));
    if (!keys.length || !values.some(Number.isFinite)) { body.appendChild(empty(ctx, ctx.t("pf.noMonthly"))); return; }
    const colors = values.map((v) => (v >= 0 ? "#1E7B4B" : "#C0392B"));
    const labels = keys.map((k) => k.slice(2)); // 'YYYY-MM' -> 'YY-MM'
    body.appendChild(wrap(ctx.charts.barChart({ labels, values, colors, dates: keys, height: 280, width: 900,
      interactive: true, valueFmt: (v) => ctx.pct(v, 2) })));
  }

  function buildStats(r) {
    const dl = ctx.el("dl", { className: "pf-kv" });
    const add = (k, v) => { dl.appendChild(ctx.el("dt", {}, k)); dl.appendChild(ctx.el("dd", {}, v)); };
    add(ctx.t("pf.gross"), ctx.pct(r.gross_return, 2));
    add(ctx.t("pf.alpha"), ctx.pct(r.alpha_capm, 2));
    add(ctx.t("pf.trackingErr"), ctx.pct(r.tracking_error, 2));
    add(ctx.t("pf.ddRecovery"), r.drawdown_recovery_days != null ? ctx.t("factor.daysShort", { days: r.drawdown_recovery_days }) : "—");
    const a = r.a_share_metrics || {};
    if (a && (a.n_limit_hits != null || a.limit_hit_rate != null)) {
      dl.appendChild(ctx.el("dt", { style: { gridColumn: "1 / -1", paddingTop: "6px", fontWeight: "600" } }, ctx.t("pf.aStatsTitle")));
      dl.appendChild(ctx.el("dd", {}, ""));
      add(ctx.t("pf.limitHitRate"), ctx.pct(a.limit_hit_rate, 2));
      add(ctx.t("pf.nLimitHits"), ctx.fmtInt(a.n_limit_hits));
      add(ctx.t("pf.nBlockedSuspended"), ctx.fmtInt(a.n_blocked_suspended));
      add(ctx.t("pf.forcedHoldRatio"), ctx.pct(a.forced_hold_ratio, 2));
    }
    return dl;
  }

  function jsonDetails(r) {
    const pre = ctx.el("pre", { className: "pf-json" }, safeJson(r));
    return ctx.el("details", { className: "card" },
      ctx.el("summary", { style: { cursor: "pointer", padding: "var(--sp-3)" }, className: "card-title" }, ctx.t("pf.fullJson")),
      ctx.el("div", { className: "card-body" }, pre));
  }

  // expose cleanup
  return () => { if (controller) controller.abort(); cleanups.forEach((fn) => { try { fn(); } catch (_) {} }); };
}

// ----------------------------------------------------------------- small utils ----

function wrap(node) {
  const w = el("div", { className: "chart-wrap" });
  if (node) w.appendChild(node);
  return w;
}
function empty(ctx, msg) {
  return ctx.el("div", { className: "empty-state", style: { minHeight: "200px" } }, ctx.el("div", { className: "muted" }, msg));
}
function safeJson(obj) { try { return JSON.stringify(obj, null, 2); } catch (_) { return String(obj); } }

// Tight y-domain (does NOT force-include zero) so NAV/value curves around 1.0 show
// their variation instead of being squashed against the top of a 0-based axis.
function tightDomain(arrays) {
  const vals = [];
  for (const a of arrays) for (const v of a || []) if (Number.isFinite(v)) vals.push(v);
  if (!vals.length) return null;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) { const p = Math.abs(lo) || 1; return [lo - p, hi + p]; }
  const pad = (hi - lo) * 0.08;
  return [lo - pad, hi + pad];
}

// Thousands-grouped integer money (currency-agnostic — A-share CNY / US USD).
function fmtMoney(v) {
  if (!Number.isFinite(v)) return "—";
  return Math.round(v).toLocaleString("en-US");
}

// Build a CSV from the full trade log and trigger a client-side download.
function downloadTradesCsv(trades) {
  const cols = ["date", "symbol", "side", "target_w", "exec_w", "price", "qty_frac", "cost", "blocked_reason"];
  const esc = (v) => {
    if (v === null || v === undefined) return "";
    const s = String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [cols.join(",")];
  for (const t of trades) lines.push(cols.map((c) => esc(t[c])).join(","));
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "portfolio_trades.csv";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 0);
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
