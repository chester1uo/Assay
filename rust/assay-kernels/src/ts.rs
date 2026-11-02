//! Rolling (time-series) kernels: `ts_*` over trailing windows of length `d`.
//!
//! Same contract as `assay.engine.operators.time_series`: the window ending at row `t`
//! is *full*, so the first `d - 1` rows and any window holding a NaN give NaN.
//! Columns (symbols) are independent and are spread over the rayon pool.
//!
//! Sums, means, variances, covariances, correlations, linear decay and the
//! regression-vs-time family are updated in O(1) per step instead of O(d): running
//! sums of `x - K` (a shift taken from the window, so a price level does not cancel
//! away the variance) are rebuilt from the window every `max(d, 16)` steps, and
//! sooner when the values that passed through dwarf the window's variance, which
//! bounds rounding drift. Results agree with the numpy kernels to ~1e-12 relative,
//! not bit for bit. Two deliberate differences: a window of identical values gives
//! exactly zero variance (so `ts_corr` / `ts_rsquare` are NaN there, where numpy
//! may return noise from a rounded mean), and correlations are clamped to [-1, 1].
//! Windows holding ±inf are evaluated directly with the numpy formula (inf / NaN as
//! numpy gives them). Min/max/argmin/argmax (monotonic deques), rank and count are
//! exact; skew, kurtosis and mean absolute deviation are computed per window.

use rayon::prelude::*;
use std::collections::VecDeque;

const NAN: f64 = f64::NAN;
/// Columns gathered per task: a few cache lines per row of the row-major input.
const BLOCK: usize = 16;

/// Raw pointer to the row-major output, shared by tasks that write disjoint columns.
#[derive(Clone, Copy)]
struct Out(*mut f64);
unsafe impl Send for Out {}
unsafe impl Sync for Out {}

/// Runs `f(columns, out)` on every column of row-major `(t, n)` inputs and returns
/// the row-major result. `out` starts as NaN. Each task gathers [`BLOCK`] columns
/// (a few cache lines per row), runs them, and writes them back into its own
/// columns of the result.
pub fn by_column<F>(srcs: &[&[f64]], t: usize, n: usize, f: F) -> Vec<f64>
where
    F: Fn(&[&[f64]], &mut [f64]) + Sync,
{
    let mut out = vec![NAN; t * n];
    if t == 0 || n == 0 {
        return out;
    }
    let dst = Out(out.as_mut_ptr());
    (0..n.div_ceil(BLOCK)).into_par_iter().for_each_init(
        || (vec![0.0f64; srcs.len() * BLOCK * t], vec![NAN; BLOCK * t]),
        |(buf, res), blk| {
            let j0 = blk * BLOCK;
            let b = BLOCK.min(n - j0);
            for (s, src) in srcs.iter().enumerate() {
                let base = s * BLOCK * t;
                for r in 0..t {
                    let row = &src[r * n + j0..r * n + j0 + b];
                    for k in 0..b {
                        buf[base + k * t + r] = row[k];
                    }
                }
            }
            res.fill(NAN);
            let mut cols: Vec<&[f64]> = Vec::with_capacity(srcs.len());
            for k in 0..b {
                cols.clear();
                for s in 0..srcs.len() {
                    let base = s * BLOCK * t + k * t;
                    cols.push(&buf[base..base + t]);
                }
                f(&cols, &mut res[k * t..(k + 1) * t]);
            }
            let dst = dst;
            for r in 0..t {
                // SAFETY: block `blk` owns columns j0..j0+b of every row; no other task
                // writes them, and `out` outlives the parallel loop.
                let row = unsafe { std::slice::from_raw_parts_mut(dst.0.add(r * n + j0), b) };
                for k in 0..b {
                    row[k] = res[k * t + r];
                }
            }
        },
    );
    out
}

/// Running sums of `u = x - k` over the window: Σu, Σu², Σ i·u (i = 0 oldest), and
/// the largest u² seen since the last rebuild (what bounds the rounding error).
#[derive(Default, Clone, Copy)]
struct Sums {
    k: f64,
    s1: f64,
    s2: f64,
    sw: f64,
    peak: f64,
}

impl Sums {
    /// From the window, shifted by its first finite value; non-finite values count as 0.
    fn rebuild(&mut self, w: &[f64]) {
        self.k = w.iter().copied().find(|v| v.is_finite()).unwrap_or(0.0);
        let (mut s1, mut s2, mut sw, mut peak) = (0.0, 0.0, 0.0, 0.0f64);
        for (i, &v) in w.iter().enumerate() {
            if v.is_finite() {
                let u = v - self.k;
                s1 += u;
                s2 += u * u;
                sw += i as f64 * u;
                peak = peak.max(u * u);
            }
        }
        (self.s1, self.s2, self.sw, self.peak) = (s1, s2, sw, peak);
    }

    /// The running sums can no longer be trusted to ~1e-12 of the window variance.
    fn stale(&self, n: f64) -> bool {
        stale(self.peak, self.s1, self.s2, n)
    }

    fn u(&self, v: f64) -> f64 {
        if v.is_finite() {
            v - self.k
        } else {
            0.0
        }
    }

    /// Drop `old` (the oldest value), append `new`.
    fn slide(&mut self, old: f64, new: f64, d: usize) {
        let (uo, ui) = (self.u(old), self.u(new));
        self.sw += uo - self.s1 + (d - 1) as f64 * ui;
        self.s1 += ui - uo;
        self.s2 += ui * ui - uo * uo;
        self.peak = self.peak.max(uo * uo).max(ui * ui);
    }
}

/// Rounding in running sums of u² is ~ε·max u² per update, so once max u² outgrows the
/// window's own variance (the level drifted away from the shift, or a spike passed
/// through) the sums are rebuilt from the window.
const TRUST: f64 = 256.0;

fn stale(peak: f64, s1: f64, s2: f64, n: f64) -> bool {
    peak > TRUST * centered_ss(s1, s2, n) / n
}

/// Per-window bookkeeping shared by the drivers: NaN / inf counts and the length of
/// the run of equal values ending at the current row.
struct Track {
    nan: usize,
    inf: usize,
    run: usize,
}

impl Track {
    fn new() -> Self {
        Track { nan: 0, inf: 0, run: 0 }
    }
    fn add(&mut self, v: f64, sign: isize) {
        let c = if v.is_nan() {
            &mut self.nan
        } else if v.is_infinite() {
            &mut self.inf
        } else {
            return;
        };
        *c = (*c as isize + sign) as usize;
    }
    fn push(&mut self, x: &[f64], t: usize, d: usize) {
        self.add(x[t], 1);
        if t >= d {
            self.add(x[t - d], -1);
        }
        self.run = if t > 0 && x[t] == x[t - 1] { self.run + 1 } else { 1 };
    }
}

fn rebuild_every(d: usize) -> usize {
    d.max(16)
}

/// One-series driver over shifted sums. `f(sums, constant, x_t)` gives the value of a
/// full finite window; a window holding ±inf goes to `direct(window)` (the numpy formula).
fn roll_sums(
    x: &[f64],
    d: usize,
    out: &mut [f64],
    f: impl Fn(&Sums, bool, f64) -> f64,
    direct: impl Fn(&[f64]) -> f64,
) {
    let tn = x.len();
    if d == 0 || d > tn {
        return;
    }
    let mut tr = Track::new();
    let mut s = Sums::default();
    let mut since = 0;
    for t in 0..tn {
        tr.push(x, t, d);
        if t + 1 < d {
            continue;
        }
        let w = &x[t + 1 - d..=t];
        if t + 1 == d || since >= rebuild_every(d) {
            s.rebuild(w);
            since = 0;
        } else {
            s.slide(x[t - d], x[t], d);
            since += 1;
            if tr.run < d && s.stale(d as f64) {
                s.rebuild(w);
                since = 0;
            }
        }
        out[t] = if tr.nan > 0 {
            NAN
        } else if tr.inf > 0 {
            direct(w)
        } else {
            f(&s, tr.run >= d, x[t])
        };
    }
}

/// Paired running sums for cov / corr: shifts `kx`, `ky` and Σu, Σv, Σu², Σv², Σuv,
/// with the peaks of u² and v² as in [`Sums`].
#[derive(Default, Clone, Copy)]
struct Pair {
    kx: f64,
    ky: f64,
    sx: f64,
    sy: f64,
    sxx: f64,
    syy: f64,
    sxy: f64,
    px: f64,
    py: f64,
}

impl Pair {
    fn rebuild(&mut self, x: &[f64], y: &[f64]) {
        let first = |w: &[f64]| w.iter().copied().find(|v| v.is_finite()).unwrap_or(0.0);
        let (kx, ky) = (first(x), first(y));
        *self = Pair { kx, ky, ..Pair::default() };
        for i in 0..x.len() {
            let (u, v) = (self.ux(x[i]), self.uy(y[i]));
            self.add(u, v, 1.0);
        }
    }
    fn ux(&self, v: f64) -> f64 {
        if v.is_finite() {
            v - self.kx
        } else {
            0.0
        }
    }
    fn uy(&self, v: f64) -> f64 {
        if v.is_finite() {
            v - self.ky
        } else {
            0.0
        }
    }
    fn add(&mut self, u: f64, v: f64, sign: f64) {
        self.sx += sign * u;
        self.sy += sign * v;
        self.sxx += sign * u * u;
        self.syy += sign * v * v;
        self.sxy += sign * u * v;
        self.px = self.px.max(u * u);
        self.py = self.py.max(v * v);
    }
    fn stale(&self, n: f64, cx: bool, cy: bool) -> bool {
        (!cx && stale(self.px, self.sx, self.sxx, n)) || (!cy && stale(self.py, self.sy, self.syy, n))
    }
}

/// Two-series driver; `f(pair, x_constant, y_constant)`, `direct(wx, wy)` for ±inf windows.
fn roll_pair(
    x: &[f64],
    y: &[f64],
    d: usize,
    out: &mut [f64],
    f: impl Fn(&Pair, bool, bool) -> f64,
    direct: impl Fn(&[f64], &[f64]) -> f64,
) {
    let tn = x.len();
    if d == 0 || d > tn {
        return;
    }
    let (mut tx, mut ty) = (Track::new(), Track::new());
    let mut p = Pair::default();
    let mut since = 0;
    for t in 0..tn {
        tx.push(x, t, d);
        ty.push(y, t, d);
        if t + 1 < d {
            continue;
        }
        let (wx, wy) = (&x[t + 1 - d..=t], &y[t + 1 - d..=t]);
        if t + 1 == d || since >= rebuild_every(d) {
            p.rebuild(wx, wy);
            since = 0;
        } else {
            let (uo, vo) = (p.ux(x[t - d]), p.uy(y[t - d]));
            let (ui, vi) = (p.ux(x[t]), p.uy(y[t]));
            p.add(uo, vo, -1.0);
            p.add(ui, vi, 1.0);
            since += 1;
            if p.stale(d as f64, tx.run >= d, ty.run >= d) {
                p.rebuild(wx, wy);
                since = 0;
            }
        }
        out[t] = if tx.nan + ty.nan > 0 {
            NAN
        } else if tx.inf + ty.inf > 0 {
            direct(wx, wy)
        } else {
            f(&p, tx.run >= d, ty.run >= d)
        };
    }
}

/// Σ (x - x̄)² from shifted sums, never negative.
fn centered_ss(s1: f64, s2: f64, n: f64) -> f64 {
    (s2 - s1 * s1 / n).max(0.0)
}

fn mean(w: &[f64]) -> f64 {
    w.iter().sum::<f64>() / w.len() as f64
}

/// Two-pass Σ (x - x̄)(y - ȳ), Σ (x - x̄)², Σ (y - ȳ)² (numpy's demeaned windows).
fn comoments(x: &[f64], y: &[f64]) -> (f64, f64, f64) {
    let (mx, my) = (mean(x), mean(y));
    let (mut c, mut vx, mut vy) = (0.0, 0.0, 0.0);
    for i in 0..x.len() {
        let (a, b) = (x[i] - mx, y[i] - my);
        c += a * b;
        vx += a * a;
        vy += b * b;
    }
    (c, vx, vy)
}

/// Σ w_i (i - t̄) over the window, i = 0 oldest.
fn sum_tc(w: &[f64], tbar: f64) -> f64 {
    w.iter().enumerate().map(|(i, v)| v * (i as f64 - tbar)).sum()
}

/// Monotonic-deque extreme over the window: value (`want_arg = false`) or days since
/// it (`true`; ties go to the oldest, like numpy's first-occurrence argmax).
fn roll_extreme(x: &[f64], d: usize, out: &mut [f64], want_max: bool, want_arg: bool) {
    let tn = x.len();
    if d == 0 || d > tn {
        return;
    }
    let mut tr = Track::new();
    let mut q: VecDeque<usize> = VecDeque::with_capacity(d);
    // `a` displaces `b` from the back of the deque
    let beats = |a: f64, b: f64| if want_max { b < a } else { b > a };
    for t in 0..tn {
        tr.push(x, t, d);
        let v = x[t];
        if !v.is_nan() {
            while let Some(&b) = q.back() {
                if beats(v, x[b]) {
                    q.pop_back();
                } else {
                    break;
                }
            }
            q.push_back(t);
        }
        if t + 1 < d {
            continue;
        }
        while let Some(&f) = q.front() {
            if f + d <= t {
                q.pop_front();
            } else {
                break;
            }
        }
        if tr.nan > 0 {
            continue;
        }
        let f = *q.front().expect("a NaN-free full window is non-empty");
        out[t] = if want_arg { (t - f) as f64 } else { x[f] };
    }
}

/// Per-window kernel `f(window)` on every full NaN-free window (O(d) each).
fn per_window(x: &[f64], d: usize, out: &mut [f64], f: impl Fn(&[f64]) -> f64) {
    let tn = x.len();
    if d == 0 || d > tn {
        return;
    }
    let mut tr = Track::new();
    for t in 0..tn {
        tr.push(x, t, d);
        if t + 1 >= d && tr.nan == 0 {
            out[t] = f(&x[t + 1 - d..=t]);
        }
    }
}

/// Mean, then the central moments Σdev², Σdev^p / n as numpy computes them (two pass).
fn central(w: &[f64], p: i32) -> (f64, f64) {
    let n = w.len() as f64;
    let m = w.iter().sum::<f64>() / n;
    let (mut m2, mut mp) = (0.0, 0.0);
    for &v in w {
        let dv = v - m;
        m2 += dv * dv;
        mp += dv.powi(p);
    }
    (mp / n, m2 / n)
}

/// One-series kernel `op` on a column.
pub fn unary(op: &str, x: &[f64], d: usize, out: &mut [f64]) {
    let n = d as f64;
    let tbar = (n - 1.0) / 2.0;
    let stt: f64 = (0..d).map(|i| (i as f64 - tbar).powi(2)).sum();
    let wsum = n * (n + 1.0) / 2.0;
    match op {
        "sum" => roll_sums(x, d, out, |s, c, v| if c { v * n } else { s.k * n + s.s1 }, |w| w.iter().sum()),
        "mean" => roll_sums(x, d, out, |s, c, v| if c { v } else { s.k + s.s1 / n }, mean),
        "var" => roll_sums(x, d, out, |s, c, _| if c { 0.0 } else { centered_ss(s.s1, s.s2, n) / (n - 1.0) }, |w| {
            comoments(w, w).1 / (n - 1.0)
        }),
        "std" => roll_sums(x, d, out, |s, c, _| if c { 0.0 } else { (centered_ss(s.s1, s.s2, n) / (n - 1.0)).sqrt() }, |w| {
            (comoments(w, w).1 / (n - 1.0)).sqrt()
        }),
        // Σ (i + 1) x_i / (d (d + 1) / 2): newest weighs most
        "decay_linear" => roll_sums(x, d, out, |s, c, v| if c { v } else { s.k + (s.sw + s.s1) / wsum }, |w| {
            w.iter().enumerate().map(|(i, v)| v * (i + 1) as f64).sum::<f64>() / wsum
        }),
        // OLS of x on t = 0..d-1; Σ u (t - t̄) = Σ i u - t̄ Σ u does not depend on the shift
        "slope" => roll_sums(x, d, out, |s, c, _| if c { 0.0 } else { (s.sw - tbar * s.s1) / stt }, |w| {
            sum_tc(w, tbar) / stt
        }),
        // x_t - (intercept + slope (d - 1)) = (x_t - x̄) - slope t̄
        "resi" => roll_sums(x, d, out, |s, c, v| {
            if c { 0.0 } else { (v - s.k) - s.s1 / n - (s.sw - tbar * s.s1) / stt * tbar }
        }, |w| {
            let slope = sum_tc(w, tbar) / stt;
            let intercept = mean(w) - slope * tbar;
            w[w.len() - 1] - (intercept + slope * (n - 1.0))
        }),
        "rsquare" => roll_sums(x, d, out, |s, c, _| {
            let sxx = if c { 0.0 } else { centered_ss(s.s1, s.s2, n) };
            let sxt = s.sw - tbar * s.s1;
            if sxx == 0.0 { NAN } else { (sxt * sxt / (stt * sxx)).min(1.0) }
        }, |w| {
            let (sxt, sxx) = (sum_tc(w, tbar), comoments(w, w).1);
            if sxx == 0.0 { NAN } else { sxt * sxt / (stt * sxx) }
        }),
        "min" => roll_extreme(x, d, out, false, false),
        "max" => roll_extreme(x, d, out, true, false),
        "argmin" => roll_extreme(x, d, out, false, true),
        "argmax" => roll_extreme(x, d, out, true, true),
        "rank" => per_window(x, d, out, |w| {
            let today = w[w.len() - 1];
            let le = w.iter().filter(|&&v| v <= today).count();
            (le - 1) as f64 / (n - 1.0)
        }),
        "count" => per_window(x, d, out, |w| w.iter().filter(|&&v| v != 0.0).count() as f64),
        "skew" => per_window(x, d, out, |w| {
            let (m3, m2) = central(w, 3);
            if m2 == 0.0 {
                NAN
            } else {
                m3 / m2.powf(1.5)
            }
        }),
        "kurt" => per_window(x, d, out, |w| {
            let (m4, m2) = central(w, 4);
            if m2 == 0.0 {
                NAN
            } else {
                m4 / (m2 * m2) - 3.0
            }
        }),
        "mad" => per_window(x, d, out, |w| {
            let m = w.iter().sum::<f64>() / n;
            w.iter().map(|v| (v - m).abs()).sum::<f64>() / n
        }),
        _ => unreachable!("checked by is_unary"),
    }
}

pub const UNARY: &[&str] = &[
    "sum", "mean", "var", "std", "decay_linear", "slope", "resi", "rsquare", "min", "max", "argmin",
    "argmax", "rank", "count", "skew", "kurt", "mad",
];
pub const BINARY: &[&str] = &["cov", "corr"];

/// Two-series kernel `op` on a pair of columns.
pub fn binary(op: &str, x: &[f64], y: &[f64], d: usize, out: &mut [f64]) {
    let n = d as f64;
    match op {
        "cov" => roll_pair(x, y, d, out, |p, cx, cy| {
            if cx || cy {
                0.0
            } else {
                (p.sxy - p.sx * p.sy / n) / (n - 1.0)
            }
        }, |wx, wy| comoments(wx, wy).0 / (n - 1.0)),
        "corr" => roll_pair(x, y, d, out, |p, cx, cy| {
            if cx || cy {
                return NAN;
            }
            let vx = p.sxx - p.sx * p.sx / n;
            let vy = p.syy - p.sy * p.sy / n;
            let denom = vx.max(0.0).sqrt() * vy.max(0.0).sqrt();
            if denom == 0.0 {
                return NAN;
            }
            ((p.sxy - p.sx * p.sy / n) / denom).clamp(-1.0, 1.0)
        }, |wx, wy| {
            let (c, vx, vy) = comoments(wx, wy);
            let denom = vx.sqrt() * vy.sqrt();
            if denom == 0.0 { NAN } else { c / denom }
        }),
        _ => unreachable!("checked by BINARY"),
    }
}
