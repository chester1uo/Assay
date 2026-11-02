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
import { openLightbox, makeZoomButton } from "./lightbox.js";
import { t, getLang, toggleLang, onLang } from "./i18n.js";

import * as dashboard from "./pages/dashboard.js";
import * as library from "./pages/library.js";
import * as factor from "./pages/factor.js";
import * as combination from "./pages/combination.js";
import * as portfolio from "./pages/portfolio.js";
import * as chart from "./pages/chart.js";
import * as dataManager from "./pages/data.js";

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
    lightbox: openLightbox,
    zoomButton: makeZoomButton,
    ApiError,
    t,
    lang: getLang(),
    params: {},
    path: router.current(),
    ...extra,
  };
}

// --------------------------------------------------------------- i18n chrome ----

// Localize the static shell (sidebar, context bar, language button, status)
// from data-i18n* attributes. Called on boot and on every language switch.
function localizeChrome() {
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    node.title = t(node.getAttribute("data-i18n-title"));
  });
  document.querySelectorAll("[data-i18n-aria]").forEach((node) => {
    node.setAttribute("aria-label", t(node.getAttribute("data-i18n-aria")));
  });
  highlightTab(router.current() || "/dashboard");
}

// --------------------------------------------------------------- sidebar ----

// Marks the sidebar link of the current page and names the page in the context bar
// and the browser tab.
function highlightTab(path) {
  const base = "#" + path.split("/").slice(0, 2).join("/"); // '#/factor/x' -> '#/factor'
  let label = "";
  document.querySelectorAll(".sidenav-link[data-route]").forEach((link) => {
    const on = link.getAttribute("data-route") === base;
    link.classList.toggle("is-active", on);
    if (on) {
      link.setAttribute("aria-current", "page");
      label = link.textContent.trim();
    } else {
      link.removeAttribute("aria-current");
    }
  });
  const title = document.getElementById("page-title");
  if (title) title.textContent = label;
  document.title = label ? `${label} · Assay` : "Assay · QFinVerse";
  // Folded, the label is only in the tooltip.
  document.querySelectorAll(".sidenav-link[data-route]").forEach((link) => {
    link.title = document.documentElement.classList.contains("nav-folded") ? link.textContent.trim() : "";
  });
}

const FOLD_KEY = "assay-sidebar-folded";

function wireSidebar() {
  const btn = document.getElementById("nav-fold");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const folded = !document.documentElement.classList.contains("nav-folded");
    document.documentElement.classList.toggle("nav-folded", folded);
    try { localStorage.setItem(FOLD_KEY, folded ? "1" : "0"); } catch (_) {}
    btn.title = t(folded ? "ctrl.expand" : "ctrl.collapse");
    highlightTab(router.current() || "/dashboard");
    window.dispatchEvent(new Event("resize"));   // charts re-measure their width
  });
  btn.title = t(document.documentElement.classList.contains("nav-folded") ? "ctrl.expand" : "ctrl.collapse");
}

const KNOWN_UNIVERSES = [
  { id: "NASDAQ100", label: "NASDAQ100 (US)", enabled: false },
  { id: "SP500", label: "SP500 (US)", enabled: false },
  { id: "RUSSELL2000", label: "Russell2000 (US)", enabled: false },
  { id: "CSI300", label: "CSI300 (A股)", enabled: false },
  { id: "CSI500", label: "CSI500 (A股)", enabled: false },
  { id: "CSI1000", label: "CSI1000 (A股)", enabled: false },
  { id: "HK100", label: "HK100 (港股)", enabled: false },
  { id: "HSHARE100", label: "HSHARE100 (H股)", enabled: false },
  { id: "HSI", label: "HSI (港股)", enabled: false },
  { id: "HSTECH", label: "HSTECH (港股)", enabled: false },
];

function populateUniverseSelect(universes) {
  const sel = document.getElementById("universe-select");
  if (!sel) return;
  // A universe is selectable only if its market store actually has symbols. The
  // /v1/system/universes endpoint reports n_symbols per universe (per its market).
  const liveIds = new Set((universes || []).filter((u) => (u.n_symbols || 0) > 0).map((u) => u.id));
  const opts = KNOWN_UNIVERSES.map((u) => ({
    ...u,
    enabled: u.enabled || liveIds.has(u.id),
  }));
  // include any extra live (data-backed) universes not in the known list
  for (const u of universes || []) {
    if ((u.n_symbols || 0) > 0 && !opts.some((o) => o.id === u.id)) {
      opts.push({ id: u.id, label: u.id, enabled: true });
    }
  }
  const current = store.get("universe");
  sel.replaceChildren(
    ...opts.map((u) =>
      el("option", { value: u.id, disabled: !u.enabled, selected: u.id === current }, u.enabled ? u.label : `${u.label} (${t("ctrl.soon")})`)
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
  const text = document.getElementById("status-text");
  const setState = (cls, title, short) => {
    dot.className = "status-pill status-pill--" + cls;
    dot.title = t("status.title") + ": " + title;
    dot.setAttribute("aria-label", t("status.title") + ": " + title);
    if (text) text.textContent = short || t("status." + (cls === "fresh" ? "online" : cls === "stale" ? "stale" : cls === "error" ? "offline" : "unknown"));
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
    const asOf = data.last_date ? `${t("status.asOf")} ${data.last_date}` : "";
    const since = lastSync ? ` — ${t("status.lastSync")} ${lastSync}` : "";
    if (status && (status.degraded || status.status === "degraded")) {
      setState("stale", t("status.degraded") + since);
      return;
    }
    if (lastSync) {
      const ageH = (Date.now() - Date.parse(lastSync)) / 3.6e6;
      if (Number.isFinite(ageH) && ageH > 24) {
        setState("stale", t("status.stale") + since);
        return;
      }
      setState("fresh", t("status.fresh") + since, asOf);
      return;
    }
    setState("fresh", t("status.online"), asOf);
  } catch (err) {
    setState("error", t("status.error"));
  }
}

// "Build <commit>" of the running deployment (as in QFinVerse); the tooltip adds the
// engine version and when it was deployed.
async function showVersion() {
  const node = document.getElementById("app-version");
  if (!node) return;
  try {
    const h = await api.health();
    const build = (h && h.build) || {};
    node.textContent = `Build ${build.id || "—"}`;
    node.title = [
      `${t("build.engine")}: v${h.engine_version || "—"}`,
      `${t("build.commit")}: ${build.commit || "—"}`,
      build.deployed_at ? `${t("build.deployed")}: ${build.deployed_at}` : "",
    ].filter(Boolean).join("\n");
  } catch (_) {
    node.textContent = "Build —";
    node.title = t("build.unavailable");
  }
}

async function loadUniverses() {
  try {
    const universes = await api.universes();
    lastUniverses = Array.isArray(universes) ? universes : [];
  } catch (_) {
    lastUniverses = []; // degrade to roadmap list
  }
  populateUniverseSelect(lastUniverses);
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
        el("div", { className: "error-state-title" }, t("common.loadFailed")),
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
    .route("#/combination", ({ path }) => mount(combination, {}, path))
    .route("#/portfolio", ({ path }) => mount(portfolio, {}, path))
    .route("#/portfolio/:id", ({ params, path }) => mount(portfolio, params, path))
    .route("#/chart", ({ path }) => mount(chart, {}, path))
    .route("#/data", ({ path }) => mount(dataManager, {}, path))
    .route("#/docs", () => { location.href = "/doc/"; })
    .notFound(({ path }) => mount(dashboard, {}, path));
}

// ---------------------------------------------------------------- boot ----

function wireLangToggle() {
  const btn = document.getElementById("lang-toggle");
  if (btn) btn.addEventListener("click", () => toggleLang());
  // On language switch: re-localize the static chrome, refresh data-bound chrome
  // (universe "soon" labels + status), and re-render the active page in the new language.
  onLang(() => {
    localizeChrome();
    refreshThemeLabel();          // the theme button text is dynamic, not a data-i18n node
    populateUniverseSelect(lastUniverses);
    refreshStatus();
    router.navigate("#" + router.current());
  });
}

// ---------------------------------------------------------------- theme ----
// Light / dark / auto, shared with QFinVerse (localStorage "qfv-theme"). index.html
// applies it before first paint; a switch reloads the page so charts pick up the new
// colours. The retro skin still lives at /legacy.
const THEME_KEY = "qfv-theme";
const THEMES = ["light", "dark", "system"];

function currentTheme() {
  try { const v = localStorage.getItem(THEME_KEY); return THEMES.includes(v) ? v : "system"; } catch (_) { return "system"; }
}

function refreshThemeLabel() {
  const theme = currentTheme();
  document.querySelectorAll("#theme-switch button").forEach((b) => {
    b.setAttribute("aria-checked", String(b.dataset.theme === theme));
  });
}

function applyTheme(theme) {
  const dark = theme === "dark" || (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  refreshThemeLabel();
}

function wireThemeToggle() {
  applyTheme(currentTheme());
  document.querySelectorAll("#theme-switch button").forEach((b) => b.addEventListener("click", () => {
    const next = b.dataset.theme;
    if (next === currentTheme()) return;
    try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
    const wasDark = document.documentElement.classList.contains("dark");
    applyTheme(next);
    // charts read their colours once (palette.js), so a real switch reloads
    if (document.documentElement.classList.contains("dark") !== wasDark) location.reload();
  }));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (currentTheme() === "system") location.reload();
  });
}

// ---------------------------------------------------------------- API key ----
// When the server answers 401, ask for a key once and keep it in localStorage
// ("assay_api_key", sent as X-API-Key by api.js).
let keyDialogOpen = false;

function askForKey(rejected) {
  if (keyDialogOpen) return;
  keyDialogOpen = true;
  const input = el("input", { className: "input", type: "password", autocomplete: "off", spellcheck: false });
  const err = el("div", { className: "err" }, rejected ? t("auth.bad") : "");
  const form = el("form", {},
    el("h2", {}, t("auth.title")),
    el("div", { className: "muted" }, t("auth.hint")),
    input, err,
    el("button", { className: "btn btn--primary", type: "submit" }, t("auth.save")));
  const box = el("div", { className: "key-dialog", role: "dialog", "aria-modal": "true" }, form);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = input.value.trim();
    if (!v) return;
    try { localStorage.setItem("assay_api_key", v); } catch (_) {}
    box.remove();
    keyDialogOpen = false;
    location.reload();
  });
  document.body.appendChild(box);
  input.focus();
}

window.addEventListener("assay:auth-required", (e) => askForKey(Boolean(e.detail && e.detail.hadKey)));

let lastUniverses = [];

function boot() {
  localizeChrome();
  wireTopNav();
  wireSidebar();
  wireLangToggle();
  wireThemeToggle();
  registerRoutes();
  syncPeriodControls();
  // Fire-and-forget async chrome; never blocks first paint.
  loadUniverses();
  refreshStatus();
  showVersion();
  // Refresh status when universe/period change (session/data context shifts).
  store.subscribe(() => refreshStatus());
  router.start("#/dashboard");
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
