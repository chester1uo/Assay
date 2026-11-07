// doc.js — the Assay Doc Hub at /doc/. Pages are pre-rendered from docs/ by
// scripts/build_doc_hub.py into /doc/content/<lang>/<slug>.html; manifest.json holds
// the navigation, titles, headings and dates. Paths: /doc/ (home), /doc/<slug>#<heading>.

const BASE = "/doc/";
const LANG_KEY = "assay_lang";
const THEME_KEY = "qfv-theme";

const T = {
  en: {
    home: "Home", openApp: "Open Assay", searchDocs: "Search docs", searchPlaceholder: "Search the docs",
    noResults: "No matches", onThisPage: "On this page", updated: "Updated", prev: "Previous", next: "Next",
    otherLang: "This page is not translated yet; showing the Chinese version.",
    notFound: "Page not found", notFoundHint: "The page may have moved. Search, or start from the home page.",
    loadFailed: "Could not load this page.", copy: "Copy", copied: "Copied",
    eyebrow: "Assay / Developer documentation",
    hero: "Research factors on point-in-time data.",
    lede: "How to write and score factor expressions, what the US data from QFinVerse holds, and how to call Assay from the REST API, Python and MCP.",
    start: "Get started", pages: (n) => `${n} pages`, allDocs: "All documentation",
    themeSystem: "System theme", themeLight: "Light", themeDark: "Dark", langOther: "中文", langTitle: "切换到中文",
    cards: {
      "guide/getting-started": ["Getting started", "Install, connect data and score your first factor."],
      "guide/qfv-data": ["US data", "QFinVerse daily panel: universes, security ids, splits and dividends."],
      "guide/rest-api": ["REST API", "Evaluate factors, run backtests and manage the library over HTTP."],
      "guide/mcp-server": ["MCP", "Let Claude, Codex or any MCP client mine and test factors."],
    },
  },
  zh: {
    home: "首页", openApp: "打开 Assay", searchDocs: "搜索文档", searchPlaceholder: "搜索文档",
    noResults: "没有匹配的结果", onThisPage: "本页目录", updated: "更新于", prev: "上一篇", next: "下一篇",
    otherLang: "本页还没有中文版，下面是英文原文。",
    notFound: "页面不存在", notFoundHint: "页面可能已经移动。可以搜索，或从首页开始。",
    loadFailed: "页面加载失败。", copy: "复制", copied: "已复制",
    eyebrow: "Assay / 开发者文档",
    hero: "在时点正确的数据上做因子研究。",
    lede: "如何编写和评估因子表达式，QFinVerse 提供的美股数据包含什么，以及如何通过 REST API、Python 和 MCP 使用 Assay。",
    start: "快速开始", pages: (n) => `${n} 篇`, allDocs: "全部文档",
    themeSystem: "跟随系统", themeLight: "浅色", themeDark: "深色", langOther: "EN", langTitle: "Switch to English",
    cards: {
      "guide/getting-started": ["快速开始", "安装、接入数据，评估第一个因子。"],
      "guide/qfv-data": ["美股数据", "QFinVerse 日频面板：股票池、证券 ID、拆股与分红。"],
      "guide/rest-api": ["REST API", "通过 HTTP 评估因子、运行回测、管理因子库。"],
      "guide/mcp-server": ["MCP 接入", "让 Claude、Codex 或任意 MCP 客户端挖掘和测试因子。"],
    },
  },
};

const GROUP_ICON = { start: "g-start", data: "g-data", research: "g-research", api: "g-api", design: "g-design", reports: "g-reports" };

let lang = readLang();
let manifest = null;
let order = [];            // every slug in navigation order
const cache = new Map();   // "<lang>/<slug>" -> html
let searchIndex = null;    // { lang, rows }
let tocObserver = null;

const $ = (id) => document.getElementById(id);
const t = (key) => T[lang][key];

function readLang() {
  try {
    const v = localStorage.getItem(LANG_KEY);
    if (v === "en" || v === "zh") return v;
  } catch (_) { /* default below */ }
  return (navigator.language || "en").toLowerCase().startsWith("zh") ? "zh" : "en";
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function icon(id, cls = "ico") {
  return `<svg class="${cls}"><use href="#${id}"/></svg>`;
}

function title(slug) {
  const p = manifest.pages[slug];
  return p ? p.title[lang] || p.title.en || p.title.zh : slug;
}

function groupOf(slug) {
  return manifest.groups.find((g) => g.pages.includes(slug));
}

function currentSlug() {
  let path = decodeURIComponent(location.pathname);
  if (!path.startsWith(BASE)) return "";
  return path.slice(BASE.length).replace(/\/+$/, "").replace(/\.html$/, "");
}

// ------------------------------------------------------------------ chrome ---

function renderChrome(slug) {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-t]").forEach((n) => { n.textContent = t(n.dataset.t); });
  $("lang-label").textContent = t("langOther");
  $("lang-toggle").title = t("langTitle");
  $("search-input").placeholder = t("searchPlaceholder");
  $("search-kbd").textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘K" : "Ctrl K";
  const labels = { system: t("themeSystem"), light: t("themeLight"), dark: t("themeDark") };
  document.querySelectorAll("#theme-switch button").forEach((b) => {
    b.title = labels[b.dataset.theme];
    b.setAttribute("aria-label", labels[b.dataset.theme]);
  });
  refreshTheme();

  const group = slug ? groupOf(slug) : null;
  $("dh-tabs").innerHTML =
    `<a class="dh-tab${slug ? "" : " is-active"}" href="${BASE}" data-link>${esc(t("home"))}</a>` +
    manifest.groups.map((g) =>
      `<a class="dh-tab${group && group.id === g.id ? " is-active" : ""}" href="${BASE}${g.pages[0]}" data-link>${esc(g.label[lang])}</a>`
    ).join("");

  $("dh-nav").innerHTML = manifest.groups.map((g) => `
    <div class="dh-nav-group">
      <div class="dh-nav-head">${icon(GROUP_ICON[g.id] || "g-start")}${esc(g.label[lang])}</div>
      ${g.pages.map((p) => `<a href="${BASE}${p}" data-link class="${p === slug ? "is-active" : ""}"${p === slug ? ' aria-current="page"' : ""}>${esc(title(p))}</a>`).join("")}
    </div>`).join("");
}

// ------------------------------------------------------------------- pages ---

function renderHome() {
  const cards = Object.entries(t("cards")).map(([slug, [name, text]], i) => {
    const g = groupOf(slug);
    return `<a class="home-card" href="${BASE}${slug}" data-link>
      <div class="home-card-top">${icon(GROUP_ICON[g ? g.id : "start"])}<small>0${i + 1}</small></div>
      <h3>${esc(name)}</h3><p>${esc(text)}</p>
      <div class="home-card-foot"><span>${esc(g ? g.label[lang] : "")} · ${esc(t("pages")(g ? g.pages.length : 0))}</span>${icon("i-arrow")}</div>
    </a>`;
  }).join("");
  const index = manifest.groups.map((g) => `
    <div><h3>${icon(GROUP_ICON[g.id] || "g-start")}${esc(g.label[lang])}</h3>
      <ul>${g.pages.map((p) => `<li><a href="${BASE}${p}" data-link>${esc(title(p))}</a></li>`).join("")}</ul></div>`).join("");
  $("dh-main").innerHTML = `
    <section class="home">
      <div class="home-eyebrow">${esc(t("eyebrow"))}</div>
      <h1>${esc(t("hero"))}</h1>
      <p class="home-lede">${esc(t("lede"))}</p>
      <div class="home-actions">
        <a class="btn btn--primary" href="${BASE}guide/getting-started" data-link>${esc(t("start"))}${icon("i-arrow")}</a>
        <button type="button" class="btn" data-search>${icon("i-search")}${esc(t("searchDocs"))}</button>
      </div>
      <div class="home-cards">${cards}</div>
      <div class="home-section"><h2>${esc(t("allDocs"))}</h2><div class="home-index">${index}</div></div>
    </section>`;
  $("dh-toc").innerHTML = "";
  document.title = "Doc Hub · Assay";
}

async function fetchPage(slug, l) {
  const key = `${l}/${slug}`;
  if (cache.has(key)) return cache.get(key);
  const res = await fetch(`${BASE}content/${l}/${slug}.html`);
  if (!res.ok) throw new Error(String(res.status));
  const html = await res.text();
  cache.set(key, html);
  return html;
}

async function renderPage(slug) {
  const page = manifest.pages[slug];
  if (!page) {
    $("dh-main").innerHTML = `<div class="doc"><div class="empty-state"><div class="empty-state-title">${esc(t("notFound"))}</div>
      <div>${esc(t("notFoundHint"))}</div><a class="btn btn--sm" href="${BASE}" data-link>${esc(t("home"))}</a></div></div>`;
    $("dh-toc").innerHTML = "";
    document.title = `${t("notFound")} · Assay`;
    return;
  }
  const useLang = page.title[lang] ? lang : lang === "zh" ? "en" : "zh";
  let body;
  try {
    body = await fetchPage(slug, useLang);
  } catch (_) {
    $("dh-main").innerHTML = `<div class="doc"><div class="error-state">${esc(t("loadFailed"))}</div></div>`;
    return;
  }
  if (currentSlug() !== slug) return;           // navigated away while loading
  const group = groupOf(slug);
  const i = order.indexOf(slug);
  const prev = order[i - 1], next = order[i + 1];
  $("dh-main").innerHTML = `
    <article class="doc">
      <div class="doc-meta">
        <nav class="doc-crumbs"><a href="${BASE}" data-link>${esc(t("home"))}</a><span>/</span>
          <a href="${BASE}${group.pages[0]}" data-link>${esc(group.label[lang])}</a><span>/</span><span>${esc(title(slug))}</span></nav>
        ${page.updated ? `<span class="doc-updated">${esc(t("updated"))} ${esc(page.updated)}</span>` : ""}
      </div>
      ${useLang !== lang ? `<div class="doc-fallback">${esc(t("otherLang"))}</div>` : ""}
      <div class="doc-body">${body}</div>
      <nav class="doc-pager">
        ${prev ? `<a class="prev" href="${BASE}${prev}" data-link><small>← ${esc(t("prev"))}</small><span>${esc(title(prev))}</span></a>` : ""}
        ${next ? `<a class="next" href="${BASE}${next}" data-link><small>${esc(t("next"))} →</small><span>${esc(title(next))}</span></a>` : ""}
      </nav>
    </article>`;
  decorate($("dh-main"));
  renderToc(page.headings[useLang] || []);
  document.title = `${title(slug)} · Assay Doc Hub`;
}

// Heading anchors and copy buttons on code blocks.
function decorate(root) {
  root.querySelectorAll(".doc-body h2[id], .doc-body h3[id]").forEach((h) => {
    const a = document.createElement("a");
    a.className = "anchor";
    a.href = `#${h.id}`;
    a.textContent = "#";
    a.setAttribute("aria-hidden", "true");
    h.appendChild(a);
  });
  root.querySelectorAll(".doc-body pre").forEach((pre) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "doc-copy";
    btn.title = t("copy");
    btn.innerHTML = icon("i-copy");
    btn.addEventListener("click", async () => {
      const text = pre.querySelector("code") ? pre.querySelector("code").innerText : pre.innerText;
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        const range = document.createRange();
        range.selectNodeContents(pre.querySelector("code") || pre);
        const sel = getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        return;
      }
      btn.innerHTML = icon("i-check");
      btn.title = t("copied");
      setTimeout(() => { btn.innerHTML = icon("i-copy"); btn.title = t("copy"); }, 1400);
    });
    pre.appendChild(btn);
  });
}

function renderToc(headings) {
  if (tocObserver) tocObserver.disconnect();
  const toc = $("dh-toc");
  if (!headings.length) { toc.innerHTML = ""; return; }
  toc.innerHTML = `<div class="dh-toc-title">${esc(t("onThisPage"))}</div>` +
    headings.map((h) => `<a href="#${esc(h.id)}" class="l${h.level}" data-id="${esc(h.id)}">${esc(h.text)}</a>`).join("");
  const links = new Map([...toc.querySelectorAll("a")].map((a) => [a.dataset.id, a]));
  const visible = new Set();
  tocObserver = new IntersectionObserver((entries) => {
    entries.forEach((e) => (e.isIntersecting ? visible.add(e.target.id) : visible.delete(e.target.id)));
    const first = headings.find((h) => visible.has(h.id));
    if (!first) return;
    links.forEach((a, id) => a.classList.toggle("is-active", id === first.id));
  }, { rootMargin: "-100px 0px -65% 0px" });
  headings.forEach((h) => { const el = document.getElementById(h.id); if (el) tocObserver.observe(el); });
}

async function route({ scroll = true } = {}) {
  const slug = currentSlug();
  renderChrome(slug);
  document.body.classList.remove("nav-open");
  $("menu-toggle").setAttribute("aria-expanded", "false");
  if (!slug) renderHome();
  else await renderPage(slug);
  if (!scroll) return;
  const target = location.hash ? document.getElementById(decodeURIComponent(location.hash.slice(1))) : null;
  if (target) target.scrollIntoView();
  else window.scrollTo(0, 0);
}

function go(href) {
  const url = new URL(href, location.href);
  if (url.pathname === location.pathname && url.hash) {
    history.pushState(null, "", url.pathname + url.hash);
    const el = document.getElementById(decodeURIComponent(url.hash.slice(1)));
    if (el) el.scrollIntoView();
    return;
  }
  history.pushState(null, "", url.pathname + url.hash);
  route();
}

// ------------------------------------------------------------------ search ---

async function loadSearch() {
  if (searchIndex && searchIndex.lang === lang) return searchIndex.rows;
  const res = await fetch(`${BASE}content/search-${lang}.json`);
  const rows = res.ok ? await res.json() : [];
  searchIndex = { lang, rows: rows.map((r) => ({ ...r, lower: r.text.toLowerCase(), ltitle: r.title.toLowerCase() })) };
  return searchIndex.rows;
}

function mark(text, terms) {
  let out = esc(text);
  terms.forEach((term) => {
    if (!term) return;
    const re = new RegExp(esc(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
    out = out.replace(re, (m) => `<mark>${m}</mark>`);
  });
  return out;
}

function search(rows, query) {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  const hits = [];
  for (const r of rows) {
    let score = 0, heading = null, at = -1;
    let all = true;
    for (const term of terms) {
      const inTitle = r.ltitle.includes(term);
      const h = r.headings.find((x) => x.toLowerCase().includes(term));
      const pos = r.lower.indexOf(term);
      if (!inTitle && !h && pos < 0) { all = false; break; }
      score += (inTitle ? 20 : 0) + (h ? 6 : 0) + (pos >= 0 ? 1 : 0);
      if (h && !heading) heading = h;
      if (pos >= 0 && at < 0) at = pos;
    }
    if (!all) continue;
    const start = Math.max(0, at - 40);
    const snippet = at >= 0 ? (start ? "…" : "") + r.text.slice(start, at + 110) : "";
    hits.push({ ...r, score, heading, snippet });
  }
  return hits.sort((a, b) => b.score - a.score).slice(0, 20).map((h) => ({ ...h, terms }));
}

let active = 0;
let results = [];

function headingId(slug, text) {
  const p = manifest.pages[slug];
  const list = (p && (p.headings[lang] || p.headings.en)) || [];
  const h = list.find((x) => x.text === text);
  return h ? `#${h.id}` : "";
}

function paintResults() {
  const list = $("search-results");
  const q = $("search-input").value.trim();
  if (!q) { list.innerHTML = ""; return; }
  if (!results.length) { list.innerHTML = `<li class="dh-search-empty">${esc(t("noResults"))}</li>`; return; }
  list.innerHTML = results.map((r, i) => {
    const g = groupOf(r.slug);
    const href = `${BASE}${r.slug}${r.heading ? headingId(r.slug, r.heading) : ""}`;
    return `<li role="option" class="${i === active ? "is-active" : ""}" aria-selected="${i === active}">
      <a href="${href}" data-link data-result>
        <span class="r-title">${mark(r.title, r.terms)}</span><span class="r-path">${esc(g ? g.label[lang] : "")}${r.heading ? " · " + mark(r.heading, r.terms) : ""}</span>
        ${r.snippet ? `<span class="r-snip">${mark(r.snippet, r.terms)}</span>` : ""}
      </a></li>`;
  }).join("");
  const el = list.children[active];
  if (el) el.scrollIntoView({ block: "nearest" });
}

async function openSearch() {
  $("search-dialog").hidden = false;
  const input = $("search-input");
  input.focus();
  input.select();
  const rows = await loadSearch();
  results = search(rows, input.value);
  active = 0;
  paintResults();
}

function closeSearch() {
  $("search-dialog").hidden = true;
}

function wireSearch() {
  $("search-open").addEventListener("click", openSearch);
  $("search-input").addEventListener("input", async () => {
    const rows = await loadSearch();
    results = search(rows, $("search-input").value);
    active = 0;
    paintResults();
  });
  $("search-input").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { active = Math.min(active + 1, results.length - 1); paintResults(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); paintResults(); e.preventDefault(); }
    else if (e.key === "Enter") {
      const a = $("search-results").querySelectorAll("a")[active];
      if (a) { closeSearch(); go(a.getAttribute("href")); }
    }
  });
  $("search-dialog").addEventListener("click", (e) => { if (e.target === $("search-dialog")) closeSearch(); });
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
    if ((e.key === "k" && (e.metaKey || e.ctrlKey)) || (e.key === "/" && !typing)) { e.preventDefault(); openSearch(); }
    else if (e.key === "Escape" && !$("search-dialog").hidden) closeSearch();
  });
}

// ------------------------------------------------------------ theme, lang ---

function currentTheme() {
  try { const v = localStorage.getItem(THEME_KEY); return ["light", "dark", "system"].includes(v) ? v : "system"; } catch (_) { return "system"; }
}

function refreshTheme() {
  const theme = currentTheme();
  const dark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  document.querySelectorAll("#theme-switch button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.theme === theme)));
}

function wireControls() {
  document.querySelectorAll("#theme-switch button").forEach((b) => b.addEventListener("click", () => {
    try { localStorage.setItem(THEME_KEY, b.dataset.theme); } catch (_) {}
    refreshTheme();
  }));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", refreshTheme);
  $("lang-toggle").addEventListener("click", () => {
    lang = lang === "en" ? "zh" : "en";
    try { localStorage.setItem(LANG_KEY, lang); } catch (_) {}
    route({ scroll: false });
  });
  $("menu-toggle").addEventListener("click", () => {
    const open = document.body.classList.toggle("nav-open");
    $("menu-toggle").setAttribute("aria-expanded", String(open));
  });
  // In-hub links stay in the page; everything else navigates normally.
  document.addEventListener("click", (e) => {
    const trigger = e.target.closest("[data-search]");
    if (trigger) { openSearch(); return; }
    const a = e.target.closest("a");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || a.target) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith(BASE) || url.pathname.startsWith(BASE + "content/")) return;
    e.preventDefault();
    if (a.hasAttribute("data-result")) closeSearch();
    go(url.pathname + url.hash);
  });
  addEventListener("popstate", () => route({ scroll: true }));
}

async function boot() {
  wireControls();
  wireSearch();
  try {
    const res = await fetch(`${BASE}content/manifest.json`);
    manifest = await res.json();
  } catch (_) {
    $("dh-main").innerHTML = `<div class="error-state">${esc(T[lang].loadFailed)}</div>`;
    return;
  }
  if (manifest.repo) $("repo-link").href = manifest.repo;
  order = manifest.groups.flatMap((g) => g.pages);
  await route();
}

boot();
