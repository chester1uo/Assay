// pages/library.js — Factor Library Analysis (assay_webui_design.md §4).
// Contract: export render(root, ctx) where ctx = {api, store, router, charts, el, ...dom}.
//
// Left panel (~35%): filterable factor list (GET /v1/library/factors).
// Mode tabs persisted in the hash query (#/library?mode=detail|matrix):
//   - Factor Detail   (GET /v1/library/factors/{id})
//   - Correlation Matrix (GET /v1/library/correlation-matrix + POST /v1/library/prune dry_run)
// Bulk action bar (Delete / Compare / Clear) when >= 1 checked.
//
// OMITTED (no backend): Alpha Space Map (UMAP), IC Heatmap, Lineage DAG — these
// are shown as disabled tabs with a "needs backend" tooltip; no fake data.
//
// NOTE on hash persistence: the foundation hash-router matches '^/library$' only,
// so a *cold* direct load of '#/library?mode=matrix' falls to the not-found route.
// Within in-app navigation we reflect the mode into the hash via history.replaceState
// (which does NOT re-dispatch the router) and also keep it in localStorage so the
// choice survives reloads. We read the mode from the hash query first, then storage.

const STYLE_ID = "library-page-style";
const MODE_STORAGE = "assay_library_mode";

// --- mode persistence (hash query <-> localStorage) -------------------------

function currentHashQuery() {
  // window.location.hash like '#/library?mode=matrix'
  const h = window.location.hash || "";
  const qi = h.indexOf("?");
  if (qi === -1) return new URLSearchParams();
  return new URLSearchParams(h.slice(qi + 1));
}

function readMode() {
  const q = currentHashQuery();
  const fromHash = q.get("mode");
  if (fromHash === "detail" || fromHash === "matrix") return fromHash;
  try {
    const stored = localStorage.getItem(MODE_STORAGE);
    if (stored === "detail" || stored === "matrix") return stored;
  } catch (_) {
    /* storage unavailable */
  }
  return "detail";
}

function writeMode(mode) {
  try {
    localStorage.setItem(MODE_STORAGE, mode);
  } catch (_) {
    /* ignore */
  }
  // Reflect into the hash query without re-dispatching the router.
  try {
    const base = "#/library";
    const target = mode === "detail" ? base : `${base}?mode=${mode}`;
    const url = new URL(window.location.href);
    url.hash = target;
    window.history.replaceState(null, "", url);
  } catch (_) {
    /* no history API */
  }
}

// --- small format helpers ----------------------------------------------------

function redundancyBadge(score, ctx) {
  const { el } = ctx;
  if (score === null || score === undefined || !Number.isFinite(Number(score))) {
    return el("span", { className: "badge badge--gray" }, "—");
  }
  const v = Number(score);
  let cls = "badge--green";
  let label = ctx.t("lib.redUnique");
  let glyph = "●"; // ●
  if (v > 0.7) {
    cls = "badge--red";
    label = ctx.t("lib.redRedundant");
    glyph = "●";
  } else if (v >= 0.4) {
    cls = "badge--amber";
    label = ctx.t("lib.redSimilar");
    glyph = "◐"; // ◐
  }
  return el("span", { className: `badge ${cls}`, title: ctx.t("lib.redundancyTitle", { v: v.toFixed(2) }) }, glyph + " " + label);
}

function decayClass(d) {
  if (d === null || d === undefined || !Number.isFinite(Number(d))) return "";
  const v = Number(d);
  if (v < 10) return "lib-decay--green";
  if (v <= 30) return "lib-decay--amber";
  return "lib-decay--red";
}

function sourceTag(source, ctx) {
  const { el } = ctx;
  const s = source ? String(source).toUpperCase() : "—";
  return el("span", { className: "tag lib-src", title: ctx.t("lib.sourceTitle", { s }) }, s);
}

// --- error helper ------------------------------------------------------------

function errMessage(err, ctx) {
  if (!err) return ctx.t("lib.errUnknown");
  if (err.status === 503 || err.code === "DATA_UNAVAILABLE") {
    return ctx.t("lib.errDataUnavailable");
  }
  return err.message ? String(err.message) : String(err);
}

// --- styles ------------------------------------------------------------------

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const css = `
.library-page { display: grid; grid-template-columns: 35% 1fr; gap: var(--sp-4); align-items: start; }
.library-page .lib-left, .library-page .lib-right { min-width: 0; }

.lib-toolbar { display: flex; flex-direction: column; gap: var(--sp-3); margin-bottom: var(--sp-3); }
.lib-modetabs { display: inline-flex; gap: var(--sp-1); border-bottom: 1px solid var(--border); margin-bottom: var(--sp-3); flex-wrap: wrap; }
.lib-modetab {
  appearance: none; background: transparent; border: none; cursor: pointer;
  padding: var(--sp-2) var(--sp-3); font-size: 13px; color: var(--text-muted);
  border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.lib-modetab:hover { color: var(--text); }
.lib-modetab.is-active { color: var(--navy); border-bottom-color: var(--blue); font-weight: 500; }
.lib-modetab:disabled { color: var(--border-strong); cursor: not-allowed; }

.lib-filters { display: flex; flex-direction: column; gap: var(--sp-2); }
.lib-filterrow { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; }
.lib-search { flex: 1 1 160px; min-width: 120px; }
.lib-slider { display: flex; align-items: center; gap: var(--sp-2); font-size: 12px; color: var(--text-muted); }
.lib-slider input[type=range] { width: 96px; accent-color: var(--blue); }
.lib-slider .lib-slider-val { font-family: var(--font-mono); color: var(--text); min-width: 28px; text-align: right; }

.lib-bulkbar {
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
  padding: var(--sp-2) var(--sp-3); background: #E4ECFD; border: 1px solid var(--blue);
  border-radius: var(--radius-btn); margin-bottom: var(--sp-3);
}
.lib-bulkbar .lib-bulk-count { font-weight: 500; color: var(--navy); }

.lib-list { list-style: none; margin: 0; padding: 0; max-height: 72vh; overflow-y: auto; }
.lib-item {
  display: grid; grid-template-columns: auto 1fr auto; gap: var(--sp-2);
  align-items: start; padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border); border-left: 3px solid transparent; cursor: pointer;
}
.lib-item:hover { background: var(--gray-1); }
.lib-item.is-selected { border-left-color: var(--blue); }
.lib-item.is-active { background: #E4ECFD; }
.lib-item-check { margin-top: 2px; }
.lib-item-main { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.lib-item-expr {
  font-family: var(--font-mono); font-size: 13px; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.lib-item-meta { display: flex; align-items: center; gap: var(--sp-2); font-size: 12px; color: var(--text-muted); flex-wrap: wrap; }
.lib-item-side { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }

.lib-icirbar { width: 90px; height: 8px; background: var(--gray-1); border-radius: 4px; overflow: hidden; }
.lib-icirbar > span { display: block; height: 100%; background: var(--blue); }
.lib-icir-num { font-family: var(--font-mono); color: var(--text); }
.lib-decay--green { color: var(--green); }
.lib-decay--amber { color: var(--amber); }
.lib-decay--red { color: var(--red); }
.lib-src { font-size: 11px; padding: 1px 5px; }

.lib-detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-3); }
.lib-detail-expr { font-family: var(--font-mono); font-size: 15px; color: var(--navy); word-break: break-word; }
.lib-detail-sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
.lib-metricgrid { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: var(--sp-3); }
.lib-metric { display: flex; flex-direction: column; gap: 2px; }
.lib-metric .lib-metric-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); }
.lib-metric .lib-metric-val { font-family: var(--font-mono); font-size: 16px; color: var(--navy); }

.lib-collapse { border-top: 1px solid var(--border); }
.lib-collapse > summary { cursor: pointer; padding: var(--sp-2) 0; font-weight: 500; font-size: 13px; list-style: revert; }
.lib-json { font-family: var(--font-mono); font-size: 12px; white-space: pre; overflow: auto; max-height: 320px; background: var(--gray-1); padding: var(--sp-3); border-radius: var(--radius-badge); margin: 0; }

.lib-matrix-controls { display: flex; align-items: center; gap: var(--sp-4); flex-wrap: wrap; margin-bottom: var(--sp-3); }
.lib-heatmap-wrap { overflow: auto; }
.lib-heatmap text { font-family: var(--font-mono); fill: var(--text-muted); }
.lib-heatmap rect.cell { stroke: #fff; stroke-width: 1; }
.lib-heatmap rect.cell.over { stroke: var(--red); stroke-width: 1.5; }
.lib-prune-list { list-style: none; margin: var(--sp-2) 0 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-1); }
.lib-prune-list li { font-family: var(--font-mono); font-size: 12px; padding: var(--sp-1) var(--sp-2); background: var(--gray-1); border-radius: var(--radius-badge); display: flex; gap: var(--sp-2); align-items: center; }

.lib-mini-empty { padding: var(--sp-6); text-align: center; color: var(--text-muted); }

.lib-add { display: flex; flex-direction: column; gap: var(--sp-2); min-width: 460px; max-width: 560px; }
.lib-add-textarea { width: 100%; resize: vertical; font-family: var(--font-mono); font-size: 13px; line-height: 1.5;
  padding: var(--sp-2); border: 1px solid var(--border); border-radius: var(--radius-card); background: var(--gray-1); color: var(--text); }
.lib-add-textarea:focus-visible { outline: none; box-shadow: var(--focus-ring); border-color: var(--blue); }
.lib-add-row { display: flex; gap: var(--sp-2); flex-wrap: wrap; }
.lib-add-field { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 130px; }
.lib-add-field .input { width: 100%; }
.lib-prog { width: 100%; height: 8px; background: var(--gray-1); border-radius: 4px; overflow: hidden; }
.lib-prog.hidden { display: none; }
.lib-prog-bar { display: block; height: 100%; width: 0%; background: var(--blue, #2D5BE3); transition: width .15s ease; }
.lib-add-status { font-size: 12px; }
`;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = css;
  document.head.appendChild(style);
}

// --- main render -------------------------------------------------------------

export function render(root, ctx) {
  injectStyle();
  const { el, clear } = ctx;

  // Page-local state (lives for the lifetime of this mount).
  const ui = {
    factors: [],          // FactorSummary[]
    loaded: false,
    loadError: null,
    search: "",
    source: "",           // '' = all
    pool: "",             // '' = all universes; else a universe_id
    sortBy: "rank_icir",
    minRankIcir: 0,
    maxRedundancy: 1,
    checked: new Set(),    // factor_ids checked (multi-select)
    activeId: null,        // last-clicked (drives detail panel)
    mode: readMode(),
    // matrix sub-state
    matrixThreshold: 0.7,
  };

  const leftPanel = el("div", { className: "lib-left panel", style: { padding: "var(--sp-3)" } });
  const rightPanel = el("div", { className: "lib-right" });
  const page = el("div", { className: "library-page" }, leftPanel, rightPanel);
  root.replaceChildren(el("div", { className: "page" }, page));

  // ---- data load -----------------------------------------------------------
  function loadList() {
    ui.loaded = false;
    ui.loadError = null;
    renderLeft();
    // The library is a cross-universe catalog — list all factors regardless of the
    // top-nav universe (each row shows its own universe_id). This keeps the seeded
    // Alpha101 / Alpha158 demo (and any saved factor) visible in every context.
    const params = {
      sort_by: ui.sortBy,
      limit: 500,
    };
    // Always send the slider value so it fully controls filtering: the route's
    // default min_rank_icir is 0.0 (hides negative ICIR), so revealing inverse-signal
    // factors requires explicitly sending a negative threshold.
    params.min_rank_icir = ui.minRankIcir;
    if (ui.maxRedundancy < 1) params.max_redundancy = ui.maxRedundancy;
    if (ui.source) params.source = ui.source;
    ctx.api
      .libraryList(params)
      .then((res) => {
        ui.factors = (res && Array.isArray(res.factors)) ? res.factors : [];
        ui.loaded = true;
        // prune checked/active ids that no longer exist
        const ids = new Set(ui.factors.map((f) => f.factor_id));
        ui.checked = new Set([...ui.checked].filter((id) => ids.has(id)));
        if (ui.activeId && !ids.has(ui.activeId)) ui.activeId = null;
        if (!ui.activeId && ui.factors.length) ui.activeId = ui.factors[0].factor_id;
        renderLeft();
        renderRight();
      })
      .catch((err) => {
        ui.loaded = true;
        ui.loadError = err;
        ui.factors = [];
        renderLeft();
        renderRight();
      });
  }

  // Reload when universe changes (the sort/filters that hit the server reload too).
  const unsub = ctx.store.subscribe(() => loadList());
  // Best-effort cleanup: when this root is replaced by the next route, the closure
  // is GC'd; we also detach on a one-shot hashchange away from /library.
  const onHash = () => {
    const h = (window.location.hash || "").replace(/^#/, "");
    if (!h.startsWith("/library")) {
      unsub();
      window.removeEventListener("hashchange", onHash);
    }
  };
  window.addEventListener("hashchange", onHash);

  // ---- client-side filtered view ------------------------------------------
  function visibleFactors() {
    const q = ui.search.trim().toLowerCase();
    return ui.factors.filter((f) => {
      if (q && !(f.expr || "").toLowerCase().includes(q)) return false;
      // sliders are also applied server-side, but re-apply client-side so live
      // drag feedback is correct even before a reload completes.
      if (ui.minRankIcir > 0 && Number(f.rank_icir) < ui.minRankIcir) return false;
      if (ui.maxRedundancy < 1) {
        const r = Number(f.redundancy_score);
        if (Number.isFinite(r) && r > ui.maxRedundancy) return false;
      }
      if (ui.source && String(f.source || "").toUpperCase() !== ui.source.toUpperCase()) return false;
      if (ui.pool && String(f.universe_id || "") !== ui.pool) return false;
      return true;
    });
  }

  function activeFilterCount() {
    let n = 0;
    if (ui.search.trim()) n++;
    if (ui.source) n++;
    if (ui.pool) n++;
    if (ui.minRankIcir > 0) n++;
    if (ui.maxRedundancy < 1) n++;
    return n;
  }

  function uniquePools() {
    const set = new Set();
    for (const f of ui.factors) if (f.universe_id) set.add(String(f.universe_id));
    return [...set].sort();
  }

  // ---- left panel render ---------------------------------------------------
  function renderLeft() {
    clear(leftPanel);

    // Filter bar
    const searchInput = el("input", {
      type: "search",
      className: "input lib-search",
      placeholder: ctx.t("lib.searchPlaceholder"),
      "aria-label": ctx.t("lib.searchAria"),
      value: ui.search,
      onInput: (e) => {
        ui.search = e.target.value;
        renderList();
        updateBulkBar();
      },
    });

    const sources = ["", ...uniqueSources()];
    const sourceSel = el(
      "select",
      {
        className: "select",
        "aria-label": ctx.t("lib.sourceAria"),
        onChange: (e) => {
          ui.source = e.target.value;
          loadList();
        },
      },
      ...sources.map((s) =>
        el("option", { value: s, selected: s === ui.source }, s ? s : ctx.t("lib.allSources"))
      )
    );

    const sortSel = el(
      "select",
      {
        className: "select",
        "aria-label": ctx.t("lib.sortAria"),
        onChange: (e) => {
          ui.sortBy = e.target.value;
          loadList();
        },
      },
      el("option", { value: "rank_icir", selected: ui.sortBy === "rank_icir" }, ctx.t("lib.sortRankIcir")),
      el("option", { value: "rank_ic", selected: ui.sortBy === "rank_ic" }, ctx.t("lib.sortRankIc")),
      el("option", { value: "ic", selected: ui.sortBy === "ic" }, ctx.t("lib.sortIc")),
      el("option", { value: "decay_halflife_days", selected: ui.sortBy === "decay_halflife_days" }, ctx.t("lib.sortDecay")),
      el("option", { value: "redundancy_score", selected: ui.sortBy === "redundancy_score" }, ctx.t("lib.sortRedundancy")),
      el("option", { value: "turnover_1d", selected: ui.sortBy === "turnover_1d" }, ctx.t("lib.sortTurnover"))
    );

    const minIcirVal = el("span", { className: "lib-slider-val" }, ui.minRankIcir.toFixed(2));
    const minIcir = el("label", { className: "lib-slider" },
      ctx.t("lib.minIcir"),
      el("input", {
        type: "range", min: "-1", max: "2", step: "0.05", value: String(ui.minRankIcir),
        "aria-label": ctx.t("lib.minIcirAria"),
        onInput: (e) => { ui.minRankIcir = Number(e.target.value); minIcirVal.textContent = ui.minRankIcir.toFixed(2); renderList(); },
        onChange: () => loadList(),
      }),
      minIcirVal
    );

    const maxRedVal = el("span", { className: "lib-slider-val" }, ui.maxRedundancy.toFixed(2));
    const maxRed = el("label", { className: "lib-slider" },
      ctx.t("lib.maxRedund"),
      el("input", {
        type: "range", min: "0", max: "1", step: "0.05", value: String(ui.maxRedundancy),
        "aria-label": ctx.t("lib.maxRedundAria"),
        onInput: (e) => { ui.maxRedundancy = Number(e.target.value); maxRedVal.textContent = ui.maxRedundancy.toFixed(2); renderList(); },
        onChange: () => loadList(),
      }),
      maxRedVal
    );

    // Pool (universe) selector — '全部' or one of the universes present in the library.
    const pools = ["", ...uniquePools()];
    const poolSel = el(
      "select",
      {
        className: "select",
        "aria-label": ctx.t("lib.poolAria"),
        onChange: (e) => { ui.pool = e.target.value; renderList(); updateBulkBar(); },
      },
      ...pools.map((p) =>
        el("option", { value: p, selected: p === ui.pool }, p ? p : ctx.t("lib.allPools"))
      )
    );

    const fcount = activeFilterCount();
    const clearBtn = el("button", {
      type: "button", className: "btn btn--sm btn--ghost",
      disabled: fcount === 0,
      onClick: () => {
        ui.search = ""; ui.source = ""; ui.pool = ""; ui.minRankIcir = 0; ui.maxRedundancy = 1;
        loadList();
      },
    }, fcount ? ctx.t("lib.clearFiltersN", { n: fcount }) : ctx.t("lib.clearFiltersX"));

    const filters = el("div", { className: "lib-filters" },
      el("div", { className: "lib-filterrow" }, searchInput),
      el("div", { className: "lib-filterrow" }, poolSel, sourceSel, sortSel, clearBtn),
      el("div", { className: "lib-filterrow" }, minIcir, maxRed)
    );

    const addBtn = el("button", {
      type: "button", className: "btn btn--sm btn--primary",
      title: ctx.t("lib.addTitle"), onClick: () => openAddModal(),
    }, "+ " + ctx.t("lib.add"));

    leftPanel.appendChild(
      el("div", { className: "lib-toolbar" },
        el("div", { className: "flex items-center justify-between" },
          el("h2", { className: "section-title" }, ctx.t("lib.factors")),
          el("span", { className: "flex items-center gap-2" },
            addBtn,
            el("span", { className: "label", id: "lib-count" }, listCountLabel())
          )
        ),
        filters
      )
    );

    // Bulk action bar
    leftPanel.appendChild(buildBulkBar());

    // List
    const listWrap = el("ul", { className: "lib-list", id: "lib-list" });
    leftPanel.appendChild(listWrap);
    renderList();
  }

  function uniqueSources() {
    const set = new Set();
    for (const f of ui.factors) if (f.source) set.add(String(f.source).toUpperCase());
    return [...set].sort();
  }

  function listCountLabel() {
    if (!ui.loaded) return ctx.t("lib.loading");
    if (ui.loadError) return "—";
    const vis = visibleFactors().length;
    const tot = ui.factors.length;
    return vis === tot ? `${tot}` : `${vis} / ${tot}`;
  }

  function renderList() {
    const listWrap = leftPanel.querySelector("#lib-list");
    const count = leftPanel.querySelector("#lib-count");
    if (count) count.textContent = listCountLabel();
    if (!listWrap) return;
    clear(listWrap);

    if (!ui.loaded) {
      for (let i = 0; i < 6; i++) {
        listWrap.appendChild(el("li", { className: "lib-item" },
          el("span", {}, ""),
          el("div", { className: "lib-item-main" },
            el("div", { className: "skeleton skeleton-line", style: { width: "80%" } }),
            el("div", { className: "skeleton skeleton-line", style: { width: "50%" } })
          ),
          el("span", {})
        ));
      }
      return;
    }

    if (ui.loadError) {
      listWrap.appendChild(el("li", { className: "lib-mini-empty" },
        el("div", { className: "error-state-title" }, ctx.t("lib.errLoadLibrary")),
        el("div", { className: "muted", style: { fontSize: "12px" } }, errMessage(ui.loadError, ctx))
      ));
      return;
    }

    if (ui.factors.length === 0) {
      listWrap.appendChild(emptyLibraryNode());
      return;
    }

    const vis = visibleFactors();
    if (vis.length === 0) {
      listWrap.appendChild(el("li", { className: "lib-mini-empty" },
        el("div", { className: "empty-state-title" }, ctx.t("lib.noMatch")),
        el("button", { type: "button", className: "btn btn--sm mt-2", onClick: () => {
          ui.search = ""; ui.source = ""; ui.pool = ""; ui.minRankIcir = 0; ui.maxRedundancy = 1; loadList();
        } }, ctx.t("lib.clearFilters"))
      ));
      return;
    }

    for (const f of vis) listWrap.appendChild(listItem(f));
  }

  function emptyLibraryNode() {
    return el("li", { className: "lib-mini-empty" },
      el("div", { className: "empty-state-title" }, ctx.t("lib.empty")),
      el("div", { className: "muted", style: { fontSize: "12px", maxWidth: "320px" } },
        ctx.t("lib.emptyHint")),
      el("button", { type: "button", className: "btn btn--sm btn--primary mt-2",
        onClick: () => ctx.router.navigate("#/factor") }, ctx.t("lib.openTester"))
    );
  }

  function listItem(f) {
    const id = f.factor_id;
    const isChecked = ui.checked.has(id);
    const isActive = ui.activeId === id;

    const checkbox = el("input", {
      type: "checkbox", className: "lib-item-check", checked: isChecked,
      "aria-label": ctx.t("lib.selectFactorAria"),
      onClick: (e) => e.stopPropagation(),
      onChange: (e) => {
        if (e.target.checked) ui.checked.add(id);
        else ui.checked.delete(id);
        item.classList.toggle("is-selected", e.target.checked);
        updateBulkBar();
      },
    });

    const icir = Number(f.rank_icir);
    const icirFill = Number.isFinite(icir) ? Math.max(0, Math.min(1, icir / 2)) * 100 : 0;
    const icirBar = el("div", { className: "lib-icirbar", title: ctx.t("lib.rankIcirTitle", { v: ctx.fmt(f.rank_icir, 2) }) },
      el("span", { style: { width: icirFill.toFixed(0) + "%" } }));

    const decayTxt = (f.decay_halflife_days === null || f.decay_halflife_days === undefined)
      ? "—"
      : ctx.t("lib.decayDays", { d: ctx.fmt(f.decay_halflife_days, 0) });

    const meta = el("div", { className: "lib-item-meta" },
      icirBar,
      el("span", { className: "lib-icir-num" }, ctx.fmt(f.rank_icir, 2)),
      el("span", {}, "·"),
      el("span", { className: decayClass(f.decay_halflife_days) }, decayTxt),
      el("span", {}, "·"),
      redundancyBadge(f.redundancy_score, ctx)
    );

    const item = el("li", {
      className: ctx.cx("lib-item", { "is-selected": isChecked, "is-active": isActive }),
      role: "button", tabindex: "0",
      onClick: () => selectFactor(id),
      onKeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectFactor(id); } },
    },
      checkbox,
      el("div", { className: "lib-item-main" },
        el("div", { className: "lib-item-expr", title: f.expr || "" }, f.expr || ctx.t("lib.noExpr")),
        meta
      ),
      el("div", { className: "lib-item-side" },
        sourceTag(f.source, ctx),
        f.failure_mode ? el("span", { className: "badge badge--red", title: ctx.t("lib.failureMode") }, String(f.failure_mode)) : null
      )
    );
    return item;
  }

  function selectFactor(id) {
    ui.activeId = id;
    // selecting a factor implies looking at it -> Factor Detail mode (unless comparing)
    if (ui.mode !== "matrix") {
      ui.mode = "detail";
      writeMode("detail");
    }
    renderList(); // refresh is-active highlight
    renderRight();
  }

  // ---- bulk action bar -----------------------------------------------------
  function buildBulkBar() {
    const bar = el("div", { className: "lib-bulkbar", id: "lib-bulkbar" });
    fillBulkBar(bar);
    return bar;
  }

  function updateBulkBar() {
    const bar = leftPanel.querySelector("#lib-bulkbar");
    if (bar) fillBulkBar(bar);
  }

  function fillBulkBar(bar) {
    clear(bar);
    const n = ui.checked.size;
    if (n === 0) {
      bar.classList.add("hidden");
      return;
    }
    bar.classList.remove("hidden");
    bar.appendChild(el("span", { className: "lib-bulk-count" }, ctx.t("lib.nSelected", { n })));
    bar.appendChild(el("button", {
      type: "button", className: "btn btn--sm", title: ctx.t("lib.compareTitle"),
      disabled: n < 2,
      onClick: () => setMode("matrix"),
    }, ctx.t("lib.compare")));
    bar.appendChild(el("button", {
      type: "button", className: "btn btn--sm btn--danger",
      onClick: () => bulkDelete(),
    }, ctx.t("lib.delete")));
    bar.appendChild(el("button", {
      type: "button", className: "btn btn--sm btn--ghost",
      onClick: () => { ui.checked.clear(); renderLeft(); },
    }, ctx.t("lib.clearX")));
  }

  function bulkDelete() {
    const ids = [...ui.checked];
    const exprs = ids.map((id) => {
      const f = ui.factors.find((x) => x.factor_id === id);
      return f ? (f.expr || id) : id;
    });
    const ok = window.confirm(
      ctx.t("lib.deleteConfirm", { n: ids.length }) + "\n\n" + exprs.slice(0, 12).join("\n") +
      (exprs.length > 12 ? "\n" + ctx.t("lib.andMore", { n: exprs.length - 12 }) : "")
    );
    if (!ok) return;
    ctx.api.libraryDelete(ids)
      .then(() => {
        ui.checked.clear();
        if (ids.includes(ui.activeId)) ui.activeId = null;
        loadList();
      })
      .catch((err) => {
        window.alert(ctx.t("lib.deleteFailed", { msg: errMessage(err, ctx) }));
      });
  }

  // ---- mode tabs + right panel --------------------------------------------
  function setMode(mode) {
    ui.mode = mode;
    writeMode(mode);
    renderRight();
  }

  function renderRight() {
    clear(rightPanel);

    // Mode tabs
    const tab = (mode, label, opts = {}) => el("button", {
      type: "button",
      className: ctx.cx("lib-modetab", { "is-active": ui.mode === mode && !opts.disabled }),
      disabled: !!opts.disabled,
      title: opts.title || label,
      onClick: opts.disabled ? null : () => setMode(mode),
    }, label);

    const tabs = el("div", { className: "lib-modetabs", role: "tablist" },
      tab("detail", ctx.t("lib.tabDetail")),
      tab("matrix", ctx.t("lib.tabMatrix")),
      tab("umap", ctx.t("lib.tabUmap"), { title: ctx.t("lib.tabUmapTitle") }),
      tab("icheat", ctx.t("lib.tabIcHeatmap"), { title: ctx.t("lib.tabIcHeatmapTitle") }),
      tab("lineage", ctx.t("lib.tabLineage"), { title: ctx.t("lib.tabLineageTitle") })
    );
    rightPanel.appendChild(tabs);

    const body = el("div", { className: "lib-right-body" });
    rightPanel.appendChild(body);

    if (ui.loadError && ui.factors.length === 0) {
      body.appendChild(el("div", { className: "error-state" },
        el("div", { className: "error-state-title" }, ctx.t("lib.libUnavailable")),
        el("div", { className: "muted" }, errMessage(ui.loadError, ctx))
      ));
      return;
    }

    if (ui.mode === "matrix") renderMatrix(body);
    else if (ui.mode === "umap") renderUmap(body);
    else if (ui.mode === "icheat") renderIcHeat(body);
    else if (ui.mode === "lineage") renderLineage(body);
    else renderDetail(body);
  }

  // ---- shared: which factor ids feed the multi-factor views -----------------
  // Checked factors if >=2, else the visible top-N (mirrors the matrix view).
  function viewIds(cap) {
    let ids = [...ui.checked];
    if (ids.length < 2) ids = visibleFactors().slice(0, cap).map((f) => f.factor_id);
    return ids;
  }

  function viewLoading(body, msg) {
    const card = el("div", { className: "card" });
    card.appendChild(el("div", { className: "muted" }, msg));
    body.appendChild(card);
    return card;
  }

  function viewError(card, titleKey, err) {
    clear(card);
    card.appendChild(el("div", { className: "error-state" },
      el("div", { className: "error-state-title" }, ctx.t(titleKey)),
      el("div", { className: "muted" }, errMessage(err, ctx))));
  }

  function exprForId(id) {
    const f = ui.factors.find((x) => x.factor_id === id);
    return f ? (f.expr || id) : id;
  }

  // ---- Mode C: IC heatmap (factor × period RankIC) --------------------------
  function renderIcHeat(body) {
    const ids = viewIds(20);
    if (ids.length < 1) {
      body.appendChild(emptyTwo()); return;
    }
    const bucketSel = el("select", { className: "select",
      onChange: () => load(), "aria-label": ctx.t("ic.bucket") },
      ...["month", "quarter", "week"].map((b) => el("option", { value: b, selected: b === "month" }, ctx.t("ic.b_" + b))));
    const controls = el("div", { className: "lib-matrix-controls" },
      el("label", { className: "lib-slider" }, ctx.t("ic.bucket"), bucketSel),
      el("span", { className: "label" }, ctx.t("lib.nFactors", { n: ids.length })));
    const card = el("div", { className: "card" });
    card.appendChild(controls);
    const wrap = el("div", { className: "lib-heatmap-wrap" });
    card.appendChild(wrap);
    body.appendChild(card);

    function load() {
      clear(wrap);
      wrap.appendChild(el("div", { className: "muted" }, ctx.t("ic.computing")));
      const universe = ctx.store.get("universe");
      const period = ctx.store.get("period");
      ctx.api.icHeatmap(ids, { universe, period, bucket: bucketSel.value })
        .then((res) => {
          if (ui.mode !== "icheat") return;
          clear(wrap);
          if (!res || !Array.isArray(res.matrix) || !res.matrix.length) {
            wrap.appendChild(el("div", { className: "muted" }, ctx.t("ic.noData"))); return;
          }
          wrap.appendChild(buildIcHeat(res));
        })
        .catch((err) => { if (ui.mode === "icheat") viewError(card, "ic.unavailable", err); });
    }
    load();
  }

  function buildIcHeat(res) {
    const NS = "http://www.w3.org/2000/svg";
    const fids = res.factor_ids, periods = res.periods, M = res.matrix, summ = res.summary || [];
    const rows = fids.length, cols = periods.length;
    const cellW = 34, cellH = 26, labelW = 160, labelH = 70, sumW = 52, pad = 8;
    const W = labelW + cols * cellW + sumW + pad * 2;
    const H = labelH + rows * cellH + pad * 2;
    // colour scale: symmetric around 0 by the largest |RankIC| present
    let maxAbs = 0;
    for (const row of M) for (const v of row) if (Number.isFinite(v)) maxAbs = Math.max(maxAbs, Math.abs(v));
    for (const v of summ) if (Number.isFinite(v)) maxAbs = Math.max(maxAbs, Math.abs(v));
    maxAbs = maxAbs || 1;

    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("width", String(W));
    svg.setAttribute("height", String(H)); svg.setAttribute("class", "chart lib-heatmap");
    const txt = (x, y, s, opts = {}) => {
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", String(x)); t.setAttribute("y", String(y));
      t.setAttribute("font-size", String(opts.size || 10));
      if (opts.anchor) t.setAttribute("text-anchor", opts.anchor);
      if (opts.rot) t.setAttribute("transform", `rotate(${opts.rot} ${x} ${y})`);
      if (opts.fill) t.setAttribute("fill", opts.fill);
      t.textContent = s;
      if (opts.title) { const tt = document.createElementNS(NS, "title"); tt.textContent = opts.title; t.appendChild(tt); }
      return t;
    };
    // period column headers (rotated)
    for (let j = 0; j < cols; j++) {
      const cx = labelW + j * cellW + cellW / 2;
      svg.appendChild(txt(cx, labelH - 6, periods[j].slice(2), { anchor: "start", rot: -55, size: 9 }));
    }
    svg.appendChild(txt(labelW + cols * cellW + sumW / 2, labelH - 6, ctx.t("ic.mean"), { anchor: "start", rot: -55, size: 9 }));
    // rows
    for (let i = 0; i < rows; i++) {
      const rowExpr = exprForId(fids[i]);
      const yc = labelH + i * cellH;
      const rt = txt(labelW - 6, yc + cellH / 2 + 3, truncate(rowExpr, 26), { anchor: "end", size: 10, title: rowExpr });
      rt.style.cursor = "pointer";
      rt.addEventListener("click", () => selectFactor(fids[i]));
      svg.appendChild(rt);
      for (let j = 0; j < cols; j++) {
        const v = M[i][j];
        const rect = document.createElementNS(NS, "rect");
        rect.setAttribute("x", String(labelW + j * cellW)); rect.setAttribute("y", String(yc));
        rect.setAttribute("width", String(cellW - 2)); rect.setAttribute("height", String(cellH - 2));
        rect.setAttribute("rx", "2");
        rect.setAttribute("fill", Number.isFinite(v) ? ctx.charts.diverging(v, maxAbs) : "#F3F4F6");
        const tt = document.createElementNS(NS, "title");
        tt.textContent = `${rowExpr}\n${periods[j]}: ${Number.isFinite(v) ? v.toFixed(4) : "—"}`;
        rect.appendChild(tt);
        svg.appendChild(rect);
      }
      // summary cell (mean RankIC)
      const sv = summ[i];
      const sx = labelW + cols * cellW + 4;
      const srect = document.createElementNS(NS, "rect");
      srect.setAttribute("x", String(sx)); srect.setAttribute("y", String(yc));
      srect.setAttribute("width", String(sumW - 6)); srect.setAttribute("height", String(cellH - 2));
      srect.setAttribute("rx", "2");
      srect.setAttribute("fill", Number.isFinite(sv) ? ctx.charts.diverging(sv, maxAbs) : "#F3F4F6");
      srect.setAttribute("stroke", "#0001");
      svg.appendChild(srect);
      svg.appendChild(txt(sx + (sumW - 6) / 2, yc + cellH / 2 + 3, Number.isFinite(sv) ? sv.toFixed(3) : "—",
        { anchor: "middle", size: 9, fill: Math.abs(sv) > maxAbs * 0.5 ? "#fff" : "#333" }));
    }
    return svg;
  }

  // ---- Mode B: Alpha space map (2-D similarity scatter) ---------------------
  function renderUmap(body) {
    const ids = viewIds(60);
    if (ids.length < 3) { body.appendChild(emptyTwo(ctx.t("umap.needThree"))); return; }
    const methodSel = el("select", { className: "select", onChange: () => load(), "aria-label": ctx.t("umap.method") },
      el("option", { value: "mds", selected: true }, "MDS"),
      el("option", { value: "tsne" }, "t-SNE"),
      el("option", { value: "umap" }, "UMAP"));
    const note = el("span", { className: "muted", style: { fontSize: "12px" } });
    const controls = el("div", { className: "lib-matrix-controls" },
      el("label", { className: "lib-slider" }, ctx.t("umap.method"), methodSel),
      el("span", { className: "label" }, ctx.t("lib.nFactors", { n: ids.length })), note);
    const card = el("div", { className: "card" });
    card.appendChild(controls);
    const wrap = el("div", { className: "lib-heatmap-wrap" });
    card.appendChild(wrap);
    body.appendChild(card);

    function load() {
      clear(wrap);
      wrap.appendChild(el("div", { className: "muted" }, ctx.t("umap.computing")));
      const universe = ctx.store.get("universe");
      const period = ctx.store.get("period");
      ctx.api.factorEmbedding(ids, { universe, period, method: methodSel.value })
        .then((res) => {
          if (ui.mode !== "umap") return;
          clear(wrap);
          if (!res || !Array.isArray(res.points) || res.points.length < 2) {
            wrap.appendChild(el("div", { className: "muted" }, ctx.t("umap.noData"))); return;
          }
          note.textContent = ctx.t("umap.proj", { m: (res.method || "mds").toUpperCase() });
          wrap.appendChild(buildScatter(res));
        })
        .catch((err) => { if (ui.mode === "umap") viewError(card, "umap.unavailable", err); });
    }
    load();
  }

  const CLUSTER_COLORS = ["#2D5BE3", "#0E8A7E", "#C0392B", "#8E44AD", "#D98C00", "#1E7B4B", "#2C82C9", "#C0398B"];

  function buildScatter(res) {
    const NS = "http://www.w3.org/2000/svg";
    const pts = res.points;
    const W = 720, H = 460, m = 36;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const xlo = Math.min(...xs), xhi = Math.max(...xs), ylo = Math.min(...ys), yhi = Math.max(...ys);
    const sx = (x) => m + ((x - xlo) / ((xhi - xlo) || 1)) * (W - 2 * m);
    const sy = (y) => H - m - ((y - ylo) / ((yhi - ylo) || 1)) * (H - 2 * m);
    const icirs = pts.map((p) => Math.abs(Number(p.rank_icir) || 0));
    const maxIcir = Math.max(...icirs, 0.01);
    const rOf = (p) => 5 + 9 * (Math.abs(Number(p.rank_icir) || 0) / maxIcir);

    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("width", "100%");
    svg.setAttribute("height", String(H)); svg.setAttribute("class", "chart");
    // subtle frame
    const frame = document.createElementNS(NS, "rect");
    frame.setAttribute("x", String(m - 8)); frame.setAttribute("y", String(m - 8));
    frame.setAttribute("width", String(W - 2 * m + 16)); frame.setAttribute("height", String(H - 2 * m + 16));
    frame.setAttribute("fill", "none"); frame.setAttribute("stroke", "#0001"); frame.setAttribute("rx", "8");
    svg.appendChild(frame);

    for (const p of pts) {
      const g = document.createElementNS(NS, "g");
      g.style.cursor = "pointer";
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", String(sx(p.x))); c.setAttribute("cy", String(sy(p.y)));
      c.setAttribute("r", String(rOf(p)));
      c.setAttribute("fill", CLUSTER_COLORS[(p.cluster || 0) % CLUSTER_COLORS.length]);
      c.setAttribute("fill-opacity", "0.78"); c.setAttribute("stroke", "#fff"); c.setAttribute("stroke-width", "1");
      const tt = document.createElementNS(NS, "title");
      tt.textContent = `${p.expr}\nRankIC ${fmtMaybe(p.rank_ic, 3)} · RankICIR ${fmtMaybe(p.rank_icir, 2)}${p.source ? " · " + p.source : ""}`;
      c.appendChild(tt);
      g.appendChild(c);
      g.addEventListener("click", () => selectFactor(p.id));
      svg.appendChild(g);
    }
    return svg;
  }

  // ---- Mode D: Lineage DAG (expression derivation) --------------------------
  function renderLineage(body) {
    const card = el("div", { className: "card" });
    const wrap = el("div", { className: "lib-heatmap-wrap" });
    card.appendChild(wrap);
    body.appendChild(card);
    wrap.appendChild(el("div", { className: "muted" }, ctx.t("lin.computing")));
    // Lineage is over the whole (visible) library; use checked subset if any.
    const ids = ui.checked.size >= 2 ? [...ui.checked] : null;
    ctx.api.factorLineage(ids, { limit: 200 })
      .then((res) => {
        if (ui.mode !== "lineage") return;
        clear(wrap);
        if (!res || !Array.isArray(res.nodes) || !res.nodes.length) {
          wrap.appendChild(el("div", { className: "muted" }, ctx.t("lin.noData"))); return;
        }
        wrap.appendChild(buildLineage(res));
      })
      .catch((err) => { if (ui.mode === "lineage") viewError(card, "lin.unavailable", err); });
  }

  function buildLineage(res) {
    const NS = "http://www.w3.org/2000/svg";
    const nodes = res.nodes, edges = res.edges || [];
    const byId = new Map(nodes.map((n) => [n.id, n]));
    // Layer = longest path from a root (no incoming edge), so derivations flow right.
    const inEdges = new Map(nodes.map((n) => [n.id, []]));
    const outEdges = new Map(nodes.map((n) => [n.id, []]));
    for (const e of edges) {
      if (byId.has(e.from) && byId.has(e.to)) { inEdges.get(e.to).push(e.from); outEdges.get(e.from).push(e.to); }
    }
    const layer = new Map();
    const visiting = new Set();
    function layerOf(id) {
      if (layer.has(id)) return layer.get(id);
      if (visiting.has(id)) return 0; // cycle guard (shouldn't happen on a DAG)
      visiting.add(id);
      const parents = inEdges.get(id) || [];
      const L = parents.length ? Math.max(...parents.map(layerOf)) + 1 : 0;
      visiting.delete(id);
      layer.set(id, L); return L;
    }
    for (const n of nodes) layerOf(n.id);
    // bucket nodes by layer
    const layers = [];
    for (const n of nodes) { const L = layer.get(n.id); (layers[L] = layers[L] || []).push(n); }
    const colW = 240, rowH = 56, nodeW = 200, nodeH = 38, padX = 20, padY = 20;
    const W = padX * 2 + layers.length * colW;
    const H = padY * 2 + Math.max(1, ...layers.map((l) => (l ? l.length : 0))) * rowH;
    const pos = new Map();
    layers.forEach((list, li) => {
      (list || []).forEach((n, ri) => {
        pos.set(n.id, { x: padX + li * colW, y: padY + ri * rowH + (rowH - nodeH) / 2 });
      });
    });

    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("width", String(W));
    svg.setAttribute("height", String(H)); svg.setAttribute("class", "chart");
    // arrow marker (built programmatically — innerHTML is unreliable for SVG)
    const defs = document.createElementNS(NS, "defs");
    const marker = document.createElementNS(NS, "marker");
    marker.setAttribute("id", "lin-arrow"); marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "9"); marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "7"); marker.setAttribute("markerHeight", "7");
    marker.setAttribute("orient", "auto-start-reverse");
    const mpath = document.createElementNS(NS, "path");
    mpath.setAttribute("d", "M0,0 L10,5 L0,10 z"); mpath.setAttribute("fill", "#9aa3b2");
    marker.appendChild(mpath); defs.appendChild(marker); svg.appendChild(defs);
    // edges first (under nodes)
    for (const e of edges) {
      const a = pos.get(e.from), b = pos.get(e.to);
      if (!a || !b) continue;
      const x1 = a.x + nodeW, y1 = a.y + nodeH / 2, x2 = b.x, y2 = b.y + nodeH / 2;
      const mx = (x1 + x2) / 2;
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
      path.setAttribute("fill", "none"); path.setAttribute("stroke", "#9aa3b2"); path.setAttribute("stroke-width", "1.4");
      path.setAttribute("marker-end", "url(#lin-arrow)");
      svg.appendChild(path);
    }
    // nodes
    const icirs = nodes.map((n) => Math.abs(Number(n.rank_icir) || 0));
    const maxIcir = Math.max(...icirs, 0.01);
    for (const n of nodes) {
      const p = pos.get(n.id); if (!p) continue;
      const g = document.createElementNS(NS, "g");
      g.style.cursor = "pointer";
      g.setAttribute("transform", `translate(${p.x},${p.y})`);
      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("width", String(nodeW)); rect.setAttribute("height", String(nodeH));
      rect.setAttribute("rx", "7");
      const intensity = Math.abs(Number(n.rank_icir) || 0) / maxIcir;
      rect.setAttribute("fill", ctx.charts.seq(0.15 + 0.55 * intensity));
      rect.setAttribute("stroke", "#0002");
      g.appendChild(rect);
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", "10"); t.setAttribute("y", String(nodeH / 2 + 4)); t.setAttribute("font-size", "11");
      t.textContent = truncate(n.expr, 26);
      const tt = document.createElementNS(NS, "title");
      tt.textContent = `${n.expr}\nRankICIR ${fmtMaybe(n.rank_icir, 2)} · depth ${n.depth}${n.source ? " · " + n.source : ""}`;
      t.appendChild(tt);
      g.appendChild(t);
      g.addEventListener("click", () => selectFactor(n.id));
      svg.appendChild(g);
    }
    return svg;
  }

  function emptyTwo(hint) {
    return el("div", { className: "empty-state" },
      el("div", { className: "empty-state-title" }, ctx.t("lib.selectTwo")),
      el("div", { className: "muted" }, hint || ctx.t("lib.selectTwoHint")));
  }
  function fmtMaybe(v, dp) { const n = Number(v); return Number.isFinite(n) ? n.toFixed(dp) : "—"; }

  // ---- Mode A: Factor Detail ----------------------------------------------
  function renderDetail(body) {
    if (!ui.activeId) {
      body.appendChild(el("div", { className: "empty-state" },
        el("div", { className: "empty-state-title" }, ctx.t("lib.selectFactor")),
        el("div", { className: "muted" }, ctx.t("lib.selectFactorHint"))
      ));
      return;
    }

    const card = el("div", { className: "card" });
    body.appendChild(card);
    card.appendChild(el("div", { className: "muted" }, ctx.t("lib.loadingReport")));

    const targetId = ui.activeId;
    ctx.api.libraryGet(targetId)
      .then((rep) => {
        if (ui.activeId !== targetId || ui.mode !== "detail") return; // stale
        clear(card);
        fillDetail(card, rep);
      })
      .catch((err) => {
        if (ui.activeId !== targetId) return;
        clear(card);
        card.appendChild(el("div", { className: "error-state" },
          el("div", { className: "error-state-title" }, ctx.t("lib.errLoadFactor")),
          el("div", { className: "muted" }, errMessage(err, ctx))
        ));
      });
  }

  function fillDetail(card, rep) {
    const { el } = ctx;
    const evalDate = (rep.eval_period && rep.eval_period[1]) || (rep.lineage && rep.lineage.evaluated_at) || null;

    const head = el("div", { className: "lib-detail-head" },
      el("div", {},
        el("div", { className: "lib-detail-expr", title: rep.expr_canonical || rep.expr }, rep.expr || ctx.t("lib.noExpr")),
        el("div", { className: "lib-detail-sub" },
          ctx.t("lib.factorId", { id: rep.factor_id || "—" }),
          evalDate ? "  ·  " + ctx.t("lib.evaluated", { date: evalDate }) : "",
          rep.universe_id ? `  ·  ${rep.universe_id}` : "",
          (rep.lineage && rep.lineage.source) ? `  ·  ${String(rep.lineage.source).toUpperCase()}` : ""
        )
      ),
      el("button", { type: "button", className: "btn btn--sm btn--primary",
        onClick: () => ctx.router.navigate("#/factor/" + encodeURIComponent(rep.factor_id)) },
        ctx.t("lib.openInTester"))
    );
    card.appendChild(head);

    // metric grid
    const metric = (label, value, extra) => el("div", { className: "lib-metric" },
      el("span", { className: "lib-metric-label" }, label),
      el("span", { className: "lib-metric-val" }, value),
      extra ? el("span", { className: "muted", style: { fontSize: "11px" } }, extra) : null
    );

    const lookahead = rep.lookahead_detected
      ? el("span", { className: "badge badge--red" }, ctx.t("lib.lookaheadDetected"))
      : el("span", { className: "badge badge--green" }, ctx.t("lib.lookaheadClean"));

    const grid = el("div", { className: "lib-metricgrid mt-4" },
      metric(ctx.t("lib.mRankIc"), ctx.fmt(rep.rank_ic, 3)),
      metric(ctx.t("lib.mRankIcir"), ctx.fmt(rep.rank_icir, 2)),
      metric(ctx.t("lib.mIc"), ctx.fmt(rep.ic, 3)),
      metric(ctx.t("lib.mIcir"), ctx.fmt(rep.icir, 2)),
      metric(ctx.t("lib.mDecay"), rep.decay_halflife_days == null ? "—" : ctx.fmt(rep.decay_halflife_days, 0) + "d"),
      metric(ctx.t("lib.mTurnover"), ctx.fmt(rep.turnover_1d, 2)),
      metric(ctx.t("lib.mRedundancy"), ctx.fmt(rep.redundancy_score, 2),
        rep.most_similar_factor ? ctx.t("lib.nearest", { id: rep.most_similar_factor }) : null),
      el("div", { className: "lib-metric" },
        el("span", { className: "lib-metric-label" }, ctx.t("lib.mLookahead")),
        lookahead
      )
    );
    card.appendChild(grid);

    // IC sparkline
    const series = Array.isArray(rep.ic_series) ? rep.ic_series
      : (Array.isArray(rep.rank_ic_series) ? rep.rank_ic_series : []);
    const sparkBlock = el("div", { className: "card-body mt-4" },
      el("div", { className: "flex items-center justify-between" },
        el("span", { className: "card-title" }, ctx.t("lib.icSeries")),
        el("span", { className: "label" }, series.length ? ctx.t("lib.nPoints", { n: series.length }) : ctx.t("lib.noSeries"))
      )
    );
    if (series.length >= 2) {
      const spark = ctx.charts.sparkline(series, { width: 560, height: 56, color: "#2D5BE3" });
      sparkBlock.appendChild(el("div", { className: "chart-wrap" }, spark));
    } else {
      sparkBlock.appendChild(el("div", { className: "muted", style: { fontSize: "12px" } }, ctx.t("lib.icSeriesNa")));
    }
    card.appendChild(sparkBlock);

    // suggestion / failure
    if (rep.suggestion || rep.failure_mode) {
      const sug = el("div", { className: "card mt-4", style: { background: "var(--gray-1)", border: "none" } });
      if (rep.failure_mode) {
        sug.appendChild(el("div", { className: "flex items-center gap-2", style: { marginBottom: "var(--sp-1)" } },
          el("span", { className: "badge badge--amber" }, ctx.t("lib.failureMode")),
          el("span", { className: "mono" }, String(rep.failure_mode))
        ));
      }
      if (rep.suggestion) {
        sug.appendChild(el("div", { className: "card-title", style: { marginBottom: "2px" } }, ctx.t("lib.suggestion")));
        sug.appendChild(el("div", {}, String(rep.suggestion)));
      }
      card.appendChild(sug);
    }

    // collapsible raw JSON
    const json = el("details", { className: "lib-collapse mt-4" },
      el("summary", {}, ctx.t("lib.fullJson")),
      el("pre", { className: "lib-json" }, safeJson(rep))
    );
    card.appendChild(json);
  }

  // ---- Mode B: Correlation Matrix -----------------------------------------
  function renderMatrix(body) {
    // Use checked factors if >=2, else fall back to the currently visible top-N.
    let ids = [...ui.checked];
    if (ids.length < 2) {
      ids = visibleFactors().slice(0, 12).map((f) => f.factor_id);
    }

    if (ids.length < 2) {
      body.appendChild(el("div", { className: "empty-state" },
        el("div", { className: "empty-state-title" }, ctx.t("lib.selectTwo")),
        el("div", { className: "muted" }, ctx.t("lib.selectTwoHint"))
      ));
      return;
    }

    // Controls
    const threshVal = el("span", { className: "lib-slider-val" }, ui.matrixThreshold.toFixed(2));
    const summaryLine = el("div", { className: "muted mt-2", style: { fontSize: "13px" } });
    const pruneSlot = el("div", { className: "mt-2" });

    const controls = el("div", { className: "lib-matrix-controls" },
      el("label", { className: "lib-slider" },
        ctx.t("lib.redundancyThreshold"),
        el("input", {
          type: "range", min: "0", max: "1", step: "0.05", value: String(ui.matrixThreshold),
          "aria-label": ctx.t("lib.correlationThresholdAria"),
          onInput: (e) => { ui.matrixThreshold = Number(e.target.value); threshVal.textContent = ui.matrixThreshold.toFixed(2); refreshOverlay(); },
        }),
        threshVal
      ),
      el("span", { className: "label" }, ctx.t("lib.nFactors", { n: ids.length }))
    );

    const card = el("div", { className: "card" });
    body.appendChild(card);
    card.appendChild(controls);
    const heatWrap = el("div", { className: "lib-heatmap-wrap" });
    card.appendChild(heatWrap);
    card.appendChild(summaryLine);
    card.appendChild(pruneSlot);
    heatWrap.appendChild(el("div", { className: "muted" }, ctx.t("lib.computingMatrix")));

    let matrixData = null; // {factor_ids, matrix}

    const universe = ctx.store.get("universe");
    const period = ctx.store.get("period");
    ctx.api.correlationMatrix(ids, { universe, period })
      .then((res) => {
        if (ui.mode !== "matrix") return;
        matrixData = res;
        clear(heatWrap);
        if (!res || !Array.isArray(res.matrix) || !res.matrix.length) {
          heatWrap.appendChild(el("div", { className: "muted" }, ctx.t("lib.matrixNoData")));
          return;
        }
        heatWrap.appendChild(buildHeatmap(res));
        refreshOverlay();
      })
      .catch((err) => {
        if (ui.mode !== "matrix") return;
        clear(heatWrap);
        heatWrap.appendChild(el("div", { className: "error-state" },
          el("div", { className: "error-state-title" }, ctx.t("lib.matrixUnavailable")),
          el("div", { className: "muted" }, errMessage(err, ctx))
        ));
      });

    function exprFor(id) {
      const f = ui.factors.find((x) => x.factor_id === id);
      return f ? (f.expr || id) : id;
    }

    function buildHeatmap(res) {
      const fids = res.factor_ids || ids;
      const M = res.matrix;
      const n = fids.length;
      const NS = "http://www.w3.org/2000/svg";
      const cell = 28;
      const labelW = 150;
      const labelH = 120;
      const W = labelW + n * cell + 8;
      const H = labelH + n * cell + 8;

      const svg = document.createElementNS(NS, "svg");
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      svg.setAttribute("width", String(W));
      svg.setAttribute("height", String(H));
      svg.setAttribute("class", "chart lib-heatmap");
      svg.setAttribute("role", "img");

      // per-cell tooltips use SVG <title> children — simplest & accessible.

      // column labels (rotated)
      for (let j = 0; j < n; j++) {
        const t = document.createElementNS(NS, "text");
        const cx = labelW + j * cell + cell / 2;
        t.setAttribute("x", String(cx));
        t.setAttribute("y", String(labelH - 6));
        t.setAttribute("text-anchor", "start");
        t.setAttribute("font-size", "10");
        t.setAttribute("transform", `rotate(-60 ${cx} ${labelH - 6})`);
        t.textContent = truncate(exprFor(fids[j]), 22);
        const ttl = document.createElementNS(NS, "title");
        ttl.textContent = exprFor(fids[j]);
        t.appendChild(ttl);
        svg.appendChild(t);
      }

      // rows
      for (let i = 0; i < n; i++) {
        const rowExpr = exprFor(fids[i]);
        const rt = document.createElementNS(NS, "text");
        rt.setAttribute("x", String(labelW - 6));
        rt.setAttribute("y", String(labelH + i * cell + cell / 2 + 3));
        rt.setAttribute("text-anchor", "end");
        rt.setAttribute("font-size", "10");
        rt.textContent = truncate(rowExpr, 24);
        const rttl = document.createElementNS(NS, "title");
        rttl.textContent = rowExpr;
        rt.appendChild(rttl);
        svg.appendChild(rt);

        const row = M[i] || [];
        for (let j = 0; j < n; j++) {
          const v = Number(row[j]);
          const rect = document.createElementNS(NS, "rect");
          rect.setAttribute("class", "cell");
          rect.setAttribute("x", String(labelW + j * cell));
          rect.setAttribute("y", String(labelH + i * cell));
          rect.setAttribute("width", String(cell));
          rect.setAttribute("height", String(cell));
          // diverging: red high +corr, white 0, blue high -corr
          rect.setAttribute("fill", divergeRWB(Number.isFinite(v) ? v : 0));
          rect.dataset.i = String(i);
          rect.dataset.j = String(j);
          rect.dataset.v = Number.isFinite(v) ? v.toFixed(4) : "";
          const ct = document.createElementNS(NS, "title");
          ct.textContent = `${truncate(rowExpr, 30)}  ×  ${truncate(exprFor(fids[j]), 30)}\n` + ctx.t("lib.correlationLabel", { v: Number.isFinite(v) ? v.toFixed(3) : "—" });
          rect.appendChild(ct);
          svg.appendChild(rect);
        }
      }
      svg._fids = fids;
      svg._M = M;
      return svg;
    }

    function refreshOverlay() {
      const svg = heatWrap.querySelector("svg.lib-heatmap");
      if (!svg) { updateSummary(); return; }
      const thr = ui.matrixThreshold;
      svg.querySelectorAll("rect.cell").forEach((rect) => {
        const i = Number(rect.dataset.i);
        const j = Number(rect.dataset.j);
        const v = rect.dataset.v === "" ? NaN : Number(rect.dataset.v);
        const over = i !== j && Number.isFinite(v) && Math.abs(v) >= thr;
        rect.classList.toggle("over", over);
      });
      updateSummary();
    }

    function updateSummary() {
      const data = matrixData;
      const thr = ui.matrixThreshold;
      if (!data || !Array.isArray(data.matrix)) { summaryLine.textContent = ""; return; }
      const M = data.matrix;
      const n = (data.factor_ids || ids).length;
      let pairs = 0;
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          const v = Number((M[i] || [])[j]);
          if (Number.isFinite(v) && Math.abs(v) >= thr) pairs++;
        }
      }
      clear(summaryLine);
      summaryLine.appendChild(document.createTextNode(ctx.t("lib.pairsAboveThreshold", { pairs, thr: thr.toFixed(2) }) + " "));
      const previewBtn = el("button", { type: "button", className: "btn btn--sm mt-2",
        onClick: () => previewPruning() }, ctx.t("lib.previewPruning"));
      summaryLine.appendChild(previewBtn);
    }

    // Greedy redundancy pruning, computed CLIENT-SIDE from the already-loaded
    // correlation matrix — instant, no extra backend round-trip (the /prune endpoint
    // would re-build the engine and re-evaluate every factor, which is slow). For each
    // over-threshold pair (in descending |corr|) drop the lower-RankICIR factor.
    function previewPruning() {
      clear(pruneSlot);
      if (!matrixData || !Array.isArray(matrixData.matrix) || !matrixData.matrix.length) {
        pruneSlot.appendChild(el("div", { className: "placeholder-note" }, ctx.t("lib.matrixNoData")));
        return;
      }
      const fids = matrixData.factor_ids || ids;
      const M = matrixData.matrix;
      const thr = ui.matrixThreshold;
      const scoreOf = (id) => {
        const f = ui.factors.find((x) => x.factor_id === id);
        const v = f ? Number(f.rank_icir) : NaN;
        return Number.isFinite(v) ? v : -Infinity;
      };
      const pairs = [];
      for (let i = 0; i < fids.length; i++) {
        for (let j = i + 1; j < fids.length; j++) {
          const v = Number((M[i] || [])[j]);
          if (Number.isFinite(v) && Math.abs(v) >= thr) pairs.push([Math.abs(v), i, j]);
        }
      }
      pairs.sort((a, b) => b[0] - a[0]);
      const removed = new Set();
      for (const [, i, j] of pairs) {
        if (removed.has(fids[i]) || removed.has(fids[j])) continue;
        removed.add(scoreOf(fids[i]) >= scoreOf(fids[j]) ? fids[j] : fids[i]);
      }
      const idList = [...removed];
      if (idList.length === 0) {
        pruneSlot.appendChild(el("div", { className: "placeholder-note" }, ctx.t("lib.noPrune")));
        return;
      }
      const list = el("ul", { className: "lib-prune-list" });
      for (const id of idList) {
        list.appendChild(el("li", {},
          el("span", { className: "badge badge--red" }, ctx.t("lib.remove")),
          el("span", { title: id }, truncate(exprFor(id), 60))
        ));
      }
      pruneSlot.appendChild(el("div", { className: "card-title", style: { marginBottom: "var(--sp-1)" } },
        ctx.t("lib.pruneWouldRemove", { n: idList.length })));
      pruneSlot.appendChild(list);
    }
  }

  // ---- add factors (bulk import) modal ------------------------------------
  function openAddModal() {
    const textarea = el("textarea", {
      className: "lib-add-textarea mono", spellcheck: "false", autocomplete: "off", rows: "10",
      placeholder: ctx.t("lib.addExprPlaceholder"),
    });
    const fileInput = el("input", { type: "file", accept: ".txt,.csv,text/plain", className: "input" });
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        const cur = textarea.value.trim();
        textarea.value = (cur ? cur + "\n" : "") + String(reader.result || "");
      };
      reader.readAsText(file);
    });

    const uniInput = el("input", { className: "input", value: ctx.store.get("universe") || "", "aria-label": ctx.t("lib.addUniverse") });
    const srcInput = el("input", { className: "input", value: "CUSTOM", "aria-label": ctx.t("lib.addSource") });

    const bar = el("span", { className: "lib-prog-bar" });
    const progWrap = el("div", { className: "lib-prog hidden" }, bar);
    const status = el("div", { className: "muted lib-add-status" }, "");

    const importBtn = el("button", { className: "btn btn--primary", type: "button" }, ctx.t("lib.addImport"));
    let closeFn = () => {};
    let busy = false;

    function parseExprs() {
      return [...new Set(
        textarea.value.split(/\r?\n/).map((s) => s.trim()).filter((s) => s && !s.startsWith("#"))
      )];
    }

    async function runImport() {
      if (busy) return;
      const exprs = parseExprs();
      if (!exprs.length) { status.textContent = ctx.t("lib.addEmpty"); return; }
      busy = true; importBtn.disabled = true; fileInput.disabled = true; textarea.disabled = true;
      progWrap.classList.remove("hidden");
      const universe = uniInput.value.trim() || undefined;
      const source = (srcInput.value.trim() || "CUSTOM").toUpperCase();
      const period = ctx.store.get("period");
      const CH = 8;
      let done = 0, saved = 0, failed = 0;
      for (let i = 0; i < exprs.length; i += CH) {
        const chunk = exprs.slice(i, i + CH);
        try {
          const res = await ctx.api.libraryBulkAdd({ exprs: chunk, universe, source, period });
          for (const r of (res && res.results) || []) { if (r.saved) saved++; else failed++; }
        } catch (_) { failed += chunk.length; }
        done += chunk.length;
        bar.style.width = Math.round((done / exprs.length) * 100) + "%";
        status.textContent = ctx.t("lib.importing", { done: Math.min(done, exprs.length), total: exprs.length, saved, failed });
      }
      status.textContent = ctx.t("lib.importDone", { saved, failed });
      importBtn.textContent = ctx.t("lib.addClose");
      importBtn.disabled = false;
      busy = false;
      loadList(); // refresh the library with the new factors
      importBtn.onclick = () => closeFn();
    }
    importBtn.addEventListener("click", () => { if (!busy) runImport(); });

    const content = el("div", { className: "lib-add" },
      el("div", { className: "muted", style: { fontSize: "12px", marginBottom: "8px" } }, ctx.t("lib.addHint")),
      el("label", { className: "label" }, ctx.t("lib.addExprLabel")),
      textarea,
      el("div", { className: "lib-add-row mt-2" },
        el("label", { className: "lib-add-field" }, el("span", { className: "label" }, ctx.t("lib.addUpload")), fileInput),
        el("label", { className: "lib-add-field" }, el("span", { className: "label" }, ctx.t("lib.addUniverse")), uniInput),
        el("label", { className: "lib-add-field" }, el("span", { className: "label" }, ctx.t("lib.addSource")), srcInput)
      ),
      progWrap,
      el("div", { className: "flex items-center justify-between mt-2" }, status, importBtn)
    );
    closeFn = ctx.lightbox({ title: ctx.t("lib.addHeading"), content, wide: false });
  }

  // ---- initial paint -------------------------------------------------------
  renderLeft();
  renderRight();
  loadList();
}

// --- module-local pure helpers ----------------------------------------------

function truncate(s, n) {
  s = String(s == null ? "" : s);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function safeJson(obj) {
  try {
    return JSON.stringify(obj, null, 2);
  } catch (_) {
    return String(obj);
  }
}

// Diverging red(+1) / white(0) / blue(-1) for correlation cells.
function divergeRWB(v) {
  const t = Math.max(-1, Math.min(1, Number(v) || 0));
  if (t >= 0) return mixRgb([255, 255, 255], [192, 57, 43], t);   // -> red
  return mixRgb([255, 255, 255], [45, 91, 227], -t);              // -> blue
}

function mixRgb(a, b, t) {
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl = Math.round(a[2] + (b[2] - a[2]) * t);
  return `rgb(${r},${g},${bl})`;
}
