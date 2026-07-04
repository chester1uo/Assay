// pages/data.js — Data Manager (operator/admin; reached from /admin or #/data).
//
// A left-tab dashboard rather than one long scroll:
//   · Data Status  — per-market cards: latest date, sync, trading days, size, dir
//   · Keys & Dirs  — data dirs + provider credentials + "test connection"
//   · Cache        — hot-cache (precompute) status + rebuild
//   · Data Setup   — init/update jobs, auto-update schedule, live job log
//   · System       — parallelism / cache budgets / evaluation defaults
//
// Secrets are masked by the backend (••••last4); leaving a masked field unchanged
// preserves the stored key. Heavy work runs in the server's background job queue.

const STYLE_ID = "data-page-style";

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const css = `
.dm-page { display: flex; flex-direction: column; gap: var(--sp-4); }
.dm-layout { display: grid; grid-template-columns: 210px minmax(0,1fr); gap: var(--sp-4); align-items: start; }
.dm-nav { display: flex; flex-direction: column; gap: 4px; position: sticky; top: var(--sp-4); }
.dm-tab { text-align: left; padding: 9px 12px; border-radius: 9px; border: 1px solid transparent;
  background: none; cursor: pointer; font-size: 13px; color: var(--text-muted); display: flex; align-items: center; gap: 8px; }
.dm-tab:hover { background: var(--gray-1); color: var(--text); }
.dm-tab.is-active { background: var(--blue, #2D5BE3); color: #fff; font-weight: 600; }
.dm-tab .dm-dot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; background: var(--gray-3, #cbd2dd); }
.dm-content { display: flex; flex-direction: column; gap: var(--sp-4); min-width: 0; }
.dm-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px,1fr)); gap: var(--sp-3); }
.dm-statcard { border: 1px solid var(--border); border-radius: var(--radius-card); padding: var(--sp-3); background: var(--surface, #fff); }
.dm-statcard-h { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.dm-statcard-h b { font-size: 15px; }
.dm-kv { display: grid; grid-template-columns: auto 1fr; gap: 5px 12px; font-size: 13px; }
.dm-kv .k { color: var(--text-muted); } .dm-kv .v { text-align: right; font-family: var(--font-mono); }
.dm-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: var(--sp-3) var(--sp-4); }
.dm-field { display: flex; flex-direction: column; gap: 3px; }
.dm-field .label { font-size: 11px; text-transform: uppercase; letter-spacing: .03em; color: var(--text-muted); }
.dm-field input, .dm-field select { width: 100%; font-family: var(--font-mono); font-size: 13px; }
.dm-sub { font-size: 12px; color: var(--text-muted); margin: var(--sp-2) 0 var(--sp-1); font-weight: 600; display: flex; align-items: center; justify-content: space-between; }
.dm-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.dm-table th, .dm-table td { border-bottom: 1px solid var(--border); padding: 6px 10px; text-align: left; vertical-align: middle; }
.dm-table th { font-size: 11px; text-transform: uppercase; color: var(--text-muted); }
.dm-mono { font-family: var(--font-mono); }
.dm-actions { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; }
.dm-actions input[type=date], .dm-actions input[type=time] { width: 140px; }
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
.dm-test { margin-top: 6px; font-size: 12px; border: 1px solid var(--border); border-radius: var(--radius-badge); padding: 6px 8px; background: var(--gray-1); }
.dm-toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: var(--navy); color: #fff;
  padding: 8px 16px; border-radius: 8px; font-size: 13px; z-index: 50; opacity: 0; transition: opacity .15s; }
.dm-toast.on { opacity: 1; } .dm-toast.err { background: var(--red); }
@media (max-width: 720px) { .dm-layout { grid-template-columns: 1fr; } .dm-nav { flex-direction: row; overflow-x: auto; position: static; } }
`;
  const s = document.createElement("style"); s.id = STYLE_ID; s.textContent = css;
  document.head.appendChild(s);
}

export function render(root, ctx) {
  injectStyle();
  const { api, el } = ctx;
  const cleanups = [];
  let poller = null;               // interval for the active tab (cleared on switch)
  const stopPoller = () => { if (poller) { clearInterval(poller); poller = null; } };

  const page = el("div", { className: "page dm-page" },
    el("div", { className: "page-header" },
      el("h1", { className: "page-title" }, "Data Manager"),
      el("span", { className: "page-subtitle" }, "Configure sources & credentials · check RAW↔ASSAY sync · run init/update jobs")
    )
  );

  const content = el("div", { className: "dm-content" });
  const TABS = [
    { id: "status", label: "数据状态 · Data Status", mount: mountStatus },
    { id: "keys", label: "密钥与目录 · Keys & Dirs", mount: mountKeys },
    { id: "cache", label: "缓存管理 · Cache", mount: mountCache },
    { id: "setup", label: "数据初始化 · Data Setup", mount: mountSetup },
    { id: "system", label: "系统设置 · System", mount: mountSystem },
  ];
  const navBtns = {};
  const nav = el("nav", { className: "dm-nav" },
    ...TABS.map((t) => {
      const b = el("button", { className: "dm-tab", type: "button" }, el("span", { className: "dm-dot" }), t.label);
      b.addEventListener("click", () => switchTab(t.id));
      navBtns[t.id] = b;
      return b;
    }));
  page.append(el("div", { className: "dm-layout" }, nav, content));
  root.replaceChildren(page);

  function switchTab(id) {
    stopPoller();
    for (const [k, b] of Object.entries(navBtns)) b.classList.toggle("is-active", k === id);
    content.replaceChildren();
    (TABS.find((t) => t.id === id) || TABS[0]).mount(content);
  }
  switchTab("status");
  cleanups.push(stopPoller);

  // ================================================================ Data Status
  function mountStatus(box) {
    const grid = el("div", { className: "dm-cards" }, el("div", { className: "muted" }, "Loading status…"));
    const refresh = el("button", { className: "btn btn--sm", type: "button" }, "↻ Refresh");
    refresh.addEventListener("click", () => load());
    box.append(el("section", { className: "card" },
      el("div", { className: "card-head" }, el("span", { className: "card-title" }, "Data status & sync"), refresh),
      el("div", { className: "card-body" }, grid)));

    function load() {
      Promise.all([api.adminDataStatus(), api.adminDataUsage().catch(() => ({ markets: [] }))])
        .then(([st, usage]) => renderStatusCards(grid, st, usage))
        .catch((e) => grid.replaceChildren(el("div", { className: "error-state" }, "Status unavailable: " + (e.message || e))));
    }
    load();
  }

  function renderStatusCards(grid, st, usage) {
    const usageBy = {};
    for (const u of (usage.markets || [])) usageBy[u.market] = u;
    const dirStyle = { fontSize: "11px", marginTop: "4px", fontFamily: "var(--font-mono)", color: "var(--text-muted)", wordBreak: "break-all" };
    const kvrows = (pairs) => el("div", { className: "dm-kv" },
      ...pairs.flatMap(([k, v]) => [el("span", { className: "k" }, k), el("span", { className: "v" }, v)]));
    const size = (b, n) => (b != null ? fmtBytes(b) + ` · ${n || 0} files` : "—");

    const cards = (st.markets || []).map((m) => {
      const u = usageBy[m.market] || {};
      const sync = m.in_sync
        ? el("span", { className: "badge badge--green" }, "✓ in sync")
        : el("span", { className: "badge badge--amber" }, m.initialized ? "behind" : "empty");
      const rawBlk = el("div", { style: { marginTop: "6px" } },
        el("div", { className: "dm-sub" }, "RAW · source"),
        kvrows([["Latest", m.raw_latest || "—"], ["Size", size(u.raw_bytes, u.raw_files)]]),
        el("div", { style: dirStyle }, m.raw_dir || u.raw_dir || ""));
      const assayBlk = el("div", { style: { marginTop: "10px" } },
        el("div", { className: "dm-sub" }, "ASSAY · store"),
        kvrows([["Latest", m.assay_latest || "—"], ["Trading days", m.trading_days != null ? String(m.trading_days) : "—"], ["Size", size(u.bytes, u.files)]]),
        el("div", { style: dirStyle }, m.assay_dir || u.assay_dir || ""));
      return el("div", { className: "dm-statcard" },
        el("div", { className: "dm-statcard-h" }, el("b", {}, m.market),
          el("span", { className: "dm-actions" },
            m.behind_days != null && m.behind_days > 0 ? el("span", { className: "muted", style: { fontSize: "12px" } }, `${m.behind_days}d behind`) : null,
            sync)),
        rawBlk, assayBlk);
    });
    grid.replaceChildren(...(cards.length ? cards : [el("div", { className: "muted" }, "No markets configured.")]),
      el("div", { className: "muted", style: { gridColumn: "1/-1", fontSize: "12px" } }, "Today: " + (st.today || "—")));
  }

  // ================================================================ Keys & Dirs
  function mountKeys(box) {
    const card = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading…")));
    box.append(card);
    api.adminConfigGet().then((cfg) => renderConfig(card, cfg))
      .catch((e) => card.replaceChildren(el("div", { className: "card-body error-state" }, "Config unavailable: " + (e.message || e))));
  }

  function renderConfig(card, cfg) {
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

    // test-connection badges
    const massiveTest = el("div", {}), tushareTest = el("div", {});
    const testBtn = (label, provider, box) => {
      const b = el("button", { className: "btn btn--sm", type: "button" }, label);
      b.addEventListener("click", () => {
        b.disabled = true; box.replaceChildren(el("div", { className: "dm-test muted" }, "Testing…"));
        api.adminTestConnection(provider)
          .then((r) => box.replaceChildren(renderTestResult(r)))
          .catch((e) => box.replaceChildren(el("div", { className: "dm-test" }, "✖ " + (e.message || e))))
          .finally(() => { b.disabled = false; });
      });
      return b;
    };

    const saveBtn = el("button", { className: "btn btn--primary", type: "button" }, "Save config");
    const note = el("span", { className: "muted", style: { fontSize: "12px", marginLeft: "8px" } }, "secrets shown masked (••••last4); leave unchanged to keep");
    saveBtn.addEventListener("click", () => {
      saveBtn.disabled = true;
      api.adminConfigPut({
        dirs: { raw_massive: f.raw_massive.value.trim(), raw_tushare: f.raw_tushare.value.trim(), assay_us: f.assay_us.value.trim(), assay_cn: f.assay_cn.value.trim() },
        massive_s3: { access_key_id: f.access_key_id.value.trim(), secret_access_key: f.secret_access_key.value, endpoint: f.endpoint.value.trim(), bucket: f.bucket.value.trim() },
        tushare: { token: f.token.value },
      }).then((m) => { renderConfig(card, m); toast("Config saved"); })
        .catch((e) => toast("Save failed: " + (e.message || e), true))
        .finally(() => { saveBtn.disabled = false; });
    });

    card.replaceChildren(
      el("div", { className: "card-head" }, el("span", { className: "card-title" }, "数据设置 · Data Settings")),
      el("div", { className: "card-body" },
        el("div", { className: "dm-sub" }, "Directories"),
        el("div", { className: "dm-grid" },
          field("RAW · MASSIVE (US source)", f.raw_massive), field("RAW · Tushare (CN source)", f.raw_tushare),
          field("ASSAY · US (output)", f.assay_us), field("ASSAY · CN (output)", f.assay_cn)),
        el("div", { className: "dm-sub" }, el("span", {}, "MASSIVE S3 (US download)"), testBtn("Test connection", "massive", massiveTest)),
        el("div", { className: "dm-grid" },
          field("Access Key ID", f.access_key_id), field("Secret Access Key", f.secret_access_key),
          field("S3 Endpoint", f.endpoint), field("Bucket", f.bucket)),
        massiveTest,
        el("div", { className: "dm-sub" }, el("span", {}, "Tushare (CN download)"), testBtn("Test token", "tushare", tushareTest)),
        el("div", { className: "dm-grid" }, field("API Token", f.token)),
        tushareTest,
        el("div", { style: { marginTop: "12px" } }, saveBtn, note)
      )
    );
  }

  function renderTestResult(r) {
    if (!r.ok) return el("div", { className: "dm-test" }, "✖ " + (r.error || "failed"));
    if (r.provider === "massive") {
      const req = (r.datasets || []).filter((x) => x.required).map((x) => x.name);
      const miss = r.missing_required || [];
      return el("div", { className: "dm-test" },
        el("span", { className: "dm-st-yes" }, "✓ connected"),
        ` · ${r.count || 0} datasets` + (req.length ? ` · required: ${req.join(", ")}` : "")
          + (miss.length ? ` · missing: ${miss.join(", ")}` : ""));
    }
    return el("div", { className: "dm-test" }, el("span", { className: "dm-st-yes" }, "✓ token valid"), ` · ${r.rows || 0} rows`);
  }

  // ================================================================ Cache
  function mountCache(box) {
    const card = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading hot cache…")));
    box.append(card);
    let lastJson = null;
    const load = () => api.adminCacheStatus().then((st) => {
      const j = JSON.stringify(st);
      if (j === lastJson) return;   // unchanged → skip re-render (no flash, keeps expanded panels)
      lastJson = j;
      renderCache(card, st);
    }).catch((e) => card.replaceChildren(el("div", { className: "card-body error-state" }, "Hot cache unavailable: " + (e.message || e))));
    load();
    poller = setInterval(load, 10000);
  }

  function fmtBytes(b) { b = Number(b) || 0; return b > 1e9 ? (b / 1e9).toFixed(1) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : (b / 1e3).toFixed(0) + " KB"; }

  function renderCache(card, st) {
    const store = st.store || {};
    const scopes = st.scopes || [];
    const head = el("div", { className: "card-head" },
      el("span", { className: "card-title" }, "Hot cache (precompute)"),
      el("span", { className: "muted", style: { fontSize: "12px" } },
        `${store.entries || 0} entries · ${fmtBytes(store.bytes)} · auto-refreshes with daily data`));
    const body = el("div", { className: "card-body" });
    if (!scopes.length) {
      body.append(el("div", { className: "muted" }, "No precomputed sub-expressions yet. Run a data update, or rebuild below."));
    } else {
      body.append(...scopes.map((s) => {
        const fresh = !!s.fresh;
        const badge = el("span", { className: "badge " + (fresh ? "badge--green" : "badge--amber") }, fresh ? "fresh" : "stale — refresh due");
        const top = (s.top || []).slice(0, 3).map((c) => `${c.expr} ×${c.count}`).join(" · ");
        const detail = el("div", { style: { padding: "0 10px 8px" } });
        let open = false;
        const viewBtn = el("button", { className: "btn btn--sm", type: "button" }, "▸ Contents");
        viewBtn.addEventListener("click", () => {
          open = !open; viewBtn.textContent = (open ? "▾" : "▸") + " Contents";
          if (!open) { detail.replaceChildren(); return; }
          detail.replaceChildren(el("div", { className: "muted" }, "Loading cache contents…"));
          api.adminCacheEntries(s.universe).then((res) => detail.replaceChildren(buildCacheEntries(res)))
            .catch((e) => detail.replaceChildren(el("div", { className: "error-state" }, "Contents unavailable: " + (e.message || e))));
        });
        return el("div", { className: "dm-job" },
          el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px", padding: "8px 10px" } },
            el("div", {},
              el("div", { style: { fontWeight: 600 } }, `${s.universe} `, badge),
              el("div", { className: "muted", style: { fontSize: "12px", fontFamily: "var(--font-mono)" } },
                `valid ${(s.period || []).join(" .. ")} · as-of ${s.as_of || "—"} · ${s.n_entries || 0} subexprs`),
              top ? el("div", { className: "muted", style: { fontSize: "11px" } }, "top: " + top) : null),
            viewBtn),
          detail);
      }));
    }
    body.append(el("div", { style: { marginTop: "10px", display: "flex", gap: "8px" } },
      el("button", { className: "btn btn--sm", type: "button", onClick: () => rebuild("US") }, "Rebuild US"),
      el("button", { className: "btn btn--sm", type: "button", onClick: () => rebuild("CN") }, "Rebuild CN")));
    card.replaceChildren(head, body);
  }

  function buildCacheEntries(res) {
    const ents = res.entries || [];
    const wrap = el("div", {});
    wrap.append(el("div", { className: "muted", style: { fontSize: "12px", margin: "4px 0" } },
      `${res.count || 0} cached sub-expressions · ${fmtBytes(res.bytes)} · fingerprint ${(res.fingerprint || "—").slice(0, 12)}`));
    if (!ents.length) { wrap.append(el("div", { className: "muted" }, "No recorded contents (rebuild to populate).")); return wrap; }
    const th = (t) => el("th", {}, t);
    const head = el("thead", {}, el("tr", {}, th("Sub-expression"), th("×count"), th("nodes"), th("shape (T×N)"), th("size"), th("on disk")));
    const tb = el("tbody", {});
    for (const e of ents.slice(0, 300)) {
      const shape = Array.isArray(e.shape) ? e.shape.join("×") : "—";
      tb.append(el("tr", {},
        el("td", { className: "dm-mono", title: e.expr, style: { maxWidth: "360px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, e.expr),
        el("td", { className: "dm-mono" }, String(e.count)),
        el("td", { className: "dm-mono" }, String(e.n_nodes)),
        el("td", { className: "dm-mono" }, shape),
        el("td", { className: "dm-mono" }, fmtBytes(e.bytes)),
        el("td", {}, e.present ? el("span", { className: "dm-st-yes" }, "✓") : el("span", { className: "dm-st-no" }, "missing"))));
    }
    wrap.append(el("div", { style: { overflow: "auto", maxHeight: "360px", marginTop: "4px" } }, el("table", { className: "dm-table" }, head, tb)));
    return wrap;
  }

  function rebuild(market) {
    api.adminCacheRebuild(market).then(() => toast(`hot-cache rebuild (${market}) queued`))
      .catch((e) => toast("rebuild failed: " + (e.message || e), true));
  }

  // ================================================================ Data Setup
  function mountSetup(box) {
    const stepsCard = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading markets…")));
    const schedCard = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading schedule…")));
    // Jobs card is built ONCE; the poll updates existing nodes in place (no flash).
    const jobsList = el("div", {});
    const jobsEmpty = el("div", { className: "muted" }, "No jobs yet. Use Initialize & update above.");
    jobsList.append(jobsEmpty);
    const jobsCard = el("section", { className: "card" },
      el("div", { className: "card-head" }, el("span", { className: "card-title" }, "Jobs")),
      el("div", { className: "card-body" }, jobsList));
    box.append(stepsCard, schedCard, jobsCard);

    api.adminDataStatus().then((st) => renderSetupSteps(stepsCard, st)).catch(() => {});
    api.adminScheduleGet().then((sc) => renderSchedule(schedCard, sc)).catch(() => {});

    // ---- in-place job list (keyed by id) — updated each poll, never rebuilt ----
    const nodes = new Map();   // id -> refs to the mutable sub-elements
    let expanded = null;

    function createJobNode(j) {
      const icon = el("span", { className: "dm-job-icon" });
      const title = el("span", { className: "dm-job-title" });
      const badge = el("span", { className: "badge" });
      const head = el("div", { className: "dm-job-head" }, icon, title, badge);
      const progSpan = el("span", {});
      const pct = el("span", { className: "dm-pct" });
      const msg = el("div", { className: "dm-job-msg muted" });
      const body = el("div", { className: "dm-job-body" }, el("div", { className: "dm-prog" }, progSpan), pct, msg);
      const log = el("div", { className: "dm-log" });
      log.style.display = "none";
      const root = el("div", { className: "dm-job" }, head, body, log);
      const ref = { root, icon, title, badge, progSpan, pct, msg, log };
      head.addEventListener("click", () => {
        expanded = expanded === j.id ? null : j.id;
        const open = expanded === j.id;
        ref.log.style.display = open ? "" : "none";
        if (open) { if (!ref.log.textContent) ref.log.textContent = "loading…"; refreshLog(j.id, ref); }
      });
      return ref;
    }

    function updateJobNode(ref, j) {
      const pctv = Math.round((j.progress || 0) * 100);
      const icon = j.status === "done" ? "✔" : j.status === "error" ? "✖" : j.status === "running" ? "⏳" : "🗎";
      const cls = "badge " + (j.status === "done" ? "badge--green" : j.status === "error" ? "badge--red" : j.status === "running" ? "badge--blue" : "badge--gray");
      const t = `${j.market} · ${j.mode}`, m = j.message || "", p = pctv + "%";
      if (ref.icon.textContent !== icon) ref.icon.textContent = icon;      // write only on change
      if (ref.title.textContent !== t) ref.title.textContent = t;
      if (ref.badge.className !== cls) ref.badge.className = cls;
      if (ref.badge.textContent !== j.status) ref.badge.textContent = j.status;
      ref.progSpan.style.width = p;                                        // CSS-animated, no flash
      if (ref.pct.textContent !== p) ref.pct.textContent = p;
      if (ref.msg.textContent !== m) ref.msg.textContent = m;
      ref.msg.style.display = m ? "" : "none";
      const open = expanded === j.id;
      ref.log.style.display = open ? "" : "none";
      if (open) refreshLog(j.id, ref);
    }

    function refreshLog(id, ref) {
      api.adminJob(id).then((full) => {
        let txt = (full.logs || []).map((l) => "· " + l.line).join("\n") || "(no log)";
        if (full.error) txt += "\n!! " + full.error;
        if (ref.log.textContent !== txt) ref.log.textContent = txt;       // update text in place
      }).catch(() => { if (!ref.log.textContent || ref.log.textContent === "loading…") ref.log.textContent = "(log unavailable)"; });
    }

    function syncJobs(list) {
      list = list || [];
      jobsEmpty.style.display = list.length ? "none" : "";
      const seen = new Set();
      for (const j of list) {
        seen.add(j.id);
        let ref = nodes.get(j.id);
        if (!ref) { ref = createJobNode(j); nodes.set(j.id, ref); }
        updateJobNode(ref, j);
        jobsList.appendChild(ref.root);   // reorder existing node (moves, never recreates)
      }
      for (const [id, ref] of nodes) if (!seen.has(id)) { ref.root.remove(); nodes.delete(id); }
      jobsList.appendChild(jobsEmpty);     // keep the empty hint last
    }

    const loadJobs = () => api.adminJobs().then((r) => syncJobs(r.jobs || [])).catch(() => {});
    loadJobs();
    poller = setInterval(loadJobs, 2000);
  }

  function renderSetupSteps(card, st) {
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
        el("td", {}, m.initialized ? el("span", { className: "dm-st-yes" }, "initialized") : el("span", { className: "dm-st-no" }, "empty")),
        el("td", { className: "dm-mono" }, m.assay_latest || "—"),
        el("td", { className: "dm-mono" }, m.behind_days == null ? "—" : m.behind_days + "d"),
        el("td", {}, el("div", { className: "dm-actions" }, start, el("span", { className: "muted" }, "–"), end,
          mkBtn("Update", "update", true),
          mkBtn("Ingest RAW→ASSAY", "ingest", false),
          mkBtn(m.initialized ? "Re-init" : "Initialize", "init", false))));
    });
    card.replaceChildren(
      el("div", { className: "card-head" }, el("span", { className: "card-title" }, "Initialize & update")),
      el("div", { className: "card-body" },
        el("table", { className: "dm-table" },
          el("thead", {}, el("tr", {}, el("th", {}, "Market"), el("th", {}, "State"), el("th", {}, "ASSAY latest"), el("th", {}, "Behind"), el("th", {}, "Actions (blank dates = auto)"))),
          el("tbody", {}, ...rows)),
        el("div", { className: "muted", style: { fontSize: "12px", marginTop: "8px" } },
          "Initialize = full history · Update = download + ingest · Ingest RAW→ASSAY = re-run ingest only (raw already downloaded).")));
  }

  function renderSchedule(card, sc) {
    const rows = (sc.markets || []).map((m) => {
      const chk = el("input", { type: "checkbox", checked: !!m.enabled });
      const time = el("input", { type: "time", className: "input", value: m.time || "18:30" });
      return { market: m.market, chk, time,
        node: el("tr", {},
          el("td", {}, el("b", {}, m.market)),
          el("td", {}, el("label", { className: "dm-actions" }, chk, el("span", { className: "muted" }, "auto-update"))),
          el("td", {}, time),
          el("td", { className: "dm-mono muted" }, m.next_run ? "next " + m.next_run.replace("T", " ") : "—"),
          el("td", { className: "dm-mono muted" }, m.last_run ? "last " + m.last_run : "—")) };
    });
    const saveBtn = el("button", { className: "btn btn--primary btn--sm", type: "button" }, "Save schedule");
    saveBtn.addEventListener("click", () => {
      saveBtn.disabled = true;
      const patch = {};
      for (const r of rows) patch[r.market.toLowerCase()] = { enabled: r.chk.checked, time: r.time.value };
      api.adminSchedulePut(patch).then((s) => { renderSchedule(card, s); toast("Schedule saved"); })
        .catch((e) => toast("Save failed: " + (e.message || e), true))
        .finally(() => { saveBtn.disabled = false; });
    });
    card.replaceChildren(
      el("div", { className: "card-head" }, el("span", { className: "card-title" }, "Auto-update schedule"),
        el("span", { className: "muted", style: { fontSize: "12px" } }, sc.running ? "job running…" : "idle")),
      el("div", { className: "card-body" },
        el("table", { className: "dm-table" },
          el("thead", {}, el("tr", {}, el("th", {}, "Market"), el("th", {}, "Enabled"), el("th", {}, "Time (daily)"), el("th", {}, "Next"), el("th", {}, "Last"))),
          el("tbody", {}, ...rows.map((r) => r.node))),
        el("div", { style: { marginTop: "10px" } }, saveBtn,
          el("span", { className: "muted", style: { fontSize: "12px", marginLeft: "8px" } }, "runs an incremental update at the given local time"))));
  }

  function startJob(market, mode, start, end, btn) {
    if (mode === "init" && !window.confirm(`Initialize ${market} from full history? This downloads + ingests a lot of data.`)) return;
    if (btn) btn.disabled = true;
    api.adminJobStart({ market, mode, start: start || null, end: end || null })
      .then((j) => toast(`Started ${mode} ${market}`))
      .catch((e) => toast("Failed: " + (e.message || e), true))
      .finally(() => { if (btn) btn.disabled = false; });
  }

  // ================================================================ System
  function mountSystem(box) {
    const card = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading system settings…")));
    box.append(card);
    api.adminConfigGet().then((cfg) => renderSystem(card, cfg))
      .catch((e) => card.replaceChildren(el("div", { className: "card-body error-state" }, "System settings unavailable: " + (e.message || e))));
  }

  function renderSystem(card, cfg) {
    const s = cfg.system || {};
    const num = (v, step) => el("input", { className: "input", type: "number", step: step || "1", value: v == null ? "" : String(v) });
    const txt = (v) => el("input", { className: "input", type: "text", value: v == null ? "" : String(v), spellcheck: "false", autocomplete: "off" });
    const sel = (v, opts) => el("select", { className: "select" }, ...opts.map((o) => el("option", { value: o, selected: String(v) === o }, o)));
    const chk = (v) => el("input", { type: "checkbox", checked: !!v });
    const field = (label, node) => el("div", { className: "dm-field" }, el("span", { className: "label" }, label), node);
    const chkField = (label, node) => el("label", { className: "dm-field", style: { flexDirection: "row", alignItems: "center", gap: "6px" } }, node, el("span", { className: "label", style: { margin: 0 } }, label));

    const f = {
      n_workers: num(s.n_workers),
      l1_memory_gb: num(s.l1_memory_gb, "0.5"), l2_max_gb: num(s.l2_max_gb, "1"),
      precompute_enabled: chk(s.precompute_enabled), precompute_auto_refresh: chk(s.precompute_auto_refresh),
      precompute_top_k: num(s.precompute_top_k), precompute_min_count: num(s.precompute_min_count),
      precompute_corpus: txt(s.precompute_corpus),
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
      } }).then((m) => { renderSystem(card, m); toast("System settings saved"); })
        .catch((e) => toast("Save failed: " + (e.message || e), true))
        .finally(() => { saveBtn.disabled = false; });
    });

    card.replaceChildren(
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
        el("div", { style: { marginTop: "12px" } }, saveBtn,
          el("span", { className: "muted", style: { fontSize: "12px", marginLeft: "8px" } }, "applies on the next request — no restart"))));
  }

  // ================================================================ toast
  function toast(msg, err) {
    let n = document.getElementById("dm-toast");
    if (!n) { n = el("div", { id: "dm-toast", className: "dm-toast" }); document.body.appendChild(n); }
    n.className = "dm-toast" + (err ? " err" : ""); n.textContent = msg;
    void n.offsetWidth; n.classList.add("on");
    clearTimeout(n._t); n._t = setTimeout(() => n.classList.remove("on"), 2600);
  }

  return () => cleanups.forEach((fn) => { try { fn(); } catch (_) {} });
}
