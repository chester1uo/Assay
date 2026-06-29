// api.js — ApiClient for the Assay /v1 REST surface + SSE evaluate streaming.
// Same-origin; auth is open by default. If localStorage 'assay_api_key' is set it
// is sent as the X-API-Key header. Non-2xx responses throw ApiError carrying the
// parsed {error:{...}} envelope (architecture §4.6) when present.

const API_KEY_STORAGE = "assay_api_key";

export class ApiError extends Error {
  constructor(message, { status, code, name, envelope } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.errorName = name;
    this.envelope = envelope || null;
  }
}

function authHeaders(extra = {}) {
  const headers = { ...extra };
  let key = null;
  try {
    key = localStorage.getItem(API_KEY_STORAGE);
  } catch (_) {
    key = null;
  }
  if (key) headers["X-API-Key"] = key;
  return headers;
}

async function parseError(res) {
  let envelope = null;
  let detailMsg = `${res.status} ${res.statusText}`;
  try {
    const body = await res.json();
    if (body && body.error) {
      envelope = body.error;
      if (envelope.message) detailMsg = envelope.message;
    } else if (body && typeof body.detail === "string") {
      detailMsg = body.detail;
    }
  } catch (_) {
    /* non-JSON error body — keep status line */
  }
  return new ApiError(detailMsg, {
    status: res.status,
    code: envelope ? envelope.code : undefined,
    name: envelope ? envelope.name : undefined,
    envelope,
  });
}

async function request(method, path, { body, query, signal } = {}) {
  let url = path;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v === null || v === undefined || v === "") continue;
      qs.append(k, Array.isArray(v) ? v.join(",") : String(v));
    }
    const s = qs.toString();
    if (s) url += "?" + s;
  }
  const init = { method, headers: authHeaders(), signal };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  if (!res.ok) throw await parseError(res);
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res.text();
}

export class ApiClient {
  constructor(base = "") {
    this.base = base;
  }

  _p(path) {
    return this.base + path;
  }

  // ---- system / health -----------------------------------------------------
  health() {
    return request("GET", this._p("/health"));
  }
  systemStatus() {
    return request("GET", this._p("/v1/system/status"));
  }
  universes() {
    return request("GET", this._p("/v1/system/universes"));
  }
  dataCalendar(market, year) {
    return request("GET", this._p("/v1/system/data-calendar"), { query: { market, year } });
  }

  // ---- factor --------------------------------------------------------------
  /** Blocking evaluation -> FactorReport JSON. */
  evaluate(req, { signal } = {}) {
    return request("POST", this._p("/v1/factor/evaluate"), {
      body: { ...req, stream: false },
      signal,
    });
  }

  /** Batch evaluation -> {total, elapsed_ms, reports}. */
  batch(req, { signal } = {}) {
    return request("POST", this._p("/v1/factor/batch"), { body: req, signal });
  }

  /** Data-free lint -> {dialect, canonical, fields, operators, ast, diagnostics}. */
  lint(expr, { signal } = {}) {
    return request("POST", this._p("/v1/factor/lint"), { body: { expr }, signal });
  }

  /**
   * Streaming evaluation over text/event-stream. Sends {...req, stream:true}.
   * Calls onEvent({event, data}) per parsed SSE frame; onError(err) on failure.
   * Returns an AbortController so callers can cancel the stream.
   */
  evaluateStream(req, onEvent, onError) {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await fetch(this._p("/v1/factor/evaluate"), {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" }),
          body: JSON.stringify({ ...req, stream: true }),
          signal: controller.signal,
        });
        if (!res.ok) {
          onError && onError(await parseError(res));
          return;
        }
        if (!res.body) {
          onError && onError(new ApiError("No response body for stream", { status: res.status }));
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        // SSE frames are separated by a blank line; each carries event:/data: lines.
        // The separator may be "\n\n" or "\r\n\r\n" (sse-starlette emits CRLF), so
        // match either — a plain indexOf("\n\n") never fires on CRLF streams and
        // would buffer the whole response into one un-parseable blob.
        const FRAME_SEP = /\r?\n\r?\n/;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let m;
          while ((m = FRAME_SEP.exec(buffer)) !== null) {
            const frame = buffer.slice(0, m.index);
            buffer = buffer.slice(m.index + m[0].length);
            const parsed = parseSseFrame(frame);
            if (parsed) onEvent(parsed);
          }
        }
        const tail = parseSseFrame(buffer);
        if (tail) onEvent(tail);
      } catch (err) {
        if (err && err.name === "AbortError") return;
        onError && onError(err);
      }
    })();
    return controller;
  }

  // ---- library -------------------------------------------------------------
  libraryList(params = {}) {
    return request("GET", this._p("/v1/library/factors"), { query: params });
  }
  libraryGet(id) {
    return request("GET", this._p(`/v1/library/factors/${encodeURIComponent(id)}`));
  }
  librarySave(report) {
    return request("POST", this._p("/v1/library/factors"), { body: report });
  }
  libraryDelete(factorIds) {
    return request("DELETE", this._p("/v1/library/factors"), { body: { factor_ids: factorIds } });
  }
  correlationMatrix(factorIds, { universe, period } = {}) {
    return request("GET", this._p("/v1/library/correlation-matrix"), {
      query: { factor_ids: factorIds, universe, period },
    });
  }
  prune({ redundancy_threshold, dry_run = true, factor_ids } = {}) {
    return request("POST", this._p("/v1/library/prune"), {
      body: { redundancy_threshold, dry_run, factor_ids },
    });
  }
  /** Bulk-import expressions: evaluate + save the good ones. body: {exprs, universe?, source?, period?}. */
  libraryBulkAdd(body, { signal } = {}) {
    return request("POST", this._p("/v1/library/factors/bulk"), { body, signal });
  }

  // ---- market data ---------------------------------------------------------
  /** OHLCV bars for one symbol. params: {symbol, freq, adj, start, end, as_of}. */
  marketBars({ symbol, freq, adj, start, end, as_of } = {}, { signal } = {}) {
    return request("GET", this._p("/v1/market/bars"), { query: { symbol, freq, adj, start, end, as_of }, signal });
  }
  /** Evaluate an alpha expression for one symbol -> {dates, values}. */
  marketFactorSeries(body, { signal } = {}) {
    return request("POST", this._p("/v1/market/factor-series"), { body, signal });
  }

  // ---- portfolio backtest --------------------------------------------------
  /** Run a portfolio backtest. `req` carries {expr, ...inline config fields} or
   *  {expr, config:{...}}. Returns the PortfolioReport dict. */
  portfolioBacktest(req, { signal } = {}) {
    return request("POST", this._p("/v1/portfolio/backtest"), { body: req, signal });
  }

  // ---- data manager (admin) ------------------------------------------------
  adminConfigGet() { return request("GET", this._p("/v1/admin/config")); }
  adminConfigPut(patch) { return request("PUT", this._p("/v1/admin/config"), { body: patch }); }
  adminDataStatus() { return request("GET", this._p("/v1/admin/data/status")); }
  adminJobStart(body) { return request("POST", this._p("/v1/admin/data/jobs"), { body }); }
  adminJobs() { return request("GET", this._p("/v1/admin/data/jobs")); }
  adminJob(id) { return request("GET", this._p(`/v1/admin/data/jobs/${encodeURIComponent(id)}`)); }

  // ---- session -------------------------------------------------------------
  createSession({ universe, period } = {}) {
    return request("POST", this._p("/v1/session/create"), { body: { universe, period } });
  }
}

/** Parse one raw SSE frame into {event, data}; data JSON-parsed when possible. */
function parseSseFrame(frame) {
  if (!frame || !frame.trim()) return null;
  let event = "message";
  const dataLines = [];
  for (const rawLine of frame.split("\n")) {
    const line = rawLine.replace(/\r$/, "");
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).replace(/^ /, ""));
    }
  }
  const dataStr = dataLines.join("\n");
  let data = dataStr;
  if (dataStr) {
    try {
      data = JSON.parse(dataStr);
    } catch (_) {
      data = dataStr;
    }
  }
  return { event, data };
}

/** Default same-origin client. */
export const api = new ApiClient("");
