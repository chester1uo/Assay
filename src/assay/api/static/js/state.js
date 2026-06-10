// state.js — global store (pub-sub, localStorage + URL sync) and a hash router.

const STORAGE_KEY = "assay_state";

const DEFAULTS = {
  universe: "NASDAQ100",
  period: ["2020-01-01", "2024-12-31"],
};

const _isFactoryPeriod = (p) =>
  Array.isArray(p) && p[0] === DEFAULTS.period[0] && p[1] === DEFAULTS.period[1];

function loadInitial() {
  const state = { ...DEFAULTS, period: [...DEFAULTS.period] };
  // True once the user has picked a *non-default* period (storage or URL). A
  // persisted factory-default period is NOT treated as user-set, so the app can
  // still adopt the ingested data range for returning visitors.
  let periodUserSet = false;
  // localStorage first, then URL query overrides.
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const saved = JSON.parse(raw);
      if (saved.universe) state.universe = saved.universe;
      if (Array.isArray(saved.period) && saved.period.length === 2) {
        state.period = saved.period;
        if (!_isFactoryPeriod(saved.period)) periodUserSet = true;
      }
    }
  } catch (_) {
    /* ignore corrupt storage */
  }
  try {
    const q = new URLSearchParams(window.location.search);
    if (q.get("universe")) state.universe = q.get("universe");
    const ps = q.get("period_start");
    const pe = q.get("period_end");
    if (ps && pe) {
      state.period = [ps, pe];
      if (!_isFactoryPeriod([ps, pe])) periodUserSet = true;
    }
  } catch (_) {
    /* no window/search */
  }
  return { state, periodUserSet };
}

class Store {
  constructor(initial, periodUserSet) {
    this._state = initial;
    this._subs = new Set();
    // Whether the user explicitly chose a period. When false, the app may adopt
    // the ingested data range (see applyDataDefaultPeriod) so evaluation defaults
    // to dates that actually have data instead of the empty factory range.
    this.periodIsUserSet = !!periodUserSet;
  }

  /** Mark the period as user-chosen so data-range auto-defaulting stops overriding it. */
  markPeriodUserSet() {
    this.periodIsUserSet = true;
  }

  /** Adopt the ingested [first,last] data range as the period — unless the user set one. */
  applyDataDefaultPeriod(first, last) {
    if (this.periodIsUserSet || !first || !last) return;
    const cur = this._state.period;
    if (cur && cur[0] === first && cur[1] === last) return;
    this.set({ period: [first, last] });
  }

  get(key) {
    return key ? this._state[key] : { ...this._state };
  }

  /** Merge a patch, persist, sync to URL, notify subscribers (if anything changed). */
  set(patch) {
    let changed = false;
    for (const [k, v] of Object.entries(patch)) {
      if (JSON.stringify(this._state[k]) !== JSON.stringify(v)) {
        this._state[k] = v;
        changed = true;
      }
    }
    if (!changed) return;
    this._persist();
    this._syncUrl();
    this._emit();
  }

  subscribe(fn) {
    this._subs.add(fn);
    return () => this._subs.delete(fn);
  }

  _emit() {
    const snap = this.get();
    for (const fn of this._subs) {
      try {
        fn(snap);
      } catch (err) {
        console.error("store subscriber error", err);
      }
    }
  }

  _persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(this._state));
    } catch (_) {
      /* storage may be unavailable */
    }
  }

  _syncUrl() {
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("universe", this._state.universe);
      url.searchParams.set("period_start", this._state.period[0]);
      url.searchParams.set("period_end", this._state.period[1]);
      window.history.replaceState(null, "", url);
    } catch (_) {
      /* no history API */
    }
  }
}

const _initial = loadInitial();
export const store = new Store(_initial.state, _initial.periodUserSet);

// ---------------------------------------------------------------- router ----

class HashRouter {
  constructor() {
    this._routes = []; // {pattern, keys, render}
    this._notFound = null;
    this._listening = false;
    this._onHash = () => this._dispatch();
  }

  /** Register a route. Path supports ':param' segments, e.g. '#/factor/:id'. */
  route(path, render) {
    const norm = path.replace(/^#/, "");
    const keys = [];
    const pattern = new RegExp(
      "^" +
        norm
          .replace(/\//g, "\\/")
          .replace(/:(\w+)/g, (_, k) => {
            keys.push(k);
            return "([^\\/]+)";
          }) +
        "$"
    );
    this._routes.push({ pattern, keys, render });
    return this;
  }

  notFound(render) {
    this._notFound = render;
    return this;
  }

  /** Current hash path without the leading '#'. */
  current() {
    return (window.location.hash || "").replace(/^#/, "") || "/dashboard";
  }

  navigate(hash) {
    const target = hash.startsWith("#") ? hash : "#" + hash;
    if (window.location.hash === target) {
      this._dispatch();
    } else {
      window.location.hash = target;
    }
  }

  start(defaultHash = "#/dashboard") {
    if (!this._listening) {
      window.addEventListener("hashchange", this._onHash);
      this._listening = true;
    }
    if (!window.location.hash) {
      window.location.hash = defaultHash;
      return; // hashchange will dispatch
    }
    this._dispatch();
  }

  _dispatch() {
    const path = this.current();
    for (const r of this._routes) {
      const m = path.match(r.pattern);
      if (m) {
        const params = {};
        r.keys.forEach((k, i) => (params[k] = decodeURIComponent(m[i + 1])));
        r.render({ path, params });
        return;
      }
    }
    if (this._notFound) this._notFound({ path, params: {} });
  }
}

export const router = new HashRouter();
