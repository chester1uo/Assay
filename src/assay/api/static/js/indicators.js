// indicators.js — pure technical-indicator math over OHLCV arrays. No DOM.
//
// Every function takes plain number arrays (aligned by bar index) and returns
// arrays of the same length, NaN during the warm-up window. Used by the chart
// page to draw overlays (MA/BOLL) and subpanels (MACD/RSI/KDJ/ATR/OBV).

const NA = NaN;
const isN = (v) => typeof v === "number" && Number.isFinite(v);

/** Simple moving average. */
export function sma(values, n) {
  const out = new Array(values.length).fill(NA);
  let sum = 0;
  const q = [];
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    q.push(v);
    if (isN(v)) sum += v;
    if (q.length > n) {
      const old = q.shift();
      if (isN(old)) sum -= old;
    }
    if (q.length === n && q.every(isN)) out[i] = sum / n;
  }
  return out;
}

/** Exponential moving average (Wilder-free, standard 2/(n+1) smoothing). */
export function ema(values, n) {
  const out = new Array(values.length).fill(NA);
  const k = 2 / (n + 1);
  let prev = null;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (!isN(v)) { out[i] = prev == null ? NA : prev; continue; }
    prev = prev == null ? v : v * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

/** Rolling standard deviation (population) over n. */
function rollStd(values, n, means) {
  const out = new Array(values.length).fill(NA);
  for (let i = n - 1; i < values.length; i++) {
    const m = means[i];
    if (!isN(m)) continue;
    let s = 0, ok = true;
    for (let j = i - n + 1; j <= i; j++) {
      if (!isN(values[j])) { ok = false; break; }
      s += (values[j] - m) ** 2;
    }
    if (ok) out[i] = Math.sqrt(s / n);
  }
  return out;
}

/** Bollinger Bands: {mid, upper, lower} (mid=SMA(n), band=k*std). */
export function boll(close, n = 20, k = 2) {
  const mid = sma(close, n);
  const sd = rollStd(close, n, mid);
  const upper = mid.map((m, i) => (isN(m) && isN(sd[i]) ? m + k * sd[i] : NA));
  const lower = mid.map((m, i) => (isN(m) && isN(sd[i]) ? m - k * sd[i] : NA));
  return { mid, upper, lower };
}

/** MACD: {dif, dea, hist}. dif=EMA(fast)-EMA(slow); dea=EMA(dif,signal); hist=2*(dif-dea). */
export function macd(close, fast = 12, slow = 26, signal = 9) {
  const ef = ema(close, fast);
  const es = ema(close, slow);
  const dif = ef.map((v, i) => (isN(v) && isN(es[i]) ? v - es[i] : NA));
  const dea = ema(dif.map((v) => (isN(v) ? v : 0)), signal);
  const hist = dif.map((v, i) => (isN(v) && isN(dea[i]) ? 2 * (v - dea[i]) : NA));
  return { dif, dea, hist };
}

/** Wilder RSI over n. */
export function rsi(close, n = 14) {
  const out = new Array(close.length).fill(NA);
  let avgG = 0, avgL = 0;
  for (let i = 1; i < close.length; i++) {
    const ch = close[i] - close[i - 1];
    const g = ch > 0 ? ch : 0;
    const l = ch < 0 ? -ch : 0;
    if (i <= n) {
      avgG += g; avgL += l;
      if (i === n) {
        avgG /= n; avgL /= n;
        out[i] = avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL);
      }
    } else {
      avgG = (avgG * (n - 1) + g) / n;
      avgL = (avgL * (n - 1) + l) / n;
      out[i] = avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL);
    }
  }
  return out;
}

/** KDJ: {k, d, j} from the n-period stochastic (RSV smoothed by kP/dP). */
export function kdj(high, low, close, n = 9, kP = 3, dP = 3) {
  const rsv = new Array(close.length).fill(NA);
  for (let i = n - 1; i < close.length; i++) {
    let hh = -Infinity, ll = Infinity, ok = true;
    for (let j = i - n + 1; j <= i; j++) {
      if (!isN(high[j]) || !isN(low[j])) { ok = false; break; }
      hh = Math.max(hh, high[j]); ll = Math.min(ll, low[j]);
    }
    if (ok && hh !== ll) rsv[i] = ((close[i] - ll) / (hh - ll)) * 100;
    else if (ok) rsv[i] = 0;
  }
  const k = new Array(close.length).fill(NA);
  const d = new Array(close.length).fill(NA);
  const j = new Array(close.length).fill(NA);
  let pk = 50, pd = 50;
  for (let i = 0; i < close.length; i++) {
    if (!isN(rsv[i])) continue;
    pk = (pk * (kP - 1) + rsv[i]) / kP;
    pd = (pd * (dP - 1) + pk) / dP;
    k[i] = pk; d[i] = pd; j[i] = 3 * pk - 2 * pd;
  }
  return { k, d, j };
}

/** Average True Range (Wilder) over n. */
export function atr(high, low, close, n = 14) {
  const tr = new Array(close.length).fill(NA);
  for (let i = 0; i < close.length; i++) {
    if (!isN(high[i]) || !isN(low[i])) continue;
    if (i === 0) { tr[i] = high[i] - low[i]; continue; }
    tr[i] = Math.max(high[i] - low[i], Math.abs(high[i] - close[i - 1]), Math.abs(low[i] - close[i - 1]));
  }
  const out = new Array(close.length).fill(NA);
  let prev = null, acc = 0;
  for (let i = 0; i < close.length; i++) {
    if (!isN(tr[i])) continue;
    if (prev == null) {
      acc += tr[i];
      if (i >= n - 1) { prev = acc / n; out[i] = prev; }
    } else {
      prev = (prev * (n - 1) + tr[i]) / n;
      out[i] = prev;
    }
  }
  return out;
}

/** On-Balance Volume. */
export function obv(close, volume) {
  const out = new Array(close.length).fill(NA);
  let acc = 0;
  for (let i = 0; i < close.length; i++) {
    if (!isN(close[i]) || !isN(volume[i])) { out[i] = acc; continue; }
    if (i > 0 && isN(close[i - 1])) {
      if (close[i] > close[i - 1]) acc += volume[i];
      else if (close[i] < close[i - 1]) acc -= volume[i];
    }
    out[i] = acc;
  }
  return out;
}
