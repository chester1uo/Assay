// lightbox.js — a tiny, dependency-free modal/lightbox shared across pages.
//
// Two entry points:
//   openLightbox({ title, content, wide })  -> close()   generic centered modal
//   makeZoomButton(ctx, getChartNode, getTitle)          ⤢ button that clones the
//                                                          current chart SVG into a
//                                                          large lightbox.
//
// Charts (charts.js) are viewBox SVGs with preserveAspectRatio, so a *clone* scales
// crisply to any size — the zoom button needs no access to the underlying data.
//
// Exposed to pages via ctx.lightbox (see app.js makeCtx).

let _openCount = 0;

/**
 * Open a centered modal over a dimmed backdrop.
 * @param {object} o
 * @param {string} o.title       header text
 * @param {Node}   o.content     body node (owned by the modal; removed on close)
 * @param {boolean}[o.wide]      use the wide (chart) layout
 * @returns {() => void} close   idempotent close function
 */
export function openLightbox({ title = "", content = null, wide = false } = {}) {
  injectStyle();

  const titleEl = document.createElement("div");
  titleEl.className = "lb-title";
  titleEl.textContent = title;

  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "lb-close";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.textContent = "✕";

  const head = document.createElement("div");
  head.className = "lb-head";
  head.append(titleEl, closeBtn);

  const body = document.createElement("div");
  body.className = "lb-body";
  if (content) body.appendChild(content);

  const dialog = document.createElement("div");
  dialog.className = "lb-dialog" + (wide ? " lb-dialog--wide" : "");
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.append(head, body);

  const backdrop = document.createElement("div");
  backdrop.className = "lb-backdrop";
  backdrop.appendChild(dialog);

  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    backdrop.remove();
    _openCount = Math.max(0, _openCount - 1);
    if (_openCount === 0) document.body.classList.remove("lb-lock");
  };

  const onKey = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  };
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
  closeBtn.addEventListener("click", close);
  document.addEventListener("keydown", onKey, true);

  document.body.appendChild(backdrop);
  _openCount += 1;
  document.body.classList.add("lb-lock");
  closeBtn.focus();
  return close;
}

/**
 * Build a small "expand" button that, on click, clones the chart SVG returned by
 * `getChartNode()` and shows it large in a lightbox. No-op (disabled look) if no
 * chart is present yet.
 * @param {object} ctx                page ctx (for ctx.el + ctx.t)
 * @param {() => (Node|null)} getChartNode  returns the live <svg> (or its wrapper)
 * @param {() => string} getTitle     lightbox title supplier
 */
export function makeZoomButton(ctx, getChartNode, getTitle) {
  const btn = ctx.el("button", {
    type: "button",
    className: "btn btn--sm lb-zoom",
    title: ctx.t("common.viewLarge"),
    "aria-label": ctx.t("common.viewLarge"),
    onClick: () => {
      const src = getChartNode && getChartNode();
      const svg = src && (src.tagName === "svg" ? src : src.querySelector && src.querySelector("svg"));
      if (!svg) return;
      const clone = svg.cloneNode(true);
      const wrap = document.createElement("div");
      wrap.className = "lb-chart";
      wrap.appendChild(clone);
      openLightbox({ title: (getTitle && getTitle()) || "", content: wrap, wide: true });
    },
  }, "⤢");
  return btn;
}

// ---------------------------------------------------------------- styles ----

const STYLE_ID = "lightbox-style";
function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const css = `
.lb-lock { overflow: hidden; }
.lb-backdrop {
  position: fixed; inset: 0; z-index: 100;
  background: rgba(15, 23, 42, 0.55);
  display: flex; align-items: center; justify-content: center;
  padding: var(--sp-4, 16px);
}
.lb-dialog {
  background: var(--bg, #fff); color: var(--text, #111);
  border-radius: var(--radius-card, 12px);
  box-shadow: 0 24px 64px rgba(0,0,0,0.28);
  max-width: 560px; width: 100%; max-height: 88vh;
  display: flex; flex-direction: column; overflow: hidden;
}
.lb-dialog--wide { max-width: min(1100px, 92vw); }
.lb-head {
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2, 8px);
  padding: var(--sp-3, 12px) var(--sp-4, 16px);
  border-bottom: 1px solid var(--border, #e5e7eb);
}
.lb-title { font-weight: 600; font-size: 15px; }
.lb-close {
  border: none; background: none; cursor: pointer; font-size: 16px; line-height: 1;
  color: var(--text-muted, #6b7280); padding: 4px 8px; border-radius: var(--radius-badge, 6px);
}
.lb-close:hover { background: var(--gray-1, #f3f4f6); color: var(--text, #111); }
.lb-body { padding: var(--sp-4, 16px); overflow: auto; }
.lb-chart { width: 100%; height: min(64vh, 620px); }
.lb-chart svg { width: 100%; height: 100%; }
.lb-zoom { padding: 2px 8px; line-height: 1.2; }
`;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = css;
  document.head.appendChild(style);
}
