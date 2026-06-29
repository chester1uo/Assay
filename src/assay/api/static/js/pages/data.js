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

  const configCard = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading config…")));
  const statusCard = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading status…")));
  const jobsCard = el("section", { className: "card" }, el("div", { className: "card-body" }, el("div", { className: "muted" }, "Loading jobs…")));
  page.append(configCard, statusCard, jobsCard);

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
      el("div", { className: "card-head" }, el("span", { className: "card-title" }, "Configuration")),
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
  function loadConfig() { api.adminConfigGet().then(renderConfig).catch((e) => configCard.replaceChildren(el("div", { className: "card-body error-state" }, "Config unavailable: " + (e.message || e)))); }
  function loadStatus() { api.adminDataStatus().then(renderStatus).catch((e) => statusCard.replaceChildren(el("div", { className: "card-body error-state" }, "Status unavailable: " + (e.message || e)))); }
  function loadJobs() { api.adminJobs().then((r) => renderJobs(r.jobs || [])).catch(() => {}); }

  loadConfig(); loadStatus(); loadJobs();
  const timer = setInterval(loadJobs, 2000);
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
