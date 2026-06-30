// pages/data.js — Data Manager (operator/admin; not in the main nav, reached from
// /admin or #/data). Edit data dirs + provider credentials, see RAW↔ASSAY sync
// status per market, and run init/update pipeline jobs with a live progress/log view.
//
// Secrets are masked by the backend (••••last4); leaving a masked field unchanged
// preserves the stored key. Heavy work runs in the server's background job queue;
// this page polls the job list while mounted.

const STYLE_ID = "data-page-style";

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const css = `
.dm-page { display: flex; flex-direction: column; gap: var(--sp-4); max-width: 1100px; }
.dm-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: var(--sp-3) var(--sp-4); }
.dm-field { display: flex; flex-direction: column; gap: 3px; }
.dm-field .label { font-size: 11px; text-transform: uppercase; letter-spacing: .03em; color: var(--text-muted); }
.dm-field input { width: 100%; font-family: var(--font-mono); font-size: 13px; }
.dm-sub { font-size: 12px; color: var(--text-muted); margin: var(--sp-2) 0 var(--sp-1); font-weight: 600; }
.dm-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.dm-table th, .dm-table td { border-bottom: 1px solid var(--border); padding: 6px 10px; text-align: left; vertical-align: middle; }
.dm-table th { font-size: 11px; text-transform: uppercase; color: var(--text-muted); }
.dm-mono { font-family: var(--font-mono); }
.dm-actions { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; }
.dm-actions input[type=date] { width: 140px; }
.dm-job { border: 1px solid var(--border); border-radius: var(--radius-card); margin-bottom: var(--sp-2); overflow: hidden; }
.dm-job-head { display: flex; align-items: center; gap: var(--sp-2); cursor: pointer; padding: var(--sp-2) var(--sp-3); }
.dm-job-icon { flex: 0 0 auto; font-size: 14px; line-height: 1; }
.dm-job-title { font-family: var(--font-mono); font-size: 13px; flex: 1; }
.dm-job-body { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; padding: var(--sp-2) var(--sp-3); }
.dm-prog { height: 10px; background: var(--gray-1); border-radius: 4px; overflow: hidden; flex: 1; min-width: 160px; }
.dm-prog > span { display: block; height: 100%; background: var(--blue); transition: width .2s ease; }
.dm-pct { font-family: var(--font-mono); font-size: 12px; color: var(--text-muted); flex: 0 0 auto; }
.dm-job-msg { width: 100%; font-size: 12px; }
.dm-log { font-family: var(--font-mono); font-size: 12px; white-space: pre-wrap; max-height: 220px; overflow: auto;
  background: var(--gray-1); padding: var(--sp-2); border-radius: var(--radius-badge); margin: 0 var(--sp-3) var(--sp-3); }
.dm-st-yes { color: var(--green); font-weight: 600; } .dm-st-no { color: var(--amber); font-weight: 600; }
.dm-toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: var(--navy); color: #fff;
  padding: 8px 16px; border-radius: 8px; font-size: 13px; z-index: 50; opacity: 0; transition: opacity .15s; }
.dm-toast.on { opacity: 1; } .dm-toast.err { background: var(--red); }
`;
  const s = document.createElement("style"); s.id = STYLE_ID; s.textContent = css;
  document.head.appendChild(s);
}

export function render(root, ctx) {
  injectStyle();
  const { api, el } = ctx;
  const cleanups = [];

  const page = el("div", { className: "page dm-page" },
    el("div", { className: "page-header" },
      el("h1", { className: "page-title" }, "Data Manager"),
      el("span", { className: "page-subtitle" }, "Configure sources & credentials · check RAW↔ASSAY sync · run init/update jobs")
    )
  );
  root.replaceChildren(page);

  const configCard = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading data settings…")));
  const systemCard = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading system settings…")));
  const statusCard = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading status…")));
  const cacheCard = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading hot cache…")));
  const jobsCard = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading jobs…")));
  page.append(configCard, systemCard, statusCard, cacheCard, jobsCard);

  // ---------------- config ----------------
  function renderConfig(cfg) {
    const d = cfg.dirs || {}, s3 = cfg.massive_s3 || {}, ts = cfg.tushare || {};
    const inp = (val, type) => el("input", { className: "input", type: type || "text", value: val == null ? "" : String(val), spellcheck: "false", autocomplete: "off" });
    const f = {
      raw_massive: inp(d.raw_massive), raw_tushare: inp(d.raw_tushare),
      assay_us: inp(d.assay_us), assay_cn: inp(d.assay_cn),
      access_key_id: inp(s3.access_key_id), secret_access_key: inp(s3.secret_access_key, "password"),
      endpoint: inp(s3.endpoint), bucket: inp(s3.bucket),
      token: inp(ts.token, "password"),
    };
    const field = (label, node) => el("div", { className: "dm-field" }, el("span", { className: "label" }, label), node);
    const saveBtn = el("button", { className: "btn btn--primary", type: "button" }, "Save config");
    const note = el("span", { className: "muted", style: { fontSize: "12px", marginLeft: "8px" } }, "secrets shown masked (••••last4); leave unchanged to keep");
    saveBtn.addEventListener("click", () => {
      saveBtn.disabled = true;
      api.adminConfigPut({
        dirs: { raw_massive: f.raw_massive.value.trim(), raw_tushare: f.raw_tushare.value.trim(), assay_us: f.assay_us.value.trim(), assay_cn: f.assay_cn.value.trim() },
        massive_s3: { access_key_id: f.access_key_id.value.trim(), secret_access_key: f.secret_access_key.value, endpoint: f.endpoint.value.trim(), bucket: f.bucket.value.trim() },
        tushare: { token: f.token.value },
      }).then((m) => { renderConfig(m); loadStatus(); toast("Config saved"); })
        .catch((e) => toast("Save failed: " + (e.message || e), true))
        .finally(() => { saveBtn.disabled = false; });
    });

    configCard.replaceChildren(
      el("div", { className: "card-head" }, el("span", { className: "card-title" }, "数据设置 · Data Settings")),
      el("div", { className: "card-body" },
        el("div", { className: "dm-sub" }, "Directories"),
        el("div", { className: "dm-grid" },
          field("RAW · MASSIVE (US source)", f.raw_massive), field("RAW · Tushare (CN source)", f.raw_tushare),
          field("ASSAY · US (output)", f.assay_us), field("ASSAY · CN (output)", f.assay_cn)),
        el("div", { className: "dm-sub" }, "MASSIVE S3 (US download)"),
        el("div", { className: "dm-grid" },
          field("Access Key ID", f.access_key_id), field("Secret Access Key", f.secret_access_key),
          field("S3 Endpoint", f.endpoint), field("Bucket", f.bucket)),
        el("div", { className: "dm-sub" }, "Tushare (CN download)"),
        el("div", { className: "dm-grid" }, field("API Token", f.token)),
        el("div", { style: { marginTop: "12px" } }, saveBtn, note)
      )
    );
  }

  // ---------------- system settings ----------------
  function renderSystem(cfg) {
    const s = cfg.system || {};
    const num = (v, step) => el("input", { className: "input", type: "number", step: step || "1", value: v == null ? "" : String(v) });
    const txt = (v) => el("input", { className: "input", type: "text", value: v == null ? "" : String(v), spellcheck: "false", autocomplete: "off" });
    const sel = (v, opts) => el("select", { className: "select" }, ...opts.map((o) => el("option", { value: o, selected: String(v) === o }, o)));
    const chk = (v) => el("input", { type: "checkbox", checked: !!v });
    const field = (label, node) => el("div", { className: "dm-field" }, el("span", { className: "label" }, label), node);

    const f = {
      // 并行
      n_workers: num(s.n_workers),
      // 缓存
      l1_memory_gb: num(s.l1_memory_gb, "0.5"), l2_max_gb: num(s.l2_max_gb, "1"),
      precompute_enabled: chk(s.precompute_enabled), precompute_auto_refresh: chk(s.precompute_auto_refresh),
      precompute_top_k: num(s.precompute_top_k), precompute_min_count: num(s.precompute_min_count),
      precompute_corpus: txt(s.precompute_corpus),
      // 默认参数
      default_universe: txt(s.default_universe),
      default_period_start: el("input", { className: "input input-date", type: "date", value: s.default_period_start || "" }),
      default_period_end: el("input", { className: "input input-date", type: "date", value: s.default_period_end || "" }),
      default_horizons: txt(s.default_horizons),
      default_execution: sel(s.default_execution, ["next_open", "next_close"]),
      default_adj: sel(s.default_adj, ["split", "total", "none"]),
      default_frequency: txt(s.default_frequency),
      annualization_basis: sel(s.annualization_basis, ["daily", "bar"]),
      risk_free_rate: num(s.risk_free_rate, "0.001"),
    };
    const saveBtn = el("button", { className: "btn btn--primary", type: "button" }, "Save system settings");
    const note = el("span", { className: "muted", style: { fontSize: "12px", marginLeft: "8px" } }, "applies on the next request — no restart");
    saveBtn.addEventListener("click", () => {
      saveBtn.disabled = true;
      api.adminConfigPut({ system: {
        n_workers: Number(f.n_workers.value) || 1,
        l1_memory_gb: Number(f.l1_memory_gb.value) || 0, l2_max_gb: Number(f.l2_max_gb.value) || 0,
        precompute_enabled: f.precompute_enabled.checked, precompute_auto_refresh: f.precompute_auto_refresh.checked,
        precompute_top_k: Number(f.precompute_top_k.value) || 256, precompute_min_count: Number(f.precompute_min_count.value) || 2,
        precompute_corpus: f.precompute_corpus.value.trim(),
        default_universe: f.default_universe.value.trim(),
        default_period_start: f.default_period_start.value, default_period_end: f.default_period_end.value,
        default_horizons: f.default_horizons.value.trim(),
        default_execution: f.default_execution.value, default_adj: f.default_adj.value,
        default_frequency: f.default_frequency.value.trim(), annualization_basis: f.annualization_basis.value,
        risk_free_rate: Number(f.risk_free_rate.value) || 0,
      } }).then((m) => { renderSystem(m); toast("System settings saved"); })
        .catch((e) => toast("Save failed: " + (e.message || e), true))
        .finally(() => { saveBtn.disabled = false; });
    });

    const chkField = (label, node) => el("label", { className: "dm-field", style: { flexDirection: "row", alignItems: "center", gap: "6px" } }, node, el("span", { className: "label", style: { margin: 0 } }, label));

    systemCard.replaceChildren(
      el("div", { className: "card-head" }, el("span", { className: "card-title" }, "系统设置 · System Settings")),
      el("div", { className: "card-body" },
        el("div", { className: "dm-sub" }, "并行 · Parallelism"),
        el("div", { className: "dm-grid" }, field("Workers (batch threads)", f.n_workers)),
        el("div", { className: "dm-sub" }, "缓存 · Cache & precompute"),
        el("div", { className: "dm-grid" },
          field("L1 memory (GB)", f.l1_memory_gb), field("L2 max (GB)", f.l2_max_gb),
          field("Precompute top-K", f.precompute_top_k), field("Precompute min count", f.precompute_min_count),
          field("Precompute corpus (path; blank = library)", f.precompute_corpus)),
        el("div", { className: "dm-grid", style: { marginTop: "6px" } },
          chkField("Precompute enabled", f.precompute_enabled),
          chkField("Auto-refresh on data update", f.precompute_auto_refresh)),
        el("div", { className: "dm-sub" }, "默认参数 · Evaluation defaults"),
        el("div", { className: "dm-grid" },
          field("Default universe", f.default_universe),
          field("Period start", f.default_period_start), field("Period end", f.default_period_end),
          field("Horizons (csv)", f.default_horizons),
          field("Execution", f.default_execution), field("Adjustment", f.default_adj),
          field("Frequency", f.default_frequency), field("Annualization", f.annualization_basis),
          field("Risk-free rate", f.risk_free_rate)),
        el("div", { style: { marginTop: "12px" } }, saveBtn, note)
      )
    );
  }

  // ---------------- status + wizard ----------------
  function renderStatus(st) {
    const rows = (st.markets || []).map((m) => {
      const start = el("input", { type: "date", className: "input input-date" });
      const end = el("input", { type: "date", className: "input input-date" });
      const mkBtn = (label, mode, primary) => {
        const b = el("button", { className: "btn btn--sm" + (primary ? " btn--primary" : ""), type: "button" }, label);
        b.addEventListener("click", () => startJob(m.market, mode, start.value, end.value, b));
        return b;
      };
      return el("tr", {},
        el("td", {}, el("b", {}, m.market)),
        el("td", { className: "dm-mono" }, m.raw_latest || "—"),
        el("td", { className: "dm-mono" }, m.assay_latest || "—"),
        el("td", { className: "dm-mono" }, m.behind_days == null ? "—" : (m.behind_days + "d")),
        el("td", {}, m.in_sync ? el("span", { className: "dm-st-yes" }, "✓ in sync") : el("span", { className: "dm-st-no" }, m.initialized ? "behind" : "empty")),
        el("td", {}, el("div", { className: "dm-actions" }, start, el("span", { className: "muted" }, "–"), end,
          mkBtn("Update", "update", true), mkBtn(m.initialized ? "Re-init" : "Initialize", "init", false)))
      );
    });
    const refresh = el("button", { className: "btn btn--sm", type: "button" }, "↻ Refresh");
    refresh.addEventListener("click", () => loadStatus());
    statusCard.replaceChildren(
      el("div", { className: "card-head" },
        el("span", { className: "card-title" }, "Data status & sync"),
        refresh),
      el("div", { className: "card-body" },
        el("table", { className: "dm-table" },
          el("thead", {}, el("tr", {},
            el("th", {}, "Market"), el("th", {}, "RAW latest"), el("th", {}, "ASSAY latest"),
            el("th", {}, "Behind"), el("th", {}, "Sync"), el("th", {}, "Actions (blank dates = auto)"))),
          el("tbody", {}, ...rows)),
        el("div", { className: "muted", style: { fontSize: "12px", marginTop: "8px" } },
          "Initialize = full history · Update = incremental from last ingest. Today: " + (st.today || "—"))
      )
    );
  }

  function startJob(market, mode, start, end, btn) {
    if (mode === "init" && !window.confirm(`Initialize ${market} from full history? This downloads + ingests a lot of data.`)) return;
    if (btn) btn.disabled = true;
    api.adminJobStart({ market, mode, start: start || null, end: end || null })
      .then((j) => { toast(`Started ${mode} ${market} (${j.mode})`); loadJobs(); })
      .catch((e) => toast("Failed: " + (e.message || e), true))
      .finally(() => { if (btn) btn.disabled = false; });
  }

  // ---------------- jobs ----------------
  let expanded = null;
  function renderJobs(list) {
    const items = (list || []).map((j) => {
      const pct = Math.round((j.progress || 0) * 100);
      const statusColor = j.status === "done" ? "badge--green" : j.status === "error" ? "badge--red" : j.status === "running" ? "badge--blue" : "badge--gray";
      const icon = j.status === "done" ? "✔" : j.status === "error" ? "✖" : j.status === "running" ? "⏳" : "🗎";
      // title bar: icon · "MARKET · mode" · status badge
      const head = el("div", { className: "dm-job-head" },
        el("span", { className: "dm-job-icon" }, icon),
        el("span", { className: "dm-job-title" }, `${j.market} · ${j.mode}`),
        el("span", { className: "badge " + statusColor }, j.status)
      );
      // body: classic progress bar + percent (+ message under it)
      const body = el("div", { className: "dm-job-body" },
        el("div", { className: "dm-prog" }, el("span", { style: { width: pct + "%" } })),
        el("span", { className: "dm-pct" }, pct + "%"),
        j.message ? el("div", { className: "dm-job-msg muted" }, j.message) : null
      );
      const box = el("div", { className: "dm-job" }, head, body);
      head.addEventListener("click", () => { expanded = expanded === j.id ? null : j.id; loadJobs(); });
      if (expanded === j.id) {
        const log = el("div", { className: "dm-log" }, "loading…");
        box.append(log);
        api.adminJob(j.id).then((full) => {
          log.textContent = (full.logs || []).map((l) => "· " + l.line).join("\n") || "(no log)";
          if (full.error) log.textContent += "\n!! " + full.error;
        }).catch(() => { log.textContent = "(log unavailable)"; });
      }
      return box;
    });
    jobsCard.replaceChildren(
      el("div", { className: "card-head" }, el("span", { className: "card-title" }, "Jobs")),
      el("div", { className: "card-body" }, items.length ? el("div", {}, ...items) : el("div", { className: "muted" }, "No jobs yet. Use the status table to start one."))
    );
  }

  // ---------------- loaders + polling ----------------
  // ---------------- hot cache (precompute) ----------------
  function fmtBytes(b) { b = Number(b) || 0; return b > 1e9 ? (b / 1e9).toFixed(1) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : (b / 1e3).toFixed(0) + " KB"; }

  function renderCache(st) {
    const store = st.store || {};
    const scopes = st.scopes || [];
    const head = el("div", { className: "card-head" },
      el("span", { className: "card-title" }, "Hot cache (precompute)"),
      el("span", { className: "muted", style: { fontSize: "12px" } },
        `${store.entries || 0} entries · ${fmtBytes(store.bytes)} · auto-refreshes with daily data`));

    const body = el("div", { className: "card-body" });
    if (!scopes.length) {
      body.append(el("div", { className: "muted" }, "No precomputed sub-expressions yet. Run a data update, or rebuild below — it mines the factor library's common sub-expressions and computes them for every asset."));
    } else {
      const rows = scopes.map((s) => {
        const fresh = !!s.fresh;
        const badge = el("span", { className: "badge " + (fresh ? "badge--green" : "badge--amber") }, fresh ? "fresh" : "stale — refresh due");
        const top = (s.top || []).slice(0, 3).map((c) => `${c.expr} ×${c.count}`).join(" · ");
        const detail = el("div", { style: { padding: "0 10px 8px" } });   // expands with contents
        let open = false;
        const viewBtn = el("button", { className: "btn btn--sm", type: "button" }, "▸ Contents");
        viewBtn.addEventListener("click", () => {
          open = !open;
          viewBtn.textContent = (open ? "▾" : "▸") + " Contents";
          if (!open) { detail.replaceChildren(); return; }
          detail.replaceChildren(el("div", { className: "muted" }, "Loading cache contents…"));
          api.adminCacheEntries(s.universe)
            .then((res) => detail.replaceChildren(buildCacheEntries(res)))
            .catch((e) => detail.replaceChildren(el("div", { className: "error-state" }, "Contents unavailable: " + (e.message || e))));
        });
        return el("div", { className: "dm-job" },
          el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px", padding: "8px 10px" } },
            el("div", {},
              el("div", { style: { fontWeight: 600 } }, `${s.universe} `, badge),
              el("div", { className: "muted", style: { fontSize: "12px", fontFamily: "var(--font-mono)" } },
                `valid ${(s.period || []).join(" .. ")} · as-of ${s.as_of || "—"} · ${s.n_entries || 0} subexprs · built ${(s.built_at || "").slice(0, 16).replace("T", " ")}`),
              top ? el("div", { className: "muted", style: { fontSize: "11px" } }, "top: " + top) : null,
              (!fresh && s.current_data_latest) ? el("div", { style: { fontSize: "11px", color: "var(--amber)" } }, `data advanced to ${s.current_data_latest} since build`) : null),
            viewBtn),
          detail);
      });
      body.append(...rows);
    }

    const usBtn = el("button", { className: "btn btn--sm", type: "button", onClick: () => rebuild("US") }, "Rebuild US");
    const cnBtn = el("button", { className: "btn btn--sm", type: "button", onClick: () => rebuild("CN") }, "Rebuild CN");
    body.append(el("div", { style: { marginTop: "10px", display: "flex", gap: "8px" } }, usBtn, cnBtn));
    cacheCard.replaceChildren(head, body);
  }

  function buildCacheEntries(res) {
    const ents = res.entries || [];
    const wrap = el("div", {});
    wrap.append(el("div", { className: "muted", style: { fontSize: "12px", margin: "4px 0" } },
      `${res.count || 0} cached sub-expressions · ${fmtBytes(res.bytes)} · fingerprint ${(res.fingerprint || "—").slice(0, 12)}`));
    if (!ents.length) { wrap.append(el("div", { className: "muted" }, "No recorded contents (rebuild to populate).")); return wrap; }
    const th = (t) => el("th", {}, t);
    const head = el("thead", {}, el("tr", {}, th("Sub-expression"), th("×count"), th("factors"), th("nodes"), th("saved"), th("shape (T×N)"), th("size"), th("coverage"), th("on disk")));
    const tb = el("tbody", {});
    for (const e of ents.slice(0, 300)) {
      const shape = Array.isArray(e.shape) ? e.shape.join("×") : "—";
      tb.append(el("tr", {},
        el("td", { className: "dm-mono", title: e.expr, style: { maxWidth: "360px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, e.expr),
        el("td", { className: "dm-mono" }, String(e.count)),
        el("td", { className: "dm-mono" }, String(e.n_factors)),
        el("td", { className: "dm-mono" }, String(e.n_nodes)),
        el("td", { className: "dm-mono" }, String(e.score)),
        el("td", { className: "dm-mono" }, shape),
        el("td", { className: "dm-mono" }, fmtBytes(e.bytes)),
        el("td", { className: "dm-mono" }, Number.isFinite(e.coverage) ? (e.coverage * 100).toFixed(0) + "%" : "—"),
        el("td", {}, e.present ? el("span", { className: "dm-st-yes" }, "✓") : el("span", { className: "dm-st-no" }, "missing"))));
    }
    wrap.append(el("div", { style: { overflow: "auto", maxHeight: "360px", marginTop: "4px" } }, el("table", { className: "dm-table" }, head, tb)));
    if (ents.length > 300) wrap.append(el("div", { className: "muted", style: { fontSize: "11px" } }, `showing top 300 of ${ents.length}`));
    return wrap;
  }

  function rebuild(market) {
    api.adminCacheRebuild(market)
      .then(() => { toast(`hot-cache rebuild (${market}) queued`); loadJobs(); })
      .catch((e) => toast("rebuild failed: " + (e.message || e), true));
  }

  function loadConfig() {
    api.adminConfigGet()
      .then((cfg) => { renderConfig(cfg); renderSystem(cfg); })
      .catch((e) => {
        const msg = "Config unavailable: " + (e.message || e);
        configCard.replaceChildren(el("div", { className: "card-body error-state" }, msg));
        systemCard.replaceChildren(el("div", { className: "card-body error-state" }, msg));
      });
  }
  function loadStatus() { api.adminDataStatus().then(renderStatus).catch((e) => statusCard.replaceChildren(el("div", { className: "card-body error-state" }, "Status unavailable: " + (e.message || e)))); }
  function loadCache() { api.adminCacheStatus().then(renderCache).catch((e) => cacheCard.replaceChildren(el("div", { className: "card-body error-state" }, "Hot cache unavailable: " + (e.message || e)))); }
  function loadJobs() { api.adminJobs().then((r) => renderJobs(r.jobs || [])).catch(() => {}); }

  loadConfig(); loadStatus(); loadCache(); loadJobs();
  let _tick = 0;
  const timer = setInterval(() => { loadJobs(); if (++_tick % 5 === 0) loadCache(); }, 2000);  // refresh cache view ~every 10s
  cleanups.push(() => clearInterval(timer));

  function toast(msg, err) {
    let n = document.getElementById("dm-toast");
    if (!n) { n = el("div", { id: "dm-toast", className: "dm-toast" }); document.body.appendChild(n); }
    n.className = "dm-toast" + (err ? " err" : ""); n.textContent = msg;
    void n.offsetWidth; n.classList.add("on");
    clearTimeout(n._t); n._t = setTimeout(() => n.classList.remove("on"), 2600);
  }

  return () => cleanups.forEach((fn) => { try { fn(); } catch (_) {} });
}
