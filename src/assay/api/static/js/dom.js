// dom.js — tiny vanilla DOM helpers. Zero dependencies; ES module.
//
// el(tag, attrs={}, ...children) builds an HTMLElement.
//   attrs:
//     className / class : string|array|object (via cx) -> element.className
//     dataset           : {k:v}                        -> data-* attributes
//     style             : {prop:value}                 -> inline styles
//     text              : string                       -> textContent
//     html              : string                       -> innerHTML (use sparingly)
//     onClick/onInput/… : function                     -> addEventListener('click'|'input'|…)
//     <any other>       : value                        -> setAttribute (booleans toggle; null/false skip)
//   children: nodes, strings, numbers, arrays (flattened); null/undefined/false ignored.

/** Join class values: strings, arrays, and {name:truthy} objects. */
export function cx(...parts) {
  const out = [];
  for (const p of parts) {
    if (!p) continue;
    if (typeof p === "string") out.push(p);
    else if (Array.isArray(p)) out.push(cx(...p));
    else if (typeof p === "object") {
      for (const [k, v] of Object.entries(p)) if (v) out.push(k);
    }
  }
  return out.join(" ");
}

function appendChild(node, child) {
  if (child === null || child === undefined || child === false || child === true) return;
  if (Array.isArray(child)) {
    for (const c of child) appendChild(node, c);
    return;
  }
  if (child instanceof Node) {
    node.appendChild(child);
    return;
  }
  node.appendChild(document.createTextNode(String(child)));
}

export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs && typeof attrs === "object" && !(attrs instanceof Node) && !Array.isArray(attrs)) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined) continue;
      if (key === "className" || key === "class") {
        node.className = cx(value);
      } else if (key === "dataset") {
        for (const [dk, dv] of Object.entries(value)) {
          if (dv !== null && dv !== undefined) node.dataset[dk] = dv;
        }
      } else if (key === "style" && typeof value === "object") {
        for (const [sk, sv] of Object.entries(value)) {
          if (sv !== null && sv !== undefined) node.style.setProperty(camelToKebab(sk), String(sv));
        }
      } else if (key === "text") {
        node.textContent = value;
      } else if (key === "html") {
        node.innerHTML = value;
      } else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (typeof value === "boolean") {
        if (value) node.setAttribute(key, "");
      } else {
        node.setAttribute(key, String(value));
      }
    }
  } else if (attrs !== undefined) {
    // attrs slot was actually a child
    children.unshift(attrs);
  }
  for (const child of children) appendChild(node, child);
  return node;
}

function camelToKebab(s) {
  return s.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
}

/** Remove all children of a node. */
export function clear(node) {
  if (node) node.replaceChildren();
  return node;
}

/** Format a number to `digits` decimals; null/NaN/undefined -> em dash. */
export function fmt(n, digits = 3) {
  if (n === null || n === undefined) return "—";
  const v = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}

/** Format a fraction as a percentage string (0.05 -> "5.0%"). */
export function pct(n, digits = 1) {
  if (n === null || n === undefined) return "—";
  const v = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(v)) return "—";
  return (v * 100).toFixed(digits) + "%";
}

/** Format an integer with grouping; null/NaN -> em dash. */
export function fmtInt(n) {
  if (n === null || n === undefined) return "—";
  const v = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(v)) return "—";
  return Math.round(v).toLocaleString("en-US");
}

/** Signed formatter: "+0.050" / "-0.050"; null/NaN -> em dash. */
export function fmtSigned(n, digits = 3) {
  if (n === null || n === undefined) return "—";
  const v = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(v)) return "—";
  return (v >= 0 ? "+" : "") + v.toFixed(digits);
}
