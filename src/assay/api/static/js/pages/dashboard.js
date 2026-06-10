// pages/dashboard.js — Dashboard screen (assay_webui_design.md §3).
//
// Contract: export render(root, ctx) where
//   ctx = {api, store, router, charts, el, clear, cx, fmt, pct, fmtInt, fmtSigned, ApiError, params, path}.
//
// Sections (bound to REAL /v1 endpoints):
//   - StatusBar         GET /v1/system/status
//   - KPI row (4 cards) GET /v1/library/factors (sorted) + status
//   - Factor Leaderboard (left ~60%)  GET /v1/library/factors with filter controls
//   - Recent / Top factors (right ~40%)  — replaces the not-yet-wired live agent feed
//   - Data Calendar (full width)  GET /v1/system/data-calendar
//
// Graceful degradation: every fetch is guarded; empty / error states are explicit and
// the page never blank-crashes when NASDAQ-100 data is not ingested.

const STYLE_ID = "dashboard-page-style";
const POLL_MS = 30000;
const LEADERBOARD_LIMIT = 200;

// One owner for the polling interval across re-renders, so we never stack timers.
let pollTimer = null;
// Monotonic token: only the latest render owns the page / may paint async results.
let renderToken = 0;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
.dash { display: flex; flex-direction: column; gap: var(--sp-6); }
.dash-statusbar {
  display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2) var(--sp-4);
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border); border-radius: var(--radius-card);
  background: var(--gray-1); font-size: 13px;
}
.dash-statusbar .sb-dot {
  width: 10px; height: 10px; border-radius: 50%; background: var(--gray-4); flex: none;
}
.dash-statusbar .sb-dot--fresh { background: var(--green); }
.dash-statusbar .sb-dot--stale { background: var(--amber); }
.dash-statusbar .sb-dot--error { background: var(--red); }
.dash-statusbar .sb-sep { color: var(--border-strong); }
.dash-statusbar .sb-warn { color: var(--amber); font-weight: 500; }
.dash-statusbar .sb-strong { color: var(--text); font-weight: 500; }

.dash-split { display: grid; grid-template-columns: 6fr 4fr; gap: var(--sp-4); align-items: start; }

.dash-controls { display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-3); }
.dash-controls .ctrl { display: inline-flex; align-items: center; gap: var(--sp-2); }
.dash-controls .ctrl-label { font-size: 12px; color: var(--text-muted); }
.dash-toggle { display: inline-flex; align-items: center; gap: var(--sp-1); cursor: pointer; font-size: 13px; }

.dash-tablewrap { max-height: 560px; overflow-y: auto; }
.dash-table td.expr-cell { max-width: 300px; overflow: hidden; text-overflow: ellipsis; }
.dash-table tbody tr { cursor: pointer; }
.dash-table .icir-bar {
  position: relative; min-width: 88px;
}
.dash-table .icir-bar .bar-track {
  position: absolute; left: 0; right: 0; top: 50%; transform: translateY(-50%);
  height: 6px; background: var(--gray-1); border-radius: 3px; overflow: hidden;
}
.dash-table .icir-bar .bar-fill { position: absolute; left: 0; top: 0; bottom: 0; background: var(--blue); }
.dash-table .icir-bar .bar-val { position: relative; font-family: var(--font-mono); }

.dash-feed { display: flex; flex-direction: column; gap: var(--sp-2); }
.dash-feed-card {
  border: 1px solid var(--border); border-left: 3px solid var(--blue);
  border-radius: var(--radius-badge); padding: var(--sp-2) var(--sp-3);
  cursor: pointer; display: flex; flex-direction: column; gap: 2px;
}
.dash-feed-card:hover { background: var(--gray-1); }
.dash-feed-card.is-fail { border-left-color: var(--red); }
.dash-feed-top { display: flex; align-items: baseline; justify-content: space-between; gap: var(--sp-2); }
.dash-feed-expr { font-family: var(--font-mono); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dash-feed-ic { font-family: var(--font-mono); font-size: 13px; font-weight: 500; flex: none; }
.dash-feed-meta { font-size: 11px; color: var(--text-muted); }

.dash-kpi.clickable { cursor: pointer; }
.dash-kpi.clickable:hover { border-color: var(--border-strong); background: var(--gray-1); }
`;
  document.head.appendChild(style);
}

// ---------------------------------------------------------------- helpers ----

function num(v) {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function decayBadge(days, ctx) {
  const d = num(days);
  if (d === null) return ctx.el("span", { className: "muted" }, "—");
  const variant = d < 10 ? "green" : d <= 30 ? "amber" : "red";
  return ctx.el("span", { className: `badge badge--${variant}`, title: `Decay half-life ${d} days` }, `${d}d`);
}

function redundancyBadge(score, ctx) {
  const s = num(score);
  if (s === null) return ctx.el("span", { className: "muted" }, "—");
  let variant = "green";
  let label = "unique";
  if (s > 0.7) {
    variant = "red";
    label = "redundant";
  } else if (s >= 0.4) {
    variant = "amber";
    label = "similar";
  }
  return ctx.el(
    "span",
    { className: `badge badge--${variant}`, title: `Redundancy ${s.toFixed(2)}` },
    label
  );
}

function statusBadge(failureMode, ctx) {
  if (failureMode) {
    return ctx.el("span", { className: "badge badge--red", title: failureMode }, "! " + failureMode);
  }
  return ctx.el("span", { className: "badge badge--green", title: "Passed" }, "✓");
}

function sourceTag(source, ctx) {
  return ctx.el("span", { className: "tag" }, source || "—");
}

function relTime(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return String(iso);
  const sec = (Date.now() - t) / 1000;
  if (sec < 90) return "just now";
  const min = sec / 60;
  if (min < 90) return `${Math.round(min)} min ago`;
  const hr = min / 60;
  if (hr < 36) return `${Math.round(hr)} h ago`;
  return `${Math.round(hr / 24)} d ago`;
}

// ---------------------------------------------------------------- status bar ----

function buildStatusBar(status, ctx) {
  const { el } = ctx;
  const data = (status && status.data) || {};
  const lastSync = data.last_sync || null;
  const symbols = num(data.symbols_available);
  const tradingDays = num(data.trading_days_available);

  // Freshness heuristic identical in spirit to app.js refreshStatus().
  let dotCls = "fresh";
  let freshLabel = "Data online";
  if (status && (status.degraded || status.status === "degraded")) {
    dotCls = "stale";
    freshLabel = "Degraded";
  } else if (!symbols) {
    // No ingested symbols -> nothing to evaluate against.
    dotCls = "error";
    freshLabel = "No data ingested";
  } else if (lastSync) {
    const ageH = (Date.now() - Date.parse(lastSync)) / 3.6e6;
    if (Number.isFinite(ageH) && ageH > 24) {
      dotCls = "stale";
      freshLabel = "Data stale";
    }
  }

  const sep = () => el("span", { className: "sb-sep", "aria-hidden": "true" }, "·");
  const items = [];
  items.push(el("span", { className: `sb-dot sb-dot--${dotCls}` }));
  items.push(el("span", { className: "sb-strong" }, freshLabel));
  items.push(sep());
  items.push(
    el(
      "span",
      {},
      "Last sync: ",
      el("span", { className: "sb-strong" }, lastSync ? relTime(lastSync) : "never")
    )
  );
  items.push(sep());
  items.push(el("span", {}, symbols !== null ? `${ctx.fmtInt(symbols)} symbols` : "— symbols"));
  if (tradingDays) {
    items.push(sep());
    items.push(el("span", {}, `${ctx.fmtInt(tradingDays)} trading days`));
  }
  if (status && status.engine_version) {
    items.push(sep());
    items.push(el("span", { className: "muted" }, `engine v${status.engine_version}`));
  }

  // Warnings: surface data unavailability as an actionable warning.
  const warnings = [];
  if (!symbols) warnings.push("No ingested data — run prepare-nasdaq100");
  if (status && (status.degraded || status.status === "degraded")) warnings.push("Service degraded");
  items.push(sep());
  items.push(
    el(
      "span",
      { className: "sb-warn", title: warnings.join(" · ") || "No warnings" },
      warnings.length ? `⚠ ${warnings.length} warning${warnings.length > 1 ? "s" : ""}` : "✓ 0 warnings"
    )
  );

  return el("div", { className: "dash-statusbar", role: "status", "aria-live": "polite" }, ...items);
}

function buildStatusBarError(ctx) {
  const { el } = ctx;
  return el(
    "div",
    { className: "dash-statusbar", role: "status" },
    el("span", { className: "sb-dot sb-dot--error" }),
    el("span", { className: "sb-strong" }, "System status unavailable"),
    el("span", { className: "sb-sep" }, "·"),
    el("span", { className: "muted" }, "GET /v1/system/status failed")
  );
}

// ---------------------------------------------------------------- KPI row ----

function kpiCard(ctx, { label, value, sub, valueCls, onClick }) {
  const { el } = ctx;
  const attrs = { className: ctx.cx("metric-card", "dash-kpi", { clickable: !!onClick }) };
  if (onClick) {
    attrs.role = "button";
    attrs.tabindex = "0";
    attrs.onClick = onClick;
    attrs.onKeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onClick();
      }
    };
  }
  return el(
    "div",
    attrs,
    el("span", { className: "metric-label" }, label),
    el("span", { className: ctx.cx("metric-value", valueCls) }, value),
    el("span", { className: "metric-sub" }, sub || "")
  );
}

function buildKpiRow(ctx, { factors, status }) {
  const { el, fmt, fmtInt } = ctx;
  const valid = factors.filter((f) => num(f.rank_icir) !== null);

  // True library size from status (the list endpoint's `total` is page length).
  const totalFactors =
    status && num(status.library_factors) !== null ? num(status.library_factors) : factors.length;

  const icirs = valid.map((f) => num(f.rank_icir)).filter((v) => v !== null);
  const avgIcir = icirs.length ? icirs.reduce((a, b) => a + b, 0) / icirs.length : null;

  // Best by rank_icir (list is already sorted desc when sort_by=rank_icir).
  let best = null;
  for (const f of valid) {
    if (best === null || num(f.rank_icir) > num(best.rank_icir)) best = f;
  }

  const sessions = status ? num(status.active_sessions) : null;

  return el(
    "div",
    { className: "grid grid-4" },
    kpiCard(ctx, {
      label: "Total factors",
      value: fmtInt(totalFactors),
      sub: factors.length < totalFactors ? `showing top ${factors.length}` : "in library",
      onClick: () => ctx.router.navigate("#/library"),
    }),
    kpiCard(ctx, {
      label: "Avg RankICIR",
      value: fmt(avgIcir, 2),
      sub: icirs.length ? `across ${fmtInt(icirs.length)} scored` : "no scored factors",
      valueCls: avgIcir !== null ? (avgIcir >= 0 ? "metric-value--pos" : "metric-value--neg") : null,
    }),
    kpiCard(ctx, {
      label: "Best RankICIR",
      value: best ? fmt(num(best.rank_icir), 2) : "—",
      sub: best ? best.expr : "—",
      valueCls: best ? "metric-value--pos" : null,
      onClick: best ? () => ctx.router.navigate(`#/factor/${encodeURIComponent(best.factor_id)}`) : null,
    }),
    kpiCard(ctx, {
      label: "Active sessions",
      value: sessions !== null ? fmtInt(sessions) : "—",
      sub:
        status && num(status.data && status.data.trading_days_available) !== null
          ? `${fmtInt(status.data.trading_days_available)} data days`
          : "agent sessions live",
    })
  );
}

// ---------------------------------------------------------------- leaderboard ----

function applyFilters(factors, filters) {
  let out = factors.slice();
  const minIcir = num(filters.minIcir) || 0;
  if (minIcir > 0) out = out.filter((f) => (num(f.rank_icir) || -Infinity) >= minIcir);
  if (filters.source && filters.source !== "All") {
    out = out.filter((f) => (f.source || "").toUpperCase() === filters.source.toUpperCase());
  }
  if (filters.hideRedundant) out = out.filter((f) => (num(f.redundancy_score) || 0) <= 0.7);

  const key = filters.sortBy;
  const cmp = (a, b) => {
    if (key === "decay_halflife_days") {
      // smaller (faster decay) first; nulls last
      const av = num(a.decay_halflife_days);
      const bv = num(b.decay_halflife_days);
      if (av === null) return 1;
      if (bv === null) return -1;
      return av - bv;
    }
    const av = num(a[key]);
    const bv = num(b[key]);
    return (bv === null ? -Infinity : bv) - (av === null ? -Infinity : av);
  };
  out.sort(cmp);
  return out;
}

function icirBarCell(ctx, value, maxAbs) {
  const { el, fmt } = ctx;
  const v = num(value);
  const denom = maxAbs > 0 ? maxAbs : 1;
  const frac = v === null ? 0 : Math.max(0, Math.min(1, Math.abs(v) / denom));
  const color = v !== null && v < 0 ? "var(--red)" : "var(--blue)";
  return el(
    "div",
    { className: "icir-bar" },
    el(
      "span",
      { className: "bar-track", "aria-hidden": "true" },
      el("span", { className: "bar-fill", style: { width: (frac * 100).toFixed(1) + "%", background: color } })
    ),
    el("span", { className: "bar-val" }, fmt(v, 2))
  );
}

function buildLeaderboard(ctx, factors, filters, onFiltersChange) {
  const { el, fmt } = ctx;

  const sortSelect = el(
    "select",
    {
      className: "select",
      "aria-label": "Sort leaderboard",
      onChange: (e) => onFiltersChange({ sortBy: e.target.value }),
    },
    el("option", { value: "rank_icir", selected: filters.sortBy === "rank_icir" }, "RankICIR"),
    el("option", { value: "rank_ic", selected: filters.sortBy === "rank_ic" }, "RankIC"),
    el("option", { value: "ic", selected: filters.sortBy === "ic" }, "IC"),
    el("option", { value: "decay_halflife_days", selected: filters.sortBy === "decay_halflife_days" }, "Decay")
  );

  const minIcirInput = el("input", {
    className: "input",
    type: "number",
    step: "0.1",
    min: "0",
    max: "2",
    value: String(filters.minIcir),
    style: { width: "72px" },
    "aria-label": "Minimum RankICIR",
    onChange: (e) => onFiltersChange({ minIcir: Number(e.target.value) || 0 }),
  });

  const sources = ["All", ...Array.from(new Set(factors.map((f) => (f.source || "").toUpperCase()).filter(Boolean)))];
  const sourceSelect = el(
    "select",
    {
      className: "select",
      "aria-label": "Filter by source",
      onChange: (e) => onFiltersChange({ source: e.target.value }),
    },
    ...sources.map((s) => el("option", { value: s, selected: filters.source === s }, s))
  );

  const hideToggle = el(
    "label",
    { className: "dash-toggle" },
    el("input", {
      type: "checkbox",
      checked: filters.hideRedundant,
      "aria-label": "Hide redundant factors",
      onChange: (e) => onFiltersChange({ hideRedundant: e.target.checked }),
    }),
    "Hide redundant"
  );

  const controls = el(
    "div",
    { className: "dash-controls" },
    el("span", { className: "ctrl" }, el("span", { className: "ctrl-label" }, "Sort"), sortSelect),
    el("span", { className: "ctrl" }, el("span", { className: "ctrl-label" }, "Min ICIR"), minIcirInput),
    el("span", { className: "ctrl" }, el("span", { className: "ctrl-label" }, "Source"), sourceSelect),
    hideToggle
  );

  const filtered = applyFilters(factors, filters);
  const maxAbsIcir = filtered.reduce((m, f) => Math.max(m, Math.abs(num(f.rank_icir) || 0)), 0);

  let body;
  if (factors.length === 0) {
    body = el(
      "div",
      { className: "empty-state" },
      el("div", { className: "empty-state-title" }, "No factors yet"),
      el("div", {}, "Evaluate one in Single Factor Test to populate the leaderboard."),
      el("button", { className: "btn btn--primary btn--sm", onClick: () => ctx.router.navigate("#/factor") }, "Open Single Factor Test")
    );
  } else if (filtered.length === 0) {
    body = el(
      "div",
      { className: "empty-state" },
      el("div", { className: "empty-state-title" }, "No factors match these filters"),
      el("div", {}, "Loosen Min ICIR, source, or the redundancy toggle.")
    );
  } else {
    const head = el(
      "thead",
      {},
      el(
        "tr",
        {},
        el("th", {}, "Expression"),
        el("th", { className: "num" }, "RankIC"),
        el("th", { className: "num" }, "RankICIR"),
        el("th", {}, "Decay"),
        el("th", {}, "Redundancy"),
        el("th", {}, "Source"),
        el("th", {}, "Status")
      )
    );
    const rows = filtered.map((f) =>
      el(
        "tr",
        {
          tabindex: "0",
          onClick: () => ctx.router.navigate(`#/factor/${encodeURIComponent(f.factor_id)}`),
          onKeydown: (e) => {
            if (e.key === "Enter") ctx.router.navigate(`#/factor/${encodeURIComponent(f.factor_id)}`);
          },
        },
        el("td", { className: "mono expr-cell", title: f.expr }, f.expr),
        el("td", { className: "num" }, fmt(num(f.rank_ic), 3)),
        el("td", { className: "num icir-bar" }, icirBarCell(ctx, f.rank_icir, maxAbsIcir)),
        el("td", {}, decayBadge(f.decay_halflife_days, ctx)),
        el("td", {}, redundancyBadge(f.redundancy_score, ctx)),
        el("td", {}, sourceTag(f.source, ctx)),
        el("td", {}, statusBadge(f.failure_mode, ctx))
      )
    );
    body = el(
      "div",
      { className: "dash-tablewrap" },
      el("table", { className: "table dash-table" }, head, el("tbody", {}, ...rows))
    );
  }

  return el(
    "section",
    { className: "panel", style: { padding: "var(--sp-4)" } },
    el(
      "div",
      { className: "card-head" },
      el("h2", { className: "section-title" }, "Factor Leaderboard"),
      el("span", { className: "muted", style: { fontSize: "12px" } }, factors.length ? `${filtered.length} of ${factors.length}` : "")
    ),
    controls,
    el("div", { className: "mt-4 w-full" }, body)
  );
}

// ---------------------------------------------------------------- right column ----

function buildTopPanel(ctx, factors) {
  const { el, fmt } = ctx;

  const top = factors
    .filter((f) => num(f.rank_icir) !== null && !f.failure_mode)
    .slice()
    .sort((a, b) => num(b.rank_icir) - num(a.rank_icir))
    .slice(0, 6);

  let list;
  if (top.length === 0) {
    list = el(
      "div",
      { className: "empty-state", style: { padding: "var(--sp-8) var(--sp-4)" } },
      el("div", { className: "empty-state-title" }, "No factors to show"),
      el("div", {}, "Top factors appear here once the library has scored entries.")
    );
  } else {
    list = el(
      "div",
      { className: "dash-feed" },
      ...top.map((f) =>
        el(
          "div",
          {
            className: ctx.cx("dash-feed-card", { "is-fail": !!f.failure_mode }),
            tabindex: "0",
            onClick: () => ctx.router.navigate(`#/factor/${encodeURIComponent(f.factor_id)}`),
            onKeydown: (e) => {
              if (e.key === "Enter") ctx.router.navigate(`#/factor/${encodeURIComponent(f.factor_id)}`);
            },
          },
          el(
            "div",
            { className: "dash-feed-top" },
            el("span", { className: "dash-feed-expr", title: f.expr }, f.expr),
            el("span", { className: "dash-feed-ic" }, fmt(num(f.rank_icir), 2))
          ),
          el(
            "div",
            { className: "dash-feed-meta" },
            `RankIC ${fmt(num(f.rank_ic), 3)} · Decay ${num(f.decay_halflife_days) !== null ? num(f.decay_halflife_days) + "d" : "—"} · ${(f.source || "—")}`
          )
        )
      )
    );
  }

  return el(
    "section",
    { className: "card" },
    el(
      "div",
      { className: "card-head" },
      el("h2", { className: "section-title" }, "Top factors"),
      el("span", { className: "muted", style: { fontSize: "12px" } }, "by RankICIR")
    ),
    list,
    el(
      "p",
      { className: "placeholder-note mt-4" },
      "Live agent activity feed is not wired yet — no session-stream endpoint exists. Showing the library's highest-ICIR factors instead."
    )
  );
}

// ---------------------------------------------------------------- data calendar ----

function buildCalendar(ctx, calendar) {
  const { el } = ctx;
  const rows = Array.isArray(calendar) ? calendar : [];
  let body;
  if (rows.length === 0) {
    body = el(
      "div",
      { className: "empty-state" },
      el("div", { className: "empty-state-title" }, "No ingested data"),
      el("div", {}, "Run prepare-nasdaq100 to populate the data calendar.")
    );
  } else {
    const dates = rows.map((r) => r.date);
    // coverage_pct may be 0..1 or 0..100; normalize to a 0..1 signal for the heatmap.
    const values = rows.map((r) => {
      const c = num(r.coverage_pct);
      if (c === null) return NaN;
      return c > 1.5 ? c / 100 : c;
    });
    const chart = ctx.charts.calendarHeatmap({ dates, values, diverging: false, width: 1080, height: 160 });
    body = el("div", { className: "chart-wrap" }, chart);
  }

  return el(
    "section",
    { className: "card" },
    el(
      "div",
      { className: "card-head" },
      el("h2", { className: "section-title" }, "Data Calendar"),
      el("span", { className: "muted", style: { fontSize: "12px" } }, rows.length ? `${rows.length} trading days` : "")
    ),
    body
  );
}

// ---------------------------------------------------------------- render ----

function skeletonBlock(ctx, h) {
  return ctx.el("div", { className: "skeleton", style: { height: h, width: "100%", borderRadius: "var(--radius-card)" } });
}

export function render(root, ctx) {
  injectStyle();
  const { el } = ctx;
  const token = ++renderToken; // invalidates any in-flight async paint from a prior mount

  // Stable section containers so polling can repaint sub-sections in place.
  const statusSlot = el("div", {}, skeletonBlock(ctx, "44px"));
  const kpiSlot = el("div", {}, el("div", { className: "grid grid-4" }, skeletonBlock(ctx, "92px"), skeletonBlock(ctx, "92px"), skeletonBlock(ctx, "92px"), skeletonBlock(ctx, "92px")));
  const leaderSlot = el("div", { className: "grow" }, skeletonBlock(ctx, "320px"));
  const rightSlot = el("div", {}, skeletonBlock(ctx, "320px"));
  const calSlot = el("div", {}, skeletonBlock(ctx, "160px"));

  const page = el(
    "div",
    { className: "dash" },
    el(
      "div",
      { className: "page-header" },
      el("h1", { className: "page-title" }, "Dashboard"),
      el("span", { className: "page-subtitle" }, `Universe ${ctx.store.get("universe")} · ${ctx.store.get("period").join(" – ")}`)
    ),
    statusSlot,
    kpiSlot,
    el("div", { className: "dash-split" }, leaderSlot, rightSlot),
    calSlot
  );
  root.replaceChildren(page);

  // Leaderboard filter state, kept in the closure so re-filtering doesn't refetch.
  const filters = { sortBy: "rank_icir", minIcir: 0, source: "All", hideRedundant: false };
  let lastFactors = [];

  const paintLeaderboard = () => {
    leaderSlot.replaceChildren(
      buildLeaderboard(ctx, lastFactors, filters, (patch) => {
        Object.assign(filters, patch);
        paintLeaderboard();
      })
    );
  };

  async function loadAll() {
    if (token !== renderToken) return; // a newer render owns the page; bail.

    const universe = ctx.store.get("universe");

    const [statusRes, factorsRes, calRes] = await Promise.allSettled([
      ctx.api.systemStatus(),
      ctx.api.libraryList({ universe, sort_by: "rank_icir", limit: LEADERBOARD_LIMIT }),
      ctx.api.dataCalendar(undefined, undefined),
    ]);
    if (token !== renderToken) return;

    // ---- status bar ----
    if (statusRes.status === "fulfilled") {
      statusSlot.replaceChildren(buildStatusBar(statusRes.value, ctx));
    } else {
      statusSlot.replaceChildren(buildStatusBarError(ctx));
    }

    // ---- factors (KPI + leaderboard + right panel) ----
    let factors = [];
    let factorsFailed = false;
    if (factorsRes.status === "fulfilled") {
      factors = (factorsRes.value && factorsRes.value.factors) || [];
    } else {
      factorsFailed = true;
    }
    lastFactors = factors;

    const status = statusRes.status === "fulfilled" ? statusRes.value : null;
    kpiSlot.replaceChildren(buildKpiRow(ctx, { factors, status }));

    if (factorsFailed) {
      const err = factorsRes.reason;
      leaderSlot.replaceChildren(
        el(
          "section",
          { className: "panel", style: { padding: "var(--sp-4)" } },
          el("h2", { className: "section-title", style: { marginBottom: "var(--sp-3)" } }, "Factor Leaderboard"),
          el(
            "div",
            { className: "error-state" },
            el("div", { className: "error-state-title" }, "Could not load the factor library"),
            el("div", { className: "muted" }, err && err.message ? err.message : "GET /v1/library/factors failed")
          )
        )
      );
      rightSlot.replaceChildren(buildTopPanel(ctx, []));
    } else {
      paintLeaderboard();
      rightSlot.replaceChildren(buildTopPanel(ctx, factors));
    }

    // ---- data calendar ----
    if (calRes.status === "fulfilled") {
      calSlot.replaceChildren(buildCalendar(ctx, calRes.value));
    } else {
      calSlot.replaceChildren(
        el(
          "section",
          { className: "card" },
          el("h2", { className: "section-title", style: { marginBottom: "var(--sp-3)" } }, "Data Calendar"),
          el(
            "div",
            { className: "error-state" },
            el("div", { className: "error-state-title" }, "Data calendar unavailable"),
            el("div", { className: "muted" }, "GET /v1/system/data-calendar failed")
          )
        )
      );
    }
  }

  // Single shared poll timer; clear any prior one so re-renders don't stack intervals.
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  loadAll();
  pollTimer = setInterval(() => {
    // Stop polling once a different page has mounted.
    if (token !== renderToken) {
      clearInterval(pollTimer);
      pollTimer = null;
      return;
    }
    loadAll();
  }, POLL_MS);
}
