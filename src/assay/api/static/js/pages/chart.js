// pages/chart.js — TradingView-style candlestick explorer.
//
// Type a symbol (US e.g. AAPL, or A-share e.g. 000001.SZ), pick a timeframe
// (minute is shown but has no ingested data → day/week/month are live), pick an
// adjustment (none/forward/split), toggle a rich set of indicators (MA/BOLL on the
// price panel; VOL/MACD/RSI/KDJ/ATR/OBV as subpanels), and optionally overlay an
// alpha expression evaluated for that single symbol (POST /v1/market/factor-series).
//
// Data is fetched ONCE for a generous range; pan (drag) and zoom (wheel) are
// entirely client-side over the cached bars — no refetch — and every indicator is
// computed once per load (not per hover frame), so interaction stays smooth.
//
// The chart is one hand-rolled SVG with a price panel + stacked indicator
// subpanels sharing an x-axis, plus a synced crosshair + an OHLC/indicator legend.
//
// Contract: export render(root, ctx). Zero deps.

import * as ind from "../indicators.js";

const NS = "http://www.w3.org/2000/svg";
const STYLE_ID = "chart-page-style";
const UP = "#E0443E";   // 涨 — red (A-share convention)
const DOWN = "#1FA85A"; // 跌 — green
const MA_DEFS = [
  { n: 5, color: "#2D5BE3" }, { n: 10, color: "#B87C1A" },
  { n: 20, color: "#8E44AD" }, { n: 60, color: "#0E8A7E" },
];
const TIMEFRAMES = [
  { code: "1min", key: "tf1m" }, { code: "5min", key: "tf5m" }, { code: "15min", key: "tf15m" },
  { code: "1d", key: "tfD" }, { code: "1w", key: "tfW" }, { code: "1mo", key: "tfM" },
];
const ADJ = [
  { code: "none", key: "adjNone" }, { code: "total", key: "adjForward" }, { code: "split", key: "adjSplit" },
];
const SUBS = ["vol", "macd", "rsi", "kdj", "atr", "obv"]; // subpanel order
const MIN_BARS = 20;        // tightest zoom
const DEFAULT_VIEW = 180;   // initial visible bar count

function svgEl(tag, attrs = {}, ...kids) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) { if (v === null || v === undefined) continue; n.setAttribute(k, String(v)); }
  for (const c of kids) if (c != null && c !== false) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  return n;
}
const isN = (v) => typeof v === "number" && Number.isFinite(v);
function fmtNum(v) {
  if (!isN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (a >= 1e4) return (v / 1e3).toFixed(1) + "K";
  if (a >= 100) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  return v.toFixed(3);
}

// =================================================================== styles ====
function injectStyle(ctx) {
  if (document.getElementById(STYLE_ID)) return;
  const css = `
.chart-page { display: flex; flex-direction: column; gap: var(--sp-3); }
.chart-toolbar { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.chart-symbol { width: 150px; font-family: var(--font-mono); text-transform: uppercase; }
.chart-seg { display: inline-flex; border: 1px solid var(--border); border-radius: var(--radius-btn); overflow: hidden; }
.chart-seg button { border: none; background: var(--bg); cursor: pointer; font-size: 13px; padding: 5px 10px; color: var(--text-muted); }
.chart-seg button + button { border-left: 1px solid var(--border); }
.chart-seg button.is-active { background: var(--blue, #2D5BE3); color: #fff; }
.chart-indbar { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.chart-chip { border: 1px solid var(--border); background: var(--bg); cursor: pointer; font-size: 12px; padding: 3px 9px; border-radius: 999px; color: var(--text-muted); }
.chart-chip.is-on { background: var(--navy, #1f2937); color: #fff; border-color: var(--navy, #1f2937); }
.chart-alpha { display: flex; align-items: center; gap: var(--sp-2); flex: 1; min-width: 240px; }
.chart-alpha input { flex: 1; font-family: var(--font-mono); font-size: 13px; }
.chart-host { position: relative; width: 100%; }
.chart-host svg { width: 100%; height: auto; display: block; cursor: crosshair; touch-action: none; }
.chart-host.is-drag svg { cursor: grabbing; }
.chart-legend2 { position: absolute; top: 8px; left: 10px; font-size: 12px; pointer-events: none; background: rgba(255,255,255,0.82); border-radius: 6px; padding: 3px 8px; }
.chart-legend2 .seg { margin-right: 10px; font-family: var(--font-mono); white-space: nowrap; }
.chart-hint { position: absolute; bottom: 6px; right: 12px; font-size: 11px; color: var(--text-muted); pointer-events: none; }
.chart-msg { padding: 40px; text-align: center; color: var(--text-muted); }
.ck-cross { stroke: #8892AA; stroke-width: 1; stroke-dasharray: 4 3; pointer-events: none; }
.ck-tag { fill: var(--navy, #1f2937); } .ck-tag-txt { fill: #fff; font-size: 10px; font-family: var(--font-mono); }
.ck-grid { stroke: var(--border, #e5e7eb); stroke-width: 1; }
.ck-axis { fill: var(--text-muted, #8892AA); font-size: 10px; font-family: var(--font-mono); }
.ck-zero { stroke: #C9CFDB; stroke-width: 1; stroke-dasharray: 2 2; }
.ck-psep { stroke: var(--border, #e5e7eb); stroke-width: 1; }
`;
  document.head.appendChild(ctx.el("style", { id: STYLE_ID }, css));
}

// =================================================================== render ====
export function render(root, ctx) {
  injectStyle(ctx);
  const { api, store } = ctx;
  const snap = store.get();
  const end = snap.period[1];
  const start = yearsBefore(end, 3); // fetch a generous window; pan/zoom within it

  const state = {
    symbol: "", freq: "1d", adj: "none", start, end,
    overlays: new Set(["ma"]), subs: new Set(["vol", "macd"]),
    bars: null, available: true, loading: false,
    alpha: null, alphaExpr: "",
    view: null, // {count, end} — persists across indicator toggles; reset on data load
  };

  const symbolInput = ctx.el("input", { className: "input chart-symbol", placeholder: "AAPL / 000001.SZ", spellcheck: "false", autocomplete: "off", "aria-label": ctx.t("chart.symbol") });
  symbolInput.addEventListener("keydown", (e) => { if (e.key === "Enter") loadBars(); });

  const tfSeg = ctx.el("div", { className: "chart-seg" },
    ...TIMEFRAMES.map((tf) => ctx.el("button", { type: "button", "data-code": tf.code, onClick: () => { state.freq = tf.code; syncSegs(); loadBars(); } }, ctx.t("chart." + tf.key))));
  const adjSeg = ctx.el("div", { className: "chart-seg" },
    ...ADJ.map((a) => ctx.el("button", { type: "button", "data-code": a.code, onClick: () => { state.adj = a.code; syncSegs(); loadBars(); } }, ctx.t("chart." + a.key))));
  const startInput = ctx.el("input", { type: "date", className: "input input-date", value: start });
  const endInput = ctx.el("input", { type: "date", className: "input input-date", value: end });
  startInput.addEventListener("change", () => { state.start = startInput.value; loadBars(); });
  endInput.addEventListener("change", () => { state.end = endInput.value; loadBars(); });
  const loadBtn = ctx.el("button", { className: "btn btn--primary", type: "button", onClick: () => loadBars() }, ctx.t("chart.load"));

  const toolbar = ctx.el("div", { className: "chart-toolbar" }, symbolInput, tfSeg, adjSeg,
    ctx.el("span", { className: "flex items-center gap-1" }, startInput, ctx.el("span", { className: "muted" }, "–"), endInput), loadBtn);

  function chip(label, on, toggle) {
    const b = ctx.el("button", { className: "chart-chip" + (on ? " is-on" : ""), type: "button" }, label);
    b.addEventListener("click", () => { toggle(); b.classList.toggle("is-on"); rerender(); });
    return b;
  }
  const overlayChips = [
    chip("MA", state.overlays.has("ma"), () => toggleSet(state.overlays, "ma")),
    chip("BOLL", state.overlays.has("boll"), () => toggleSet(state.overlays, "boll")),
  ];
  const subChips = SUBS.map((k) => chip(ctx.t("chart.ind_" + k), state.subs.has(k), () => toggleSet(state.subs, k)));
  const alphaInput = ctx.el("input", { className: "input", placeholder: ctx.t("chart.alphaPlaceholder"), spellcheck: "false", autocomplete: "off", value: lastHistoryExpr() });
  const alphaBtn = ctx.el("button", { className: "btn btn--sm", type: "button", onClick: () => loadAlpha() }, ctx.t("chart.overlayAlpha"));
  const alphaClear = ctx.el("button", { className: "btn btn--sm", type: "button", onClick: () => { state.alpha = null; state.alphaExpr = ""; state.subs.delete("alpha"); rerender(); } }, ctx.t("chart.clear"));
  const indbar = ctx.el("div", { className: "chart-indbar" },
    ctx.el("span", { className: "muted", style: { fontSize: "12px" } }, ctx.t("chart.overlays")), ...overlayChips,
    ctx.el("span", { className: "muted", style: { fontSize: "12px", marginLeft: "8px" } }, ctx.t("chart.panels")), ...subChips,
    ctx.el("div", { className: "chart-alpha" }, alphaInput, alphaBtn, alphaClear));

  const host = ctx.el("div", { className: "chart-host" });
  const page = ctx.el("div", { className: "page chart-page" },
    ctx.el("div", { className: "page-header" }, ctx.el("h1", { className: "page-title" }, ctx.t("chart.title")), ctx.el("span", { className: "page-subtitle" }, ctx.t("chart.subtitle"))),
    ctx.el("section", { className: "card" }, ctx.el("div", { className: "card-body" }, toolbar, ctx.el("div", { style: { marginTop: "10px" } }, indbar))),
    ctx.el("section", { className: "card" }, ctx.el("div", { className: "card-body" }, host)));
  root.replaceChildren(page);

  function syncSegs() {
    tfSeg.querySelectorAll("button").forEach((b) => b.classList.toggle("is-active", b.getAttribute("data-code") === state.freq));
    adjSeg.querySelectorAll("button").forEach((b) => b.classList.toggle("is-active", b.getAttribute("data-code") === state.adj));
  }
  syncSegs();
  renderMessage(ctx.t("chart.enterSymbol"));

  let reqSeq = 0;
  async function loadBars() {
    const sym = symbolInput.value.trim().toUpperCase();
    if (!sym) { renderMessage(ctx.t("chart.enterSymbol")); return; }
    state.symbol = sym; state.start = startInput.value; state.end = endInput.value;
    const seq = ++reqSeq; state.loading = true; renderMessage(ctx.t("chart.loading"));
    try {
      const res = await api.marketBars({ symbol: sym, freq: state.freq, adj: state.adj, start: state.start, end: state.end });
      if (seq !== reqSeq) return;
      state.available = res.available !== false;
      state.bars = res.bars || [];
      state.view = null;       // reset zoom/pan for a fresh series
      state.alpha = null; state.subs.delete("alpha");
      state.loading = false; rerender();
      if (state.alphaExpr) loadAlpha();
    } catch (err) {
      if (seq !== reqSeq) return;
      state.loading = false; renderMessage((err && err.message) ? err.message : String(err));
    }
  }
  async function loadAlpha() {
    const expr = alphaInput.value.trim();
    if (!expr || !state.symbol || !state.bars || !state.bars.length) return;
    state.alphaExpr = expr;
    try {
      const res = await api.marketFactorSeries({ symbol: state.symbol, expr, start: state.start, end: state.end, adj: state.adj });
      state.alpha = { expr, byDate: new Map((res.dates || []).map((d, i) => [String(d), res.values[i]])) };
      state.subs.add("alpha"); rerender();
    } catch (err) { console.error(err); }
  }
  function renderMessage(msg) { host.replaceChildren(ctx.el("div", { className: "chart-msg" }, msg)); }
  function rerender() {
    if (state.loading) return;
    if (!state.bars) { renderMessage(ctx.t("chart.enterSymbol")); return; }
    if (!state.available) { renderMessage(ctx.t("chart.noIntraday")); return; }
    if (!state.bars.length) { renderMessage(ctx.t("chart.noData", { symbol: state.symbol })); return; }
    mountChart(ctx, host, state);
  }
  return () => { if (host._cleanup) host._cleanup(); };
}

// =================================================================== chart ====
// Precompute everything once, then draw only the visible [a,b] window. Pan/zoom
// mutate the window and redraw (no recompute, no refetch).
function mountChart(ctx, host, state) {
  if (host._cleanup) host._cleanup(); // tear down a prior mount's listeners (no leak on re-render)
  const bars = state.bars;
  const N = bars.length;
  const S = {
    dates: bars.map((b) => b.date),
    open: bars.map((b) => num(b.open)), high: bars.map((b) => num(b.high)),
    low: bars.map((b) => num(b.low)), close: bars.map((b) => num(b.close)),
    vol: bars.map((b) => num(b.volume)),
  };
  S.ma = {}; for (const d of MA_DEFS) S.ma[d.n] = ind.sma(S.close, d.n);
  S.boll = ind.boll(S.close, 20, 2);
  S.macd = ind.macd(S.close);
  S.rsi = ind.rsi(S.close, 14);
  S.kdj = ind.kdj(S.high, S.low, S.close);
  S.atr = ind.atr(S.high, S.low, S.close);
  S.obv = ind.obv(S.close, S.vol);
  S.alpha = (state.alpha && state.subs.has("alpha")) ? S.dates.map((d) => { const v = state.alpha.byDate.get(String(d)); return isN(v) ? v : NaN; }) : null;

  // view window (persisted across toggles)
  let count = state.view ? state.view.count : Math.min(DEFAULT_VIEW, N);
  let endIdx = state.view ? state.view.end : N - 1;
  count = clamp(count, Math.min(MIN_BARS, N), N);
  endIdx = clamp(endIdx, count - 1, N - 1);

  host.replaceChildren();
  const legend = document.createElement("div"); legend.className = "chart-legend2"; host.appendChild(legend);
  const hint = ctx.el("div", { className: "chart-hint" }, ctx.t("chart.panHint")); host.appendChild(hint);

  const subs = [...SUBS.filter((k) => state.subs.has(k))];
  if (S.alpha) subs.push("alpha");

  // geometry
  const W = 1180, padL = 56, padR = 58, padTop = 8, padBottom = 22;
  const mainH = 330, subPH = 104, gap = 10;
  const innerW = W - padL - padR;
  const totalH = padTop + mainH + subs.reduce((s) => s + gap + subPH, 0) + padBottom;

  let curSvg = null;
  let panels = [];
  let band = 1;
  let A = 0, B = 0;

  function draw() {
    A = endIdx - count + 1; B = endIdx;
    band = innerW / Math.max(1, count);
    const cw = Math.max(1, Math.min(16, band * 0.62));
    const cx = (li) => padL + (li + 0.5) * band;
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${totalH}`, class: "chart-kline", preserveAspectRatio: "xMidYMid meet", role: "img" });
    panels = [];

    // ---- price panel ----
    const overlays = [];
    if (state.overlays.has("ma")) for (const d of MA_DEFS) overlays.push({ values: S.ma[d.n], color: d.color });
    if (state.overlays.has("boll")) { overlays.push({ values: S.boll.upper, color: "#9AA4B8" }); overlays.push({ values: S.boll.mid, color: "#5B6678" }); overlays.push({ values: S.boll.lower, color: "#9AA4B8" }); }
    let lo = Infinity, hi = -Infinity;
    for (let i = A; i <= B; i++) { if (isN(S.low[i])) lo = Math.min(lo, S.low[i]); if (isN(S.high[i])) hi = Math.max(hi, S.high[i]); for (const o of overlays) { const v = o.values[i]; if (isN(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } } }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    const priceY = makeScale(lo, hi, padTop, mainH, 0.05);
    drawGrid(svg, padL, W - padR, lo, hi, priceY, fmtNum);
    for (let i = A; i <= B; i++) {
      if (!isN(S.open[i]) || !isN(S.close[i])) continue;
      const up = S.close[i] >= S.open[i], col = up ? UP : DOWN, x = cx(i - A);
      if (isN(S.high[i]) && isN(S.low[i])) svg.appendChild(svgEl("line", { x1: x, x2: x, y1: priceY(S.high[i]), y2: priceY(S.low[i]), stroke: col, "stroke-width": 1 }));
      const yo = priceY(S.open[i]), yc = priceY(S.close[i]);
      svg.appendChild(svgEl("rect", { x: x - cw / 2, y: Math.min(yo, yc), width: cw, height: Math.max(1, Math.abs(yo - yc)), fill: col }));
    }
    for (const o of overlays) svg.appendChild(linePath(o.values, A, B, cx, priceY, o.color));
    panels.push({ key: "price", top: padTop, h: mainH, y: priceY, fmt: fmtNum });

    // ---- subpanels ----
    let top = padTop + mainH;
    for (const key of subs) {
      top += gap;
      svg.appendChild(svgEl("line", { x1: padL, x2: W - padR, y1: top, y2: top, class: "ck-psep" }));
      const p = drawSub(svg, ctx, key, S, A, B, cx, cw, { top, h: subPH, padL, padR, W });
      panels.push({ key, top, h: subPH, y: p.y, fmt: p.fmt });
      top += subPH;
    }

    // ---- x axis ----
    const xstep = Math.max(1, Math.ceil(count / 9));
    for (let i = A; i <= B; i += xstep) svg.appendChild(svgEl("text", { x: cx(i - A), y: totalH - 7, "text-anchor": "middle", class: "ck-axis" }, shortDate(S.dates[i])));

    // ---- crosshair ----
    const vline = svgEl("line", { class: "ck-cross", y1: padTop, y2: top, opacity: 0 });
    const hline = svgEl("line", { class: "ck-cross", x1: padL, x2: W - padR, opacity: 0 });
    const tagG = svgEl("g", { opacity: 0 }, svgEl("rect", { class: "ck-tag", width: 52, height: 14, rx: 2 }), svgEl("text", { class: "ck-tag-txt", x: 26, y: 10, "text-anchor": "middle" }));
    svg.appendChild(vline); svg.appendChild(hline); svg.appendChild(tagG);
    svg._cross = { vline, hline, tagG, cx };

    if (curSvg) curSvg.replaceWith(svg); else host.insertBefore(svg, legend);
    curSvg = svg;
    updateLegend(B);
  }

  function updateLegend(gi) {
    legend.replaceChildren();
    const seg = (txt, color) => { const s = document.createElement("span"); s.className = "seg"; if (color) s.style.color = color; s.textContent = txt; legend.appendChild(s); };
    seg(S.dates[gi]);
    seg(`O ${fmtNum(S.open[gi])}  H ${fmtNum(S.high[gi])}  L ${fmtNum(S.low[gi])}  C ${fmtNum(S.close[gi])}`, S.close[gi] >= S.open[gi] ? UP : DOWN);
    if (state.overlays.has("ma")) for (const d of MA_DEFS) { const v = S.ma[d.n][gi]; if (isN(v)) seg(`MA${d.n} ${fmtNum(v)}`, d.color); }
    for (const key of subs) {
      if (key === "vol") seg(`VOL ${fmtNum(S.vol[gi])}`);
      else if (key === "macd") seg(`DIF ${fmtNum(S.macd.dif[gi])} DEA ${fmtNum(S.macd.dea[gi])}`, "#2D5BE3");
      else if (key === "rsi") seg(`RSI ${fmtNum(S.rsi[gi])}`, "#8E44AD");
      else if (key === "kdj") seg(`K ${fmtNum(S.kdj.k[gi])} D ${fmtNum(S.kdj.d[gi])} J ${fmtNum(S.kdj.j[gi])}`, "#2D5BE3");
      else if (key === "atr") seg(`ATR ${fmtNum(S.atr[gi])}`, "#0E8A7E");
      else if (key === "obv") seg(`OBV ${fmtNum(S.obv[gi])}`, "#2D5BE3");
      else if (key === "alpha" && S.alpha) seg(`α ${fmtNum(S.alpha[gi])}`, "#C0392B");
    }
  }

  // ---- interactions (pan/zoom), all client-side ----
  function userXToLocal(e) {
    const ctm = curSvg.getScreenCTM(); if (!ctm) return null;
    const pt = curSvg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
    return pt.matrixTransform(ctm.inverse());
  }
  function onMove(e) {
    if (dragging) return; // panning handled in onDragMove
    const u = userXToLocal(e); if (!u) return;
    let li = Math.floor((u.x - padL) / (band || 1)); li = clamp(li, 0, count - 1);
    const gi = A + li, x = (li + 0.5) * band + padL;
    const c = curSvg._cross;
    c.vline.setAttribute("x1", x); c.vline.setAttribute("x2", x); c.vline.setAttribute("opacity", 1);
    const panel = panels.find((p) => u.y >= p.top && u.y <= p.top + p.h);
    if (panel) {
      const yy = clamp(u.y, panel.top, panel.top + panel.h);
      c.hline.setAttribute("y1", yy); c.hline.setAttribute("y2", yy); c.hline.setAttribute("opacity", 1);
      c.tagG.setAttribute("opacity", 1); c.tagG.setAttribute("transform", `translate(${W - padR + 3}, ${yy - 7})`);
      c.tagG.querySelector("text").textContent = panel.fmt(invScale(panel.y, yy));
    }
    updateLegend(gi);
  }
  function onLeave() { if (!curSvg) return; const c = curSvg._cross; c.vline.setAttribute("opacity", 0); c.hline.setAttribute("opacity", 0); c.tagG.setAttribute("opacity", 0); updateLegend(B); }

  let dragging = false, dragStartLi = 0, dragStartEnd = 0, raf = 0;
  function onDown(e) { const u = userXToLocal(e); if (!u) return; dragging = true; dragStartLi = (u.x - padL) / (band || 1); dragStartEnd = endIdx; host.classList.add("is-drag"); e.preventDefault(); }
  function onDragMove(e) {
    if (!dragging) return;
    const u = userXToLocal(e); if (!u) return;
    const curLi = (u.x - padL) / (band || 1);
    const deltaBars = Math.round(curLi - dragStartLi);
    const newEnd = clamp(dragStartEnd - deltaBars, count - 1, N - 1);
    if (newEnd !== endIdx) { endIdx = newEnd; state.view = { count, end: endIdx }; if (!raf) raf = requestAnimationFrame(() => { raf = 0; draw(); }); }
  }
  function onUp() { if (dragging) { dragging = false; host.classList.remove("is-drag"); } }
  function onWheel(e) {
    e.preventDefault();
    const u = userXToLocal(e); if (!u) return;
    const li = clamp(Math.floor((u.x - padL) / (band || 1)), 0, count - 1);
    const anchor = A + li;                       // global bar under cursor stays put
    const frac = count > 1 ? li / (count - 1) : 0;
    const factor = e.deltaY > 0 ? 1.2 : 1 / 1.2; // wheel down = zoom out
    let newCount = clamp(Math.round(count * factor), Math.min(MIN_BARS, N), N);
    if (newCount === count) return;
    let newA = Math.round(anchor - frac * (newCount - 1));
    newA = clamp(newA, 0, Math.max(0, N - newCount));
    count = newCount; endIdx = clamp(newA + newCount - 1, newCount - 1, N - 1);
    state.view = { count, end: endIdx };
    draw();
  }

  host.addEventListener("mousemove", onMove);
  host.addEventListener("mouseleave", onLeave);
  host.addEventListener("mousedown", onDown);
  window.addEventListener("mousemove", onDragMove);
  window.addEventListener("mouseup", onUp);
  host.addEventListener("wheel", onWheel, { passive: false });
  host._cleanup = () => {
    host.removeEventListener("mousemove", onMove);
    host.removeEventListener("mouseleave", onLeave);
    host.removeEventListener("mousedown", onDown);
    window.removeEventListener("mousemove", onDragMove);
    window.removeEventListener("mouseup", onUp);
    host.removeEventListener("wheel", onWheel);
    if (raf) cancelAnimationFrame(raf);
  };

  draw();
}

// ---- subpanel drawing (uses precomputed S, slices [A,B]) ----
function drawSub(svg, ctx, key, S, A, B, cx, cw, G) {
  const { top, h, padL, padR, W } = G;
  const n = B - A + 1;
  const label = ctx.t("chart.ind_" + key);
  let y, fmt = fmtNum, lo = 0, hi = 1;
  const winFin = (arr) => { const o = []; for (let i = A; i <= B; i++) if (isN(arr[i])) o.push(arr[i]); return o; };
  const line = (arr, color, sw = 1.2) => svg.appendChild(linePath(arr, A, B, cx, y, color, sw));
  const zero = () => { if (lo < 0 && hi > 0) svg.appendChild(svgEl("line", { x1: padL, x2: W - padR, y1: y(0), y2: y(0), class: "ck-zero" })); };

  if (key === "vol") {
    hi = Math.max(1, ...winFin(S.vol)); y = makeScale(0, hi, top, h, 0.05);
    for (let i = A; i <= B; i++) { if (!isN(S.vol[i])) continue; const up = S.close[i] >= S.open[i]; svg.appendChild(svgEl("rect", { x: cx(i - A) - cw / 2, y: y(S.vol[i]), width: cw, height: Math.max(0, top + h - y(S.vol[i])), fill: up ? UP : DOWN, opacity: 0.85 })); }
  } else if (key === "macd") {
    const ext = Math.max(1e-9, ...winFin(S.macd.dif).map(Math.abs), ...winFin(S.macd.dea).map(Math.abs), ...winFin(S.macd.hist).map(Math.abs)); lo = -ext; hi = ext;
    y = makeScale(lo, hi, top, h, 0.06); zero();
    for (let i = A; i <= B; i++) { const v = S.macd.hist[i]; if (!isN(v)) continue; const h0 = y(0), hy = y(v); svg.appendChild(svgEl("rect", { x: cx(i - A) - cw / 2, y: Math.min(h0, hy), width: cw, height: Math.max(1, Math.abs(hy - h0)), fill: v >= 0 ? UP : DOWN, opacity: 0.8 })); }
    line(S.macd.dif, "#2D5BE3"); line(S.macd.dea, "#B87C1A");
  } else if (key === "rsi") {
    lo = 0; hi = 100; y = makeScale(0, 100, top, h, 0);
    for (const g of [30, 50, 70]) svg.appendChild(svgEl("line", { x1: padL, x2: W - padR, y1: y(g), y2: y(g), class: "ck-zero" }));
    line(S.rsi, "#8E44AD");
  } else if (key === "kdj") {
    const all = [...winFin(S.kdj.k), ...winFin(S.kdj.d), ...winFin(S.kdj.j)]; lo = Math.min(0, ...all); hi = Math.max(100, ...all);
    y = makeScale(lo, hi, top, h, 0.04); line(S.kdj.k, "#2D5BE3"); line(S.kdj.d, "#B87C1A"); line(S.kdj.j, "#8E44AD");
  } else if (key === "atr") {
    hi = Math.max(1e-9, ...winFin(S.atr)); y = makeScale(0, hi, top, h, 0.06); line(S.atr, "#0E8A7E");
  } else if (key === "obv") {
    const f = winFin(S.obv); lo = f.length ? Math.min(...f) : 0; hi = f.length ? Math.max(...f) : 1; y = makeScale(lo, hi, top, h, 0.06); line(S.obv, "#2D5BE3");
  } else if (key === "alpha") {
    const f = winFin(S.alpha); lo = f.length ? Math.min(...f) : -1; hi = f.length ? Math.max(...f) : 1; y = makeScale(lo, hi, top, h, 0.08); zero(); line(S.alpha, "#C0392B", 1.4);
  }
  svg.appendChild(svgEl("text", { x: padL + 2, y: top + 11, class: "ck-axis", "text-anchor": "start" }, label));
  svg.appendChild(svgEl("text", { x: padL - 4, y: top + 9, class: "ck-axis", "text-anchor": "end" }, fmt(hi)));
  svg.appendChild(svgEl("text", { x: padL - 4, y: top + h - 2, class: "ck-axis", "text-anchor": "end" }, fmt(lo)));
  return { y, fmt };
}

// =================================================================== helpers ====
function makeScale(lo, hi, top, h, padFrac) {
  if (!isN(lo) || !isN(hi) || lo === hi) { const p = Math.abs(lo) || 1; lo = (lo || 0) - p; hi = (hi || 0) + p; }
  const pad = (hi - lo) * (padFrac || 0); const L = lo - pad, H = hi + pad;
  const y = (v) => top + h - ((v - L) / (H - L || 1)) * h;
  y._lo = L; y._hi = H; y._top = top; y._h = h; return y;
}
function invScale(y, py) { return y._lo + (1 - (py - y._top) / (y._h || 1)) * (y._hi - y._lo); }
function drawGrid(svg, x0, x1, lo, hi, y, fmt) {
  for (let t = 0; t <= 4; t++) { const v = lo + ((hi - lo) * t) / 4, yy = y(v);
    svg.appendChild(svgEl("line", { x1: x0, x2: x1, y1: yy, y2: yy, class: "ck-grid" }));
    svg.appendChild(svgEl("text", { x: x0 - 4, y: yy + 3, "text-anchor": "end", class: "ck-axis" }, fmt(v))); }
}
function linePath(values, A, B, cx, y, color, sw = 1.2) {
  let d = "", pen = false;
  for (let i = A; i <= B; i++) { const v = values[i]; if (!isN(v)) { pen = false; continue; } d += (pen ? " L" : " M") + ` ${cx(i - A).toFixed(1)} ${y(v).toFixed(1)}`; pen = true; }
  return svgEl("path", { d: d.trim(), fill: "none", stroke: color, "stroke-width": sw });
}
function num(v) { return v == null ? NaN : Number(v); }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function shortDate(d) { return typeof d === "string" ? (d.length > 7 ? d.slice(2) : d) : String(d); }
function toggleSet(s, k) { if (s.has(k)) s.delete(k); else s.add(k); }
function yearsBefore(iso, yrs) { if (typeof iso !== "string" || iso.length < 10) return iso; const y = Number(iso.slice(0, 4)) - yrs; return String(y) + iso.slice(4); }
function lastHistoryExpr() { try { const a = JSON.parse(localStorage.getItem("assay_expr_history") || "[]"); return Array.isArray(a) && typeof a[0] === "string" ? a[0] : ""; } catch (_) { return ""; } }
