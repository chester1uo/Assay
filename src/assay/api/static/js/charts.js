import { P } from "./palette.js";
// charts.js — hand-rolled SVG chart factories. Pure DOM, no dependencies.
// Every factory returns an <svg> (or wrapping) element built in the SVG namespace.

const NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}, ...children) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, String(v));
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function isNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

function finiteValues(arrays) {
  const out = [];
  for (const arr of arrays) for (const v of arr || []) if (isNum(v)) out.push(v);
  return out;
}

let textMeasureContext;
function chartTextWidth(text) {
  if (!textMeasureContext) {
    textMeasureContext = document.createElement("canvas").getContext("2d");
    textMeasureContext.font = "10px system-ui, sans-serif";
  }
  return textMeasureContext.measureText(String(text)).width;
}

function axisLeftMargin(lo, hi, ticks, width) {
  const step = (hi - lo) / ticks;
  let widest = 0;
  for (let i = 0; i <= ticks; i++) {
    widest = Math.max(widest, chartTextWidth(formatTick(lo + step * i, step)));
  }
  return Math.min(width - 40, Math.max(44, Math.ceil(widest) + 12));
}

function niceDomain(values, { padFrac = 0.08, includeZero = true } = {}) {
  if (!values.length) return [-1, 1];
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (includeZero) {
    lo = Math.min(lo, 0);
    hi = Math.max(hi, 0);
  }
  if (lo === hi) {
    const pad = Math.abs(lo) || 1;
    return [lo - pad, hi + pad];
  }
  const pad = (hi - lo) * padFrac;
  return [lo - pad, hi + pad];
}

// ---------------------------------------------------------------- color ----

/** Diverging color: green for +, white at 0, red for −, symmetric about `max`. */
export function diverging(value, max) {
  if (!isNum(value) || !isNum(max) || max === 0) return P.surface;
  const t = Math.max(-1, Math.min(1, value / max));
  if (t >= 0) return mix(P.surface, P.up, t);
  return mix(P.surface, P.down, -t);
}

/** Sequential color from light to `to` (default blue) over [0,1]. */
export function seq(t, to = P.primary, from = P.fill) {
  const c = Math.max(0, Math.min(1, isNum(t) ? t : 0));
  return mix(from, to, c);
}

function mix(a, b, t) {
  const ca = hexToRgb(a);
  const cb = hexToRgb(b);
  const r = Math.round(ca[0] + (cb[0] - ca[0]) * t);
  const g = Math.round(ca[1] + (cb[1] - ca[1]) * t);
  const bl = Math.round(ca[2] + (cb[2] - ca[2]) * t);
  return `rgb(${r},${g},${bl})`;
}

function hexToRgb(hex) {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

const DEFAULT_COLORS = [P.primary, P.second, P.amber, P.down, P.up];

// ---------------------------------------------------------------- hover ----
// Opt-in interactivity for line/bar charts: a dashed crosshair + focus dots and an
// HTML tooltip showing the date and each series value at the hovered index.
//
// Mouse→data mapping uses the SVG's own getScreenCTM() inverse, so it stays correct
// under viewBox scaling / letterboxing (the charts use preserveAspectRatio). The
// chart returns a positioned wrapper <div> (svg + tooltip) instead of a bare <svg>;
// callers already wrap charts in a container and the zoom button finds the inner
// <svg> via querySelector, so both keep working.

const HOVER_STYLE_ID = "chart-hover-style";
function injectHoverStyle() {
  if (typeof document === "undefined" || document.getElementById(HOVER_STYLE_ID)) return;
  const css = `
.chart-interactive { position: relative; }
.chart-crosshair { stroke: var(--text-muted); stroke-width: 1; stroke-dasharray: 3 3; pointer-events: none; }
.chart-tip { position: absolute; pointer-events: none; z-index: 5; background: rgba(17,24,39,0.92);
  color: #fff; font-size: 12px; padding: 6px 8px; border-radius: 6px; white-space: nowrap;
  box-shadow: 0 4px 12px rgba(0,0,0,0.22); transform: translateZ(0); }
.chart-tip-date { font-weight: 600; margin-bottom: 3px; }
.chart-tip-row { display: flex; align-items: center; gap: 6px; font-family: ui-monospace, monospace; line-height: 1.5; }
.chart-tip-sw { width: 8px; height: 8px; border-radius: 2px; display: inline-block; flex: 0 0 auto; }
`;
  const style = document.createElement("style");
  style.id = HOVER_STYLE_ID;
  style.textContent = css;
  document.head.appendChild(style);
}

/**
 * Wrap `svg` with crosshair + tooltip interactivity.
 * cfg: { top, ih, n, indexAt(ux)->i, xAt(i)->px, seriesAt(i)->[{name,color,value,cy?}], dateAt(i)->str, valueFmt }
 */
function attachHover(svg, cfg) {
  injectHoverStyle();
  const { top, ih, n, indexAt, xAt, seriesAt, dateAt, valueFmt } = cfg;
  const fmtV = valueFmt || ((v) => (isNum(v) ? String(v) : "—"));

  const cross = svgEl("line", { class: "chart-crosshair", x1: 0, x2: 0, y1: top, y2: top + ih, opacity: 0 });
  const focus = svgEl("g", {});
  svg.appendChild(cross);
  svg.appendChild(focus);

  const wrap = document.createElement("div");
  wrap.className = "chart-interactive";
  wrap.appendChild(svg);
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.style.display = "none";
  wrap.appendChild(tip);

  function toUserX(e) {
    if (typeof svg.getScreenCTM !== "function") return null;
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    const pt = svg.createSVGPoint();
    pt.x = e.clientX;
    pt.y = e.clientY;
    return pt.matrixTransform(ctm.inverse()).x;
  }

  function onMove(e) {
    if (!n) return;
    const ux = toUserX(e);
    if (ux == null) return;
    let i = indexAt(ux);
    i = Math.max(0, Math.min(n - 1, i));
    const cx = xAt(i);
    cross.setAttribute("x1", cx);
    cross.setAttribute("x2", cx);
    cross.setAttribute("opacity", 1);
    const rows = seriesAt(i) || [];
    focus.replaceChildren();
    for (const p of rows) {
      if (p.cy == null || !isNum(p.cy)) continue;
      focus.appendChild(svgEl("circle", { cx, cy: p.cy, r: 3.2, fill: p.color, stroke: P.surface, "stroke-width": 1 }));
    }
    tip.replaceChildren();
    const dEl = document.createElement("div");
    dEl.className = "chart-tip-date";
    dEl.textContent = dateAt(i);
    tip.appendChild(dEl);
    for (const p of rows) {
      const row = document.createElement("div");
      row.className = "chart-tip-row";
      const sw = document.createElement("span");
      sw.className = "chart-tip-sw";
      sw.style.background = p.color;
      row.appendChild(sw);
      row.appendChild(document.createTextNode(`${p.name ? p.name + ": " : ""}${fmtV(p.value)}`));
      tip.appendChild(row);
    }
    tip.style.display = "";
    const wr = wrap.getBoundingClientRect();
    let tx = e.clientX - wr.left + 14;
    let ty = e.clientY - wr.top + 14;
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    if (tx + tw > wr.width) tx = e.clientX - wr.left - tw - 14;
    if (ty + th > wr.height) ty = wr.height - th - 4;
    tip.style.left = Math.max(0, tx) + "px";
    tip.style.top = Math.max(0, ty) + "px";
  }
  function onLeave() {
    cross.setAttribute("opacity", 0);
    focus.replaceChildren();
    tip.style.display = "none";
  }
  svg.addEventListener("mousemove", onMove);
  svg.addEventListener("mouseleave", onLeave);
  return wrap;
}

// ---------------------------------------------------------------- lineChart ----
/**
 * Multi-line chart with axes, monthly-ish x ticks, and a zero line.
 * series: [{name, values:[num], color?}]  (NaN/null -> gap)
 * dates?: [isoStr] aligned to value index; yDomain?: [lo,hi]; bands?: [{from,to,color}]
 */
export function lineChart({ series = [], dates = null, yDomain = null, height = 240, bands = [], width = 640, interactive = false, valueFmt = null }) {
  const w = width;
  const h = height;
  const n = series.reduce((mx, s) => Math.max(mx, (s.values || []).length), 0);
  const dom = yDomain || niceDomain(finiteValues(series.map((s) => s.values)), { includeZero: true });
  const [ylo, yhi] = dom;
  const m = { top: 12, right: 16, bottom: 24, left: axisLeftMargin(ylo, yhi, 4, w) };
  const iw = w - m.left - m.right;
  const ih = h - m.top - m.bottom;

  const x = (i) => (n <= 1 ? m.left + iw / 2 : m.left + (i / (n - 1)) * iw);
  const y = (v) => m.top + ih - ((v - ylo) / (yhi - ylo || 1)) * ih;

  const root = svgEl("svg", {
    viewBox: `0 0 ${w} ${h}`,
    class: "chart chart--line",
    preserveAspectRatio: "xMidYMid meet",
    role: "img",
  });

  // y-axis grid + labels
  const axis = svgEl("g", { class: "chart-axis" });
  const ticks = 4;
  for (let t = 0; t <= ticks; t++) {
    const val = ylo + ((yhi - ylo) * t) / ticks;
    const yy = y(val);
    axis.appendChild(svgEl("line", { x1: m.left, x2: w - m.right, y1: yy, y2: yy, class: "chart-grid" }));
    axis.appendChild(svgEl("text", { x: m.left - 6, y: yy + 3, "text-anchor": "end", text: formatTick(val, (yhi - ylo) / ticks) }));
  }
  root.appendChild(axis);

  // shaded bands (optional)
  for (const b of bands) {
    if (!isNum(b.from) || !isNum(b.to)) continue;
    const yA = y(b.to);
    const yB = y(b.from);
    root.appendChild(
      svgEl("rect", {
        x: m.left, y: Math.min(yA, yB), width: iw, height: Math.abs(yB - yA),
        fill: b.color || P.fill,
      })
    );
  }

  // zero line (if within domain)
  if (ylo < 0 && yhi > 0) {
    root.appendChild(svgEl("line", { x1: m.left, x2: w - m.right, y1: y(0), y2: y(0), class: "chart-zero" }));
  }

  // x ticks — monthly-ish: pick ~6 evenly spaced labels from dates (or indices)
  if (n > 1) {
    const xAxis = svgEl("g", { class: "chart-axis" });
    const labelCount = Math.min(6, n);
    const indices = Array.from({ length: labelCount }, (_, t) => Math.round((t / (labelCount - 1)) * (n - 1)));
    const monthLabels = indices.map((i) => dates && dates[i] ? shortDate(dates[i]) : String(i));
    // Daily data within the same month needs a day component to distinguish ticks.
    const showDays = new Set(monthLabels).size < monthLabels.length && dates;
    for (let t = 0; t < labelCount; t++) {
      const i = indices[t];
      const xx = x(i);
      const lbl = showDays && typeof dates[i] === "string" && dates[i].length >= 10
        ? dates[i].slice(2, 10) : monthLabels[t];
      xAxis.appendChild(svgEl("text", { x: xx, y: h - 8,
        "text-anchor": t === 0 ? "start" : t === labelCount - 1 ? "end" : "middle", text: lbl }));
    }
    root.appendChild(xAxis);
  }

  // lines (NaN-aware: break path on gaps)
  series.forEach((s, si) => {
    const color = s.color || DEFAULT_COLORS[si % DEFAULT_COLORS.length];
    let d = "";
    let pen = false;
    (s.values || []).forEach((v, i) => {
      if (!isNum(v)) {
        pen = false;
        return;
      }
      d += (pen ? " L" : " M") + ` ${x(i).toFixed(2)} ${y(v).toFixed(2)}`;
      pen = true;
    });
    if (d) root.appendChild(svgEl("path", { d: d.trim(), fill: "none", stroke: color, "stroke-width": 1.6 }));
  });

  if (!interactive) return root;
  return attachHover(root, {
    top: m.top, ih, n,
    indexAt: (ux) => (n <= 1 ? 0 : Math.round(((ux - m.left) / (iw || 1)) * (n - 1))),
    xAt: x,
    seriesAt: (i) => series.map((s, si) => {
      const v = (s.values || [])[i];
      return {
        name: s.name || "",
        color: s.color || DEFAULT_COLORS[si % DEFAULT_COLORS.length],
        value: v,
        cy: isNum(v) ? y(v) : null,
      };
    }),
    dateAt: (i) => (dates && dates[i] != null ? String(dates[i]) : String(i)),
    valueFmt,
  });
}

// ---------------------------------------------------------------- barChart ----
/**
 * Vertical bar chart with zero baseline and optional value labels.
 * labels:[str], values:[num], colors?:[str], valueLabels?:bool
 */
export function barChart({ labels = [], values = [], colors = null, height = 240, valueLabels = false, width = 640, interactive = false, valueFmt = null, dates = null }) {
  const w = width;
  const h = height;
  const dom = niceDomain(values.filter(isNum), { includeZero: true });
  const [ylo, yhi] = dom;
  const m = { top: 16, right: 16, bottom: 28, left: axisLeftMargin(ylo, yhi, 4, w) };
  const iw = w - m.left - m.right;
  const ih = h - m.top - m.bottom;
  const y = (v) => m.top + ih - ((v - ylo) / (yhi - ylo || 1)) * ih;
  const n = values.length || 1;
  const slot = iw / n;
  const bw = Math.max(2, slot * 0.62);

  const root = svgEl("svg", {
    viewBox: `0 0 ${w} ${h}`,
    class: "chart chart--bar",
    preserveAspectRatio: "xMidYMid meet",
    role: "img",
  });

  const axis = svgEl("g", { class: "chart-axis" });
  for (let t = 0; t <= 4; t++) {
    const val = ylo + ((yhi - ylo) * t) / 4;
    const yy = y(val);
    axis.appendChild(svgEl("line", { x1: m.left, x2: w - m.right, y1: yy, y2: yy, class: "chart-grid" }));
    axis.appendChild(svgEl("text", { x: m.left - 6, y: yy + 3, "text-anchor": "end", text: formatTick(val, (yhi - ylo) / 4) }));
  }
  root.appendChild(axis);

  const y0 = y(0);
  root.appendChild(svgEl("line", { x1: m.left, x2: w - m.right, y1: y0, y2: y0, class: "chart-zero" }));

  // Account for label width as well as the number of bars, and keep the last
  // label when it fits. The chart's text is drawn at 10px in the SVG viewBox.
  const textWidth = (i) => labels[i] == null ? 0 : chartTextWidth(labels[i]);
  const maxLabelWidth = labels.reduce((max, _, i) => Math.max(max, textWidth(i)), 0);
  const labelStep = Math.max(1, Math.ceil(n / 12), Math.ceil((maxLabelWidth + 8) / slot));
  const labelX = (i) => Math.max(textWidth(i) / 2 + 4,
    Math.min(w - textWidth(i) / 2 - 4, m.left + slot * i + slot / 2));
  const labelIndices = [];
  for (let i = 0; i < n; i++) {
    if (labels[i] === undefined || !isNum(values[i]) || (i % labelStep !== 0 && i !== n - 1)) continue;
    const collides = (previous) => labelX(i) - labelX(previous) < (textWidth(i) + textWidth(previous)) / 2 + 8;
    if (i === n - 1) while (labelIndices.length > 1 && collides(labelIndices[labelIndices.length - 1])) labelIndices.pop();
    if (!labelIndices.length || !collides(labelIndices[labelIndices.length - 1])) labelIndices.push(i);
  }
  const labeled = new Set(labelIndices);

  values.forEach((v, i) => {
    if (!isNum(v)) return;
    const cx = m.left + slot * i + slot / 2;
    const yy = y(v);
    const color = (colors && colors[i]) || (v >= 0 ? P.primary : P.down);
    root.appendChild(
      svgEl("rect", { x: cx - bw / 2, y: Math.min(yy, y0), width: bw, height: Math.abs(yy - y0), fill: color, rx: 2 })
    );
    if (valueLabels) {
      root.appendChild(
        svgEl("text", {
          x: cx, y: v >= 0 ? yy - 4 : yy + 12, "text-anchor": "middle",
          class: "chart-axis", text: formatTick(v),
        })
      );
    }
    if (labels[i] !== undefined && labeled.has(i)) {
      root.appendChild(
        svgEl("text", { x: labelX(i), y: h - 10, "text-anchor": "middle", class: "chart-axis", text: String(labels[i]) })
      );
    }
  });

  if (!interactive) return root;
  return attachHover(root, {
    top: m.top, ih, n,
    indexAt: (ux) => Math.floor((ux - m.left) / (slot || 1)),
    xAt: (i) => m.left + slot * i + slot / 2,
    seriesAt: (i) => {
      const v = values[i];
      const color = (colors && colors[i]) || (v >= 0 ? P.primary : P.down);
      return [{ name: "", color, value: v, cy: isNum(v) ? y(v) : null }];
    },
    dateAt: (i) => (dates && dates[i] != null ? String(dates[i]) : (labels[i] != null ? String(labels[i]) : String(i))),
    valueFmt,
  });
}

// ---------------------------------------------------------------- calendarHeatmap ----
/**
 * Calendar heatmap, cells grouped by year then month, colored by diverging scale.
 * dates:[isoStr], values:[num]  (aligned). Title row shows year labels.
 */
export function calendarHeatmap({ dates = [], values = [], height = 140, diverging: useDiv = true, width = 640 }) {
  const valid = dates.map((d, i) => ({ d, v: values[i] })).filter((p) => p.d);
  const finite = valid.map((p) => p.v).filter(isNum);
  const absMax = finite.length ? Math.max(...finite.map(Math.abs)) : 1;

  // group by year-month
  const byYM = new Map(); // 'YYYY-MM' -> {year, month, value(mean)}
  for (const p of valid) {
    const ym = p.d.slice(0, 7);
    if (!byYM.has(ym)) byYM.set(ym, { year: p.d.slice(0, 4), month: Number(p.d.slice(5, 7)), sum: 0, n: 0 });
    const cell = byYM.get(ym);
    if (isNum(p.v)) {
      cell.sum += p.v;
      cell.n += 1;
    }
  }
  const cells = [...byYM.values()].map((c) => ({ ...c, value: c.n ? c.sum / c.n : NaN }));
  const years = [...new Set(cells.map((c) => c.year))].sort();

  const w = width;
  const h = height;
  const left = 36;
  const top = 18;
  const cellW = years.length ? (w - left - 8) / years.length : 0;
  const cellH = (h - top - 8) / 12;

  const root = svgEl("svg", {
    viewBox: `0 0 ${w} ${h}`,
    class: "chart chart--cal",
    preserveAspectRatio: "xMidYMid meet",
    role: "img",
  });

  // Keep labels far enough apart even when the calendar spans decades.
  const yearStep = cellW > 0 ? Math.max(1, Math.ceil(34 / cellW)) : 1;
  const yearIndices = years.map((_, i) => i).filter((i) => i % yearStep === 0);
  const last = years.length - 1;
  if (last > 0 && yearIndices[yearIndices.length - 1] !== last) {
    if ((last - yearIndices[yearIndices.length - 1]) * cellW >= 34) yearIndices.push(last);
    else if (yearIndices.length > 1) yearIndices[yearIndices.length - 1] = last;
  }
  yearIndices.forEach((xi) => {
    root.appendChild(
      svgEl("text", {
        x: Math.min(w - 14, left + cellW * xi + cellW / 2), y: 12,
        "text-anchor": "middle", class: "chart-cal-label chart-cal-year", text: years[xi],
      })
    );
  });
  // month labels
  const MONTHS = document.documentElement.lang.toLowerCase().startsWith("zh")
    ? Array.from({ length: 12 }, (_, i) => `${i + 1}月`)
    : ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  for (let mo = 0; mo < 12; mo++) {
    root.appendChild(
      svgEl("text", {
        x: left - 6, y: top + cellH * mo + cellH / 2 + 3, "text-anchor": "end",
        class: "chart-cal-label chart-cal-month", text: MONTHS[mo],
      })
    );
  }

  for (const c of cells) {
    const xi = years.indexOf(c.year);
    if (xi < 0) continue;
    const fill = useDiv ? diverging(c.value, absMax) : seq(isNum(c.value) ? c.value / absMax : 0);
    root.appendChild(
      svgEl("rect", {
        x: left + cellW * xi + 1,
        y: top + cellH * (c.month - 1) + 1,
        width: Math.max(1, cellW - 2),
        height: Math.max(1, cellH - 2),
        fill: isNum(c.value) ? fill : P.surface,
        stroke: P.border,
        "stroke-width": 0.5,
        rx: 1,
      })
    );
  }

  return root;
}

// ---------------------------------------------------------------- sparkline ----
/** Minimal inline line; NaN-aware. */
export function sparkline(values, { width = 120, height = 32, color = P.primary } = {}) {
  const vals = (values || []).map((v) => (isNum(v) ? v : NaN));
  const finite = vals.filter(isNum);
  const root = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    class: "chart chart--spark",
    width,
    height,
    preserveAspectRatio: "none",
    role: "img",
  });
  if (finite.length < 2) return root;
  const lo = Math.min(...finite);
  const hi = Math.max(...finite);
  const n = vals.length;
  const pad = 2;
  const x = (i) => pad + (i / (n - 1)) * (width - 2 * pad);
  const y = (v) => height - pad - ((v - lo) / (hi - lo || 1)) * (height - 2 * pad);

  let d = "";
  let pen = false;
  vals.forEach((v, i) => {
    if (!isNum(v)) {
      pen = false;
      return;
    }
    d += (pen ? " L" : " M") + ` ${x(i).toFixed(2)} ${y(v).toFixed(2)}`;
    pen = true;
  });
  if (d) root.appendChild(svgEl("path", { d: d.trim(), fill: "none", stroke: color, "stroke-width": 1.3 }));
  return root;
}

// ---------------------------------------------------------------- helpers ----
function formatTick(v, step = null) {
  if (!isNum(v)) return "";
  const a = Math.abs(v);
  const scale = isNum(step) && step > 0 ? step : null;
  if (a !== 0 && (a < 0.01 || a >= 10000)) {
    const digits = scale ? Math.max(1, Math.min(8, Math.ceil(Math.log10(a / scale)))) : 1;
    return v.toExponential(digits);
  }
  const base = a < 1 ? 2 : a < 100 ? 1 : 0;
  const digits = scale ? Math.max(base, Math.min(8, Math.ceil(-Math.log10(scale)))) : base;
  if (digits > 0) return v.toFixed(digits);
  return Math.round(v).toLocaleString("en-US");
}

function shortDate(iso) {
  // 'YYYY-MM-DD' -> "YY-MM"
  if (typeof iso !== "string" || iso.length < 7) return String(iso);
  return iso.slice(2, 7);
}

/** Build a legend node for a set of series (returned as an HTML div, not SVG). */
export function legend(series) {
  const wrap = document.createElement("div");
  wrap.className = "chart-legend";
  series.forEach((s, i) => {
    const item = document.createElement("span");
    item.className = "chart-legend-item";
    const sw = document.createElement("span");
    sw.className = "chart-legend-swatch";
    sw.style.background = s.color || DEFAULT_COLORS[i % DEFAULT_COLORS.length];
    item.appendChild(sw);
    item.appendChild(document.createTextNode(s.name || ""));
    wrap.appendChild(item);
  });
  return wrap;
}

export const charts = {
  lineChart,
  barChart,
  calendarHeatmap,
  sparkline,
  diverging,
  seq,
  legend,
};
