// app.js — bootstrap: TopNav wiring, router registration, page mounting.
//
// ============================================================================
//  PAGE MODULE CONTRACT
// ----------------------------------------------------------------------------
//  Every page module under js/pages/ exports:
//
//      export function render(root, ctx) { ... }
//
//  where:
//    root  : the <main id="app"> element. The page OWNS its content and should
//            replace it (root.replaceChildren(...)) on each render.
//    ctx   : a shared context object:
//      {
//        api,        // ApiClient instance (api.js) — all /v1 calls + evaluateStream
//        store,      // global store (state.js): get/set/subscribe; {universe, period}
//        router,     // hash router: route/navigate/current/start; params via ctx.params
//        charts,     // chart factories (charts.js): lineChart, barChart,
//                    //   calendarHeatmap, sparkline, diverging, seq, legend
//        el,         // dom.js el(tag, attrs, ...children)
//        clear, cx, fmt, pct, fmtInt, fmtSigned, // dom.js helpers
//        params,     // route params, e.g. {id} for '#/factor/:id' (may be {})
//        path,       // current hash path string
//      }
//
//  Pages may subscribe to the store for universe/period changes; they should
//  return an unsubscribe via cleanup if they add listeners (the app calls the
//  page's render fresh on each route change, so transient listeners are fine if
//  scoped to the page's own nodes).
// ============================================================================

import * as dom from "./dom.js";
import { el } from "./dom.js";
import { store, router } from "./state.js";
import { charts } from "./charts.js";
import { api, ApiError } from "./api.js";

import * as dashboard from "./pages/dashboard.js";
import * as library from "./pages/library.js";
import * as factor from "./pages/factor.js";

const appEl = document.getElementById("app");

// Shared context handed to every page render.
function makeCtx(extra = {}) {
  return {
    api,
    store,
    router,
    charts,
    el,
    clear: dom.clear,
    cx: dom.cx,
    fmt: dom.fmt,
    pct: dom.pct,
    fmtInt: dom.fmtInt,
    fmtSigned: dom.fmtSigned,
    ApiError,
    params: {},
    path: router.current(),
    ...extra,
  };
}

// ---------------------------------------------------------------- TopNav ----

function highlightTab(path) {
  const tabs = document.querySelectorAll(".topnav-tab");
  tabs.forEach((tab) => {
    const route = tab.getAttribute("data-route") || "";
    const base = "#" + path.split("/").slice(0, 2).join("/"); // '#/factor/x' -> '#/factor'
    tab.classList.toggle("is-active", route === base);
  });
}

const KNOWN_UNIVERSES = [
  { id: "NASDAQ100", label: "NASDAQ100", enabled: true },
  { id: "SP500", label: "SP500", enabled: false },
  { id: "RUSSELL2000", label: "Russell2000", enabled: false },
];

function populateUniverseSelect(universes) {
  const sel = document.getElementById("universe-select");
  if (!sel) return;
  // Merge live universes (from /v1/system/universes) with the known roadmap list.
  const liveIds = new Set((universes || []).map((u) => u.id));
  const opts = KNOWN_UNIVERSES.map((u) => ({
    ...u,
    enabled: u.enabled || liveIds.has(u.id),
  }));
  // include any extra live universes not in the known list
  for (const u of universes || []) {
    if (!opts.some((o) => o.id === u.id)) opts.push({ id: u.id, label: u.id, enabled: true });
  }
  const current = store.get("universe");
  sel.replaceChildren(
    ...opts.map((u) =>
      el("option", { value: u.id, disabled: !u.enabled, selected: u.id === current }, u.enabled ? u.label : `${u.label} (soon)`)
    )
  );
}

// Period presets compute [start, end] ending today (or at the configured end).
function presetPeriod(preset) {
  const end = new Date();
  const start = new Date(end);
  const years = { "1Y": 1, "3Y": 3, "5Y": 5 }[preset] || 5;
  start.setFullYear(start.getFullYear() - years);
  return [iso(start), iso(end)];
}
function iso(d) {
  return d.toISOString().slice(0, 10);
}

function syncPeriodControls() {
  const sel = document.getElementById("period-select");
  const custom = document.getElementById("period-custom");
  const startInput = document.getElementById("period-start");
  const endInput = document.getElementById("period-end");
  const [s, e] = store.get("period");
  if (startInput) startInput.value = s;
  if (endInput) endInput.value = e;
  // Best-effort: detect which preset matches; otherwise mark custom.
  let matched = "custom";
  for (const p of ["1Y", "3Y", "5Y"]) {
    const [ps, pe] = presetPeriod(p);
    if (ps === s && pe === e) {
      matched = p;
      break;
    }
  }
  if (sel) sel.value = matched;
  if (custom) custom.hidden = matched !== "custom";
}

function wireTopNav() {
  const universeSel = document.getElementById("universe-select");
  const periodSel = document.getElementById("period-select");
  const custom = document.getElementById("period-custom");
  const startInput = document.getElementById("period-start");
  const endInput = document.getElementById("period-end");
  const statusDot = document.getElementById("status-dot");

  if (universeSel) {
    universeSel.addEventListener("change", () => {
      store.set({ universe: universeSel.value });
    });
  }

  if (periodSel) {
    periodSel.addEventListener("change", () => {
      const val = periodSel.value;
      if (val === "custom") {
        if (custom) custom.hidden = false;
        return;
      }
      if (custom) custom.hidden = true;
      store.markPeriodUserSet();
      store.set({ period: presetPeriod(val) });
    });
  }

  const applyCustom = () => {
    if (!startInput || !endInput) return;
    const s = startInput.value;
    const e = endInput.value;
    if (s && e && s <= e) {
      store.markPeriodUserSet();
      store.set({ period: [s, e] });
    }
  };
  if (startInput) startInput.addEventListener("change", applyCustom);
  if (endInput) endInput.addEventListener("change", applyCustom);

  if (statusDot) {
    statusDot.addEventListener("click", () => router.navigate("#/dashboard"));
  }

  // Re-sync controls when the store changes from anywhere (e.g. URL/other tab).
  store.subscribe(() => {
    syncPeriodControls();
    const sel = document.getElementById("universe-select");
    if (sel && sel.value !== store.get("universe")) sel.value = store.get("universe");
  });
}

// ---------------------------------------------------------------- status ----

async function refreshStatus() {
  const dot = document.getElementById("status-dot");
  if (!dot) return;
  const setState = (cls, title) => {
    dot.className = "status-dot status-dot--" + cls;
    dot.title = "System status: " + title;
    dot.setAttribute("aria-label", "System status: " + title);
  };
  try {
    const status = await api.systemStatus();
    // Tolerate a missing/degraded payload. Heuristic on data freshness.
    const data = (status && status.data) || {};
    // Default the evaluation period to the ingested data range (unless the user
    // chose one) so out-of-the-box evaluation hits dates that actually have data.
    if (data.first_date && data.last_date) {
      store.applyDataDefaultPeriod(data.first_date, data.last_date);
    }
    const lastSync = data.last_sync || data.lastSync || null;
    if (status && (status.degraded || status.status === "degraded")) {
      setState("stale", "degraded" + (lastSync ? ` — last sync ${lastSync}` : ""));
      return;
    }
    if (lastSync) {
      const ageH = (Date.now() - Date.parse(lastSync)) / 3.6e6;
      if (Number.isFinite(ageH) && ageH > 24) {
        setState("stale", `stale — last sync ${lastSync}`);
        return;
      }
      setState("fresh", `data fresh — last sync ${lastSync}`);
      return;
    }
    setState("fresh", "online");
  } catch (err) {
    setState("error", "sync error / data unavailable");
  }
}

async function loadUniverses() {
  try {
    const universes = await api.universes();
    populateUniverseSelect(Array.isArray(universes) ? universes : []);
  } catch (_) {
    populateUniverseSelect([]); // degrade to roadmap list
  }
}

// ---------------------------------------------------------------- routing ----

function mount(pageModule, params, path) {
  highlightTab(path);
  const ctx = makeCtx({ params, path });
  try {
    pageModule.render(appEl, ctx);
  } catch (err) {
    console.error("page render failed", err);
    appEl.replaceChildren(
      el("div", { className: "error-state" },
        el("div", { className: "error-state-title" }, "This page failed to render."),
        el("div", { className: "muted" }, String(err && err.message ? err.message : err))
      )
    );
  }
}

function registerRoutes() {
  router
    .route("#/dashboard", ({ path }) => mount(dashboard, {}, path))
    .route("#/library", ({ path }) => mount(library, {}, path))
    .route("#/factor", ({ path }) => mount(factor, {}, path))
    .route("#/factor/:id", ({ params, path }) => mount(factor, params, path))
    .notFound(({ path }) => mount(dashboard, {}, path));
}

// ---------------------------------------------------------------- boot ----

function boot() {
  wireTopNav();
  registerRoutes();
  syncPeriodControls();
  // Fire-and-forget async chrome; never blocks first paint.
  loadUniverses();
  refreshStatus();
  // Refresh status when universe/period change (session/data context shifts).
  store.subscribe(() => refreshStatus());
  router.start("#/dashboard");
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
