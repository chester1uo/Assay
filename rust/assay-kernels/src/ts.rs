//! Rolling (time-series) kernels: `ts_*` over trailing windows of length `d`.
//!
//! Same contract as `assay.engine.operators.time_series`: the window ending at row `t`
//! is *full*, so the first `d - 1` rows and any window holding a NaN give NaN.
//!
//! Row sweep: every rayon task owns a block of [`LANES`] columns and walks the rows top
//! to bottom, keeping one small state per column. Each step reads the block's part of a
//! row as one contiguous run and writes its part of the output row in place, so the
//! row-major input is never transposed and the inner loops run across columns.
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
//! numpy gives them). Min/max/argmin/argmax (van Herk / Gil-Werman blocks), rank and
//! count are exact; rank, count, skew, kurtosis and mean absolute deviation are computed
//! per window on the block gathered column by column.

use rayon::prelude::*;

const NAN: f64 = f64::NAN;
/// Columns per task: each row of a row-major input is read as one 512-byte run.
const LANES: usize = 64;

/// Raw pointer to the row-major output, shared by tasks that write disjoint columns.
#[derive(Clone, Copy)]
struct Out(*mut f64);
unsafe impl Send for Out {}
unsafe impl Sync for Out {}

/// One task's share of a sweep: columns `j0..j0 + b` of the row-major `(t, n)` inputs
/// and of the output.
pub struct Cols<'a> {
    srcs: &'a [&'a [f64]],
    out: Out,
    t: usize,
    n: usize,
    j0: usize,
    b: usize,
}

impl Cols<'_> {
    /// This block's part of row `r` of input `s`.
    fn row(&self, s: usize, r: usize) -> &[f64] {
        let o = r * self.n + self.j0;
        &self.srcs[s][o..o + self.b]
    }

    /// This block's part of row `r` of the output.
    #[allow(clippy::mut_from_ref)]
    fn out(&self, r: usize) -> &mut [f64] {
        assert!(r < self.t);
        // SAFETY: the task owns columns j0..j0+b of every output row, no other task
        // touches them, and each driver holds one output row at a time.
        unsafe { std::slice::from_raw_parts_mut(self.out.0.add(r * self.n + self.j0), self.b) }
    }

    /// Column `k` of input `s` over rows `r0..r0 + len`, into `buf` (strided; used
    /// only for the rare per-column rebuilds and ±inf windows).
    fn column(&self, s: usize, k: usize, r0: usize, len: usize, buf: &mut Vec<f64>) {
        let (src, j) = (self.srcs[s], self.j0 + k);
        buf.clear();
        buf.extend((r0..r0 + len).map(|r| src[r * self.n + j]));
    }
}

/// Runs `f` on every block of [`LANES`] columns of row-major `(t, n)` inputs and
/// returns the row-major result; `f` must write every row of its columns.
pub fn sweep<F>(srcs: &[&[f64]], t: usize, n: usize, f: F) -> Vec<f64>
where
    F: Fn(&Cols) + Sync,
{
    let mut out = vec![0.0f64; t * n];
    if t == 0 || n == 0 {
        return out;
    }
    let dst = Out(out.as_mut_ptr());
    (0..n.div_ceil(LANES)).into_par_iter().for_each(|blk| {
        let j0 = blk * LANES;
        f(&Cols { srcs, out: dst, t, n, j0, b: LANES.min(n - j0) });
    });
    out
}

/// Per-column window bookkeeping: NaN / inf counts and the length of the run of
/// equal values ending at the current row.
struct Track {
    nan: Vec<u32>,
    inf: Vec<u32>,
    run: Vec<u32>,
}

impl Track {
    fn new(b: usize) -> Self {
        Track { nan: vec![0; b], inf: vec![0; b], run: vec![0; b] }
    }

    /// Row `t` of input `s` enters the window, row `t - d` leaves it.
    fn push(&mut self, c: &Cols, s: usize, t: usize, d: usize) {
        let cur = c.row(s, t);
        for k in 0..c.b {
            self.nan[k] += cur[k].is_nan() as u32;
            self.inf[k] += cur[k].is_infinite() as u32;
        }
        if t >= d {
            let old = c.row(s, t - d);
            for k in 0..c.b {
                self.nan[k] -= old[k].is_nan() as u32;
                self.inf[k] -= old[k].is_infinite() as u32;
            }
        }
        if t == 0 {
            self.run.fill(1);
        } else {
            let prev = c.row(s, t - 1);
            for k in 0..c.b {
                self.run[k] = if cur[k] == prev[k] { self.run[k] + 1 } else { 1 };
            }
        }
    }

    /// The whole window of column `k` holds one value.
    fn constant(&self, k: usize, d: usize) -> bool {
        self.run[k] as usize >= d
    }
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

/// `v - k`, or 0 for a non-finite value (so it leaves the sums unchanged).
fn shifted(v: f64, k: f64) -> f64 {
    if v.is_finite() {
        v - k
    } else {
        0.0
    }
}

fn first_finite(w: &[f64]) -> f64 {
    w.iter().copied().find(|v| v.is_finite()).unwrap_or(0.0)
}

impl Sums {
    /// From the window, shifted by its first finite value.
    fn rebuild(&mut self, w: &[f64]) {
        *self = Sums { k: first_finite(w), ..Sums::default() };
        for (i, &v) in w.iter().enumerate() {
            let u = shifted(v, self.k);
            self.s1 += u;
            self.s2 += u * u;
            self.sw += i as f64 * u;
            self.peak = self.peak.max(u * u);
        }
    }
}

/// [`Sums`] of every column of a block, one array per field: the per-row updates run
/// across columns without branches, in vector registers.
struct SumLanes {
    k: Vec<f64>,
    s1: Vec<f64>,
    s2: Vec<f64>,
    sw: Vec<f64>,
    peak: Vec<f64>,
}

impl SumLanes {
    fn new(b: usize) -> Self {
        SumLanes { k: vec![0.0; b], s1: vec![0.0; b], s2: vec![0.0; b], sw: vec![0.0; b], peak: vec![0.0; b] }
    }

    fn get(&self, j: usize) -> Sums {
        Sums { k: self.k[j], s1: self.s1[j], s2: self.s2[j], sw: self.sw[j], peak: self.peak[j] }
    }

    fn set(&mut self, j: usize, s: Sums) {
        (self.k[j], self.s1[j], self.s2[j], self.sw[j], self.peak[j]) = (s.k, s.s1, s.s2, s.sw, s.peak);
    }

    /// [`Sums::rebuild`] of every column over rows `r0..r0 + d` of input `src` (the same
    /// additions in the same order, taken row by row).
    fn rebuild(&mut self, c: &Cols, src: usize, r0: usize, d: usize) {
        let b = c.b;
        shifts(c, src, r0, d, &mut self.k);
        for v in [&mut self.s1, &mut self.s2, &mut self.sw, &mut self.peak] {
            v.fill(0.0);
        }
        let (k, s1, s2, sw, pk) = (&self.k[..b], &mut self.s1[..b], &mut self.s2[..b], &mut self.sw[..b], &mut self.peak[..b]);
        for i in 0..d {
            let (row, fi) = (c.row(src, r0 + i), i as f64);
            for j in 0..b {
                let u = shifted(row[j], k[j]);
                s1[j] += u;
                s2[j] += u * u;
                sw[j] += fi * u;
                pk[j] = pk[j].max(u * u);
            }
        }
    }

    /// Drop row `old` (the oldest), append row `cur`; `dm1` = d - 1.
    fn slide(&mut self, old: &[f64], cur: &[f64], dm1: f64) {
        let b = cur.len();
        let (old, k) = (&old[..b], &self.k[..b]);
        let (s1, s2, sw, pk) = (&mut self.s1[..b], &mut self.s2[..b], &mut self.sw[..b], &mut self.peak[..b]);
        for j in 0..b {
            let (uo, ui) = (shifted(old[j], k[j]), shifted(cur[j], k[j]));
            sw[j] += uo - s1[j] + dm1 * ui;
            s1[j] += ui - uo;
            s2[j] += ui * ui - uo * uo;
            pk[j] = pk[j].max(uo * uo).max(ui * ui);
        }
    }
}

/// First finite value of each column over rows `r0..r0 + d` of input `s` (0 if none),
/// into `k`.
fn shifts(c: &Cols, s: usize, r0: usize, d: usize, k: &mut [f64]) {
    k.fill(NAN);
    for r in r0..r0 + d {
        for (kk, &v) in k.iter_mut().zip(c.row(s, r)) {
            if kk.is_nan() && v.is_finite() {
                *kk = v;
            }
        }
    }
    for kk in k.iter_mut().filter(|kk| kk.is_nan()) {
        *kk = 0.0;
    }
}

/// Rounding in running sums of u² is ~ε·max u² per update, so once max u² outgrows the
/// window's own variance (the level drifted away from the shift, or a spike passed
/// through) the sums are rebuilt from the window.
const TRUST: f64 = 256.0;

fn stale(peak: f64, s1: f64, s2: f64, n: f64, inv_n: f64) -> bool {
    peak * n > TRUST * centered_ss(s1, s2, inv_n)
}

/// Windows starting at a multiple of this are rebuilt for every column of the block.
fn rebuild_every(d: usize) -> usize {
    d.max(16)
}

/// One-series driver over shifted sums. `f(sums, constant, x_t)` gives the value of a
/// full finite window; a window holding ±inf goes to `direct(window)` (the numpy formula).
fn roll_sums(c: &Cols, d: usize, f: impl Fn(&Sums, bool, f64) -> f64, direct: impl Fn(&[f64]) -> f64) {
    let b = c.b;
    let (n, inv_n, dm1) = (d as f64, 1.0 / d as f64, (d - 1) as f64);
    let mut tr = Track::new(b);
    let mut s = SumLanes::new(b);
    let mut flag = vec![false; b];
    let mut w = Vec::with_capacity(d);
    for t in 0..c.t {
        tr.push(c, 0, t, d);
        let out = c.out(t);
        if t + 1 < d {
            out.fill(NAN);
            continue;
        }
        let r0 = t + 1 - d;
        let cur = c.row(0, t);
        if r0 % rebuild_every(d) == 0 {
            s.rebuild(c, 0, r0, d);
        } else {
            s.slide(c.row(0, t - d), cur, dm1);
            // a window holding NaN is NaN anyway: checked once it is clean
            for j in 0..b {
                flag[j] = (tr.nan[j] == 0) & !tr.constant(j, d) & stale(s.peak[j], s.s1[j], s.s2[j], n, inv_n);
            }
            for j in 0..b {
                if flag[j] {
                    c.column(0, j, r0, d, &mut w);
                    let mut one = Sums::default();
                    one.rebuild(&w);
                    s.set(j, one);
                }
            }
        }
        for j in 0..b {
            out[j] = if tr.nan[j] > 0 {
                NAN
            } else if tr.inf[j] > 0 {
                c.column(0, j, r0, d, &mut w);
                direct(&w)
            } else {
                f(&s.get(j), tr.constant(j, d), cur[j])
            };
        }
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
        *self = Pair { kx: first_finite(x), ky: first_finite(y), ..Pair::default() };
        for i in 0..x.len() {
            let (u, v) = (shifted(x[i], self.kx), shifted(y[i], self.ky));
            self.sx += u;
            self.sy += v;
            self.sxx += u * u;
            self.syy += v * v;
            self.sxy += u * v;
            self.px = self.px.max(u * u);
            self.py = self.py.max(v * v);
        }
    }
}

/// [`Pair`] of every column of a block, one array per field (as [`SumLanes`]), in the
/// order kx, ky, sx, sy, sxx, syy, sxy, px, py.
struct PairLanes {
    f: [Vec<f64>; 9],
}

impl PairLanes {
    fn new(b: usize) -> Self {
        PairLanes { f: std::array::from_fn(|_| vec![0.0; b]) }
    }

    fn get(&self, j: usize) -> Pair {
        let f = &self.f;
        Pair {
            kx: f[0][j],
            ky: f[1][j],
            sx: f[2][j],
            sy: f[3][j],
            sxx: f[4][j],
            syy: f[5][j],
            sxy: f[6][j],
            px: f[7][j],
            py: f[8][j],
        }
    }

    fn set(&mut self, j: usize, p: Pair) {
        for (i, v) in [p.kx, p.ky, p.sx, p.sy, p.sxx, p.syy, p.sxy, p.px, p.py].into_iter().enumerate() {
            self.f[i][j] = v;
        }
    }

    /// [`Pair::rebuild`] of every column over rows `r0..r0 + d`.
    fn rebuild(&mut self, c: &Cols, r0: usize, d: usize) {
        let b = c.b;
        let [kx, ky, sx, sy, sxx, syy, sxy, px, py] = &mut self.f;
        shifts(c, 0, r0, d, kx);
        shifts(c, 1, r0, d, ky);
        for v in [&mut *sx, &mut *sy, &mut *sxx, &mut *syy, &mut *sxy, &mut *px, &mut *py] {
            v.fill(0.0);
        }
        let (kx, ky) = (&kx[..b], &ky[..b]);
        let (sx, sy, sxx, syy, sxy, px, py) =
            (&mut sx[..b], &mut sy[..b], &mut sxx[..b], &mut syy[..b], &mut sxy[..b], &mut px[..b], &mut py[..b]);
        for r in r0..r0 + d {
            let (xr, yr) = (c.row(0, r), c.row(1, r));
            for j in 0..b {
                let (u, v) = (shifted(xr[j], kx[j]), shifted(yr[j], ky[j]));
                sx[j] += u;
                sy[j] += v;
                sxx[j] += u * u;
                syy[j] += v * v;
                sxy[j] += u * v;
                px[j] = px[j].max(u * u);
                py[j] = py[j].max(v * v);
            }
        }
    }

    /// Drop rows `(xo, yo)`, append rows `(xi, yi)`.
    fn slide(&mut self, xo: &[f64], yo: &[f64], xi: &[f64], yi: &[f64]) {
        let b = xi.len();
        let [kx, ky, sx, sy, sxx, syy, sxy, px, py] = &mut self.f;
        let (xo, yo, yi, kx, ky) = (&xo[..b], &yo[..b], &yi[..b], &kx[..b], &ky[..b]);
        let (sx, sy, sxx, syy, sxy, px, py) =
            (&mut sx[..b], &mut sy[..b], &mut sxx[..b], &mut syy[..b], &mut sxy[..b], &mut px[..b], &mut py[..b]);
        for j in 0..b {
            let (uo, vo) = (shifted(xo[j], kx[j]), shifted(yo[j], ky[j]));
            let (ui, vi) = (shifted(xi[j], kx[j]), shifted(yi[j], ky[j]));
            sx[j] += ui - uo;
            sy[j] += vi - vo;
            sxx[j] += ui * ui - uo * uo;
            syy[j] += vi * vi - vo * vo;
            sxy[j] += ui * vi - uo * vo;
            px[j] = px[j].max(uo * uo).max(ui * ui);
            py[j] = py[j].max(vo * vo).max(vi * vi);
        }
    }
}

/// Two-series driver; `f(pair, x_constant, y_constant)`, `direct(wx, wy)` for ±inf windows.
fn roll_pair(c: &Cols, d: usize, f: impl Fn(&Pair, bool, bool) -> f64, direct: impl Fn(&[f64], &[f64]) -> f64) {
    let b = c.b;
    let (n, inv_n) = (d as f64, 1.0 / d as f64);
    let (mut tx, mut ty) = (Track::new(b), Track::new(b));
    let mut p = PairLanes::new(b);
    let mut flag = vec![false; b];
    let (mut wx, mut wy) = (Vec::with_capacity(d), Vec::with_capacity(d));
    for t in 0..c.t {
        tx.push(c, 0, t, d);
        ty.push(c, 1, t, d);
        let out = c.out(t);
        if t + 1 < d {
            out.fill(NAN);
            continue;
        }
        let r0 = t + 1 - d;
        if r0 % rebuild_every(d) == 0 {
            p.rebuild(c, r0, d);
        } else {
            p.slide(c.row(0, t - d), c.row(1, t - d), c.row(0, t), c.row(1, t));
            let [_, _, sx, sy, sxx, syy, _, px, py] = &p.f;
            for j in 0..b {
                let gx = !tx.constant(j, d) & stale(px[j], sx[j], sxx[j], n, inv_n);
                let gy = !ty.constant(j, d) & stale(py[j], sy[j], syy[j], n, inv_n);
                flag[j] = (tx.nan[j] + ty.nan[j] == 0) & (gx | gy);
            }
            for j in 0..b {
                if flag[j] {
                    c.column(0, j, r0, d, &mut wx);
                    c.column(1, j, r0, d, &mut wy);
                    let mut one = Pair::default();
                    one.rebuild(&wx, &wy);
                    p.set(j, one);
                }
            }
        }
        for j in 0..b {
            out[j] = if tx.nan[j] + ty.nan[j] > 0 {
                NAN
            } else if tx.inf[j] + ty.inf[j] > 0 {
                c.column(0, j, r0, d, &mut wx);
                c.column(1, j, r0, d, &mut wy);
                direct(&wx, &wy)
            } else {
                f(&p.get(j), tx.constant(j, d), ty.constant(j, d))
            };
        }
    }
}

/// Σ (x - x̄)² from shifted sums, never negative (`inv_n` = 1 / n: the per-step
/// arithmetic multiplies by reciprocals, divisions are several times slower).
fn centered_ss(s1: f64, s2: f64, inv_n: f64) -> f64 {
    (s2 - s1 * s1 * inv_n).max(0.0)
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

/// Rolling max (`want_max`) or min, as the value or as days since it (`want_arg`; ties
/// go to the oldest, like numpy's first-occurrence argmax). Van Herk / Gil-Werman: the
/// rows are cut into blocks of `d`, and a window is the tail of one block (its suffix
/// bests, computed when the next block starts) joined to the head of the next (running
/// bests). Windows holding a NaN are NaN, so the bests never need to skip one.
fn roll_extreme(c: &Cols, d: usize, want_max: bool, want_arg: bool) {
    let b = c.b;
    let better = |a: f64, z: f64| if want_max { a > z } else { a < z };
    let mut tr = Track::new(b);
    // suffix bests of the previous block (row q of it at q * b) and running bests
    let (mut sv, mut si) = (vec![NAN; d * b], vec![0usize; d * b]);
    let (mut pv, mut pi) = (vec![NAN; b], vec![0usize; b]);
    for t in 0..c.t {
        tr.push(c, 0, t, d);
        let cur = c.row(0, t);
        let start = t - t % d;
        if t == start {
            if start >= d {
                for q in (0..d).rev() {
                    let (r, row) = (start - d + q, c.row(0, start - d + q));
                    let (lo, hi) = sv.split_at_mut((q + 1) * b);
                    let (slo, shi) = si.split_at_mut((q + 1) * b);
                    let (v, i) = (&mut lo[q * b..], &mut slo[q * b..]);
                    for k in 0..b {
                        // the older value wins ties
                        if q + 1 == d || !better(hi[k], row[k]) {
                            (v[k], i[k]) = (row[k], r);
                        } else {
                            (v[k], i[k]) = (hi[k], shi[k]);
                        }
                    }
                }
            }
            pv.copy_from_slice(cur);
            pi.fill(t);
        } else {
            for k in 0..b {
                if better(cur[k], pv[k]) {
                    (pv[k], pi[k]) = (cur[k], t);
                }
            }
        }
        let out = c.out(t);
        if t + 1 < d {
            out.fill(NAN);
            continue;
        }
        let i0 = t + 1 - d;
        let q = (i0 + d - start) % d; // row of the window start in the previous block
        for k in 0..b {
            let (v, i) = if i0 == start || better(pv[k], sv[q * b + k]) {
                (pv[k], pi[k])
            } else {
                (sv[q * b + k], si[q * b + k])
            };
            out[k] = if tr.nan[k] > 0 {
                NAN
            } else if want_arg {
                (t - i) as f64
            } else {
                v
            };
        }
    }
}

/// Per-window kernel `f(window)` (O(d) each) on every full NaN-free window. The block
/// is gathered column by column first: a window is then a contiguous slice that stays
/// in L1 as it slides, where reading it across rows would stream `d` rows per step.
fn per_window(c: &Cols, d: usize, f: impl Fn(&[f64]) -> f64) {
    let (b, tn) = (c.b, c.t);
    let mut cols = vec![0.0f64; b * tn];
    for r in 0..tn {
        for (k, &v) in c.row(0, r).iter().enumerate() {
            cols[k * tn + r] = v;
        }
    }
    let mut res = vec![NAN; b * tn];
    for k in 0..b {
        let (x, o) = (&cols[k * tn..(k + 1) * tn], &mut res[k * tn..(k + 1) * tn]);
        let mut nan = 0usize;
        for t in 0..tn {
            nan += x[t].is_nan() as usize;
            if t >= d {
                nan -= x[t - d].is_nan() as usize;
            }
            if t + 1 >= d && nan == 0 {
                o[t] = f(&x[t + 1 - d..=t]);
            }
        }
    }
    for r in 0..tn {
        for (k, v) in c.out(r).iter_mut().enumerate() {
            *v = res[k * tn + r];
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

/// One-series kernel `op` on a block of columns.
pub fn unary(op: &str, c: &Cols, d: usize) {
    let n = d as f64;
    let tbar = (n - 1.0) / 2.0;
    let stt: f64 = (0..d).map(|i| (i as f64 - tbar).powi(2)).sum();
    let wsum = n * (n + 1.0) / 2.0;
    let (inv_n, inv_n1, inv_stt, inv_w) = (1.0 / n, 1.0 / (n - 1.0), 1.0 / stt, 1.0 / wsum);
    match op {
        "sum" => roll_sums(c, d, |s, k, v| if k { v * n } else { s.k * n + s.s1 }, |w| w.iter().sum()),
        "mean" => roll_sums(c, d, |s, k, v| if k { v } else { s.k + s.s1 * inv_n }, mean),
        "var" => roll_sums(c, d, |s, k, _| if k { 0.0 } else { centered_ss(s.s1, s.s2, inv_n) * inv_n1 }, |w| {
            comoments(w, w).1 / (n - 1.0)
        }),
        "std" => roll_sums(c, d, |s, k, _| if k { 0.0 } else { (centered_ss(s.s1, s.s2, inv_n) * inv_n1).sqrt() }, |w| {
            (comoments(w, w).1 / (n - 1.0)).sqrt()
        }),
        // Σ (i + 1) x_i / (d (d + 1) / 2): newest weighs most
        "decay_linear" => roll_sums(c, d, |s, k, v| if k { v } else { s.k + (s.sw + s.s1) * inv_w }, |w| {
            w.iter().enumerate().map(|(i, v)| v * (i + 1) as f64).sum::<f64>() / wsum
        }),
        // OLS of x on t = 0..d-1; Σ u (t - t̄) = Σ i u - t̄ Σ u does not depend on the shift
        "slope" => roll_sums(c, d, |s, k, _| if k { 0.0 } else { (s.sw - tbar * s.s1) * inv_stt }, |w| {
            sum_tc(w, tbar) / stt
        }),
        // x_t - (intercept + slope (d - 1)) = (x_t - x̄) - slope t̄
        "resi" => roll_sums(c, d, |s, k, v| {
            if k { 0.0 } else { (v - s.k) - s.s1 * inv_n - (s.sw - tbar * s.s1) * inv_stt * tbar }
        }, |w| {
            let slope = sum_tc(w, tbar) / stt;
            let intercept = mean(w) - slope * tbar;
            w[w.len() - 1] - (intercept + slope * (n - 1.0))
        }),
        "rsquare" => roll_sums(c, d, |s, k, _| {
            let sxx = if k { 0.0 } else { centered_ss(s.s1, s.s2, inv_n) };
            let sxt = s.sw - tbar * s.s1;
            if sxx == 0.0 { NAN } else { (sxt * sxt / (stt * sxx)).min(1.0) }
        }, |w| {
            let (sxt, sxx) = (sum_tc(w, tbar), comoments(w, w).1);
            if sxx == 0.0 { NAN } else { sxt * sxt / (stt * sxx) }
        }),
        "min" => roll_extreme(c, d, false, false),
        "max" => roll_extreme(c, d, true, false),
        "argmin" => roll_extreme(c, d, false, true),
        "argmax" => roll_extreme(c, d, true, true),
        "rank" => per_window(c, d, |w| {
            let today = w[w.len() - 1];
            let le = w.iter().filter(|&&v| v <= today).count();
            (le - 1) as f64 / (n - 1.0)
        }),
        "count" => per_window(c, d, |w| w.iter().filter(|&&v| v != 0.0).count() as f64),
        "skew" => per_window(c, d, |w| {
            let (m3, m2) = central(w, 3);
            if m2 == 0.0 {
                NAN
            } else {
                m3 / m2.powf(1.5)
            }
        }),
        "kurt" => per_window(c, d, |w| {
            let (m4, m2) = central(w, 4);
            if m2 == 0.0 {
                NAN
            } else {
                m4 / (m2 * m2) - 3.0
            }
        }),
        "mad" => per_window(c, d, |w| {
            let m = w.iter().sum::<f64>() / n;
            w.iter().map(|v| (v - m).abs()).sum::<f64>() / n
        }),
        _ => unreachable!("checked against UNARY"),
    }
}

pub const UNARY: &[&str] = &[
    "sum", "mean", "var", "std", "decay_linear", "slope", "resi", "rsquare", "min", "max", "argmin",
    "argmax", "rank", "count", "skew", "kurt", "mad",
];
pub const BINARY: &[&str] = &["cov", "corr"];

/// Two-series kernel `op` on a block of column pairs.
pub fn binary(op: &str, c: &Cols, d: usize) {
    let n = d as f64;
    let (inv_n, inv_n1) = (1.0 / n, 1.0 / (n - 1.0));
    match op {
        "cov" => roll_pair(c, d, |p, cx, cy| {
            if cx || cy {
                0.0
            } else {
                (p.sxy - p.sx * p.sy * inv_n) * inv_n1
            }
        }, |wx, wy| comoments(wx, wy).0 / (n - 1.0)),
        "corr" => roll_pair(c, d, |p, cx, cy| {
            if cx || cy {
                return NAN;
            }
            let vx = p.sxx - p.sx * p.sx * inv_n;
            let vy = p.syy - p.sy * p.sy * inv_n;
            let denom = vx.max(0.0).sqrt() * vy.max(0.0).sqrt();
            if denom == 0.0 {
                return NAN;
            }
            ((p.sxy - p.sx * p.sy * inv_n) / denom).clamp(-1.0, 1.0)
        }, |wx, wy| {
            let (c, vx, vy) = comoments(wx, wy);
            let denom = vx.sqrt() * vy.sqrt();
            if denom == 0.0 { NAN } else { c / denom }
        }),
        _ => unreachable!("checked against BINARY"),
    }
}
