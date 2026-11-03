//! Native kernels for Assay: cross-sectional (this file) and rolling time-series
//! (`ts.rs`).
//!
//! Every cross-sectional kernel works on a row-major `(T, N)` float64 matrix, one date per row, and
//! treats rows independently, so rows are spread over a rayon pool with the GIL
//! released. The arithmetic follows the Python/numba references in
//! `assay.engine.operators.cross_sectional` and `assay.evaluator` step by step (same
//! masks, same tie rule, same summation order), so results agree with them; the
//! tests in `tests/engine/test_native_kernels.py` check that.

use numpy::{PyArray1, PyArray2, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

mod ts;

fn rows<'a>(a: &'a PyReadonlyArray2<'_, f64>) -> PyResult<(&'a [f64], usize, usize)> {
    let shape = a.shape();
    let (t, n) = (shape[0], shape[1]);
    let s = a
        .as_slice()
        .map_err(|_| PyValueError::new_err("expected a C-contiguous float64 matrix"))?;
    Ok((s, t, n))
}

fn same_shape(a: &PyReadonlyArray2<'_, f64>, b: &PyReadonlyArray2<'_, f64>) -> PyResult<()> {
    if a.shape() != b.shape() {
        return Err(PyValueError::new_err("both matrices must share the same (T, N) shape"));
    }
    Ok(())
}

/// Sort key in the order of the values (-0.0 counts as 0.0); NaN must be left out.
fn key(v: f64) -> u64 {
    let b = (v + 0.0).to_bits();
    if b >> 63 == 1 {
        !b
    } else {
        b | 1 << 63
    }
}

/// Keys with their column indices, and the scratch to sort them.
#[derive(Default)]
struct Sorter {
    keys: Vec<u64>,
    idx: Vec<u32>,
    packed: Vec<u128>,
}

impl Sorter {
    /// The entries of `row` that pass `keep`, in column order.
    fn load(&mut self, row: &[f64], keep: impl Fn(f64) -> bool) {
        self.keys.clear();
        self.idx.clear();
        for (j, &v) in row.iter().enumerate() {
            if keep(v) {
                self.keys.push(key(v));
                self.idx.push(j as u32);
            }
        }
    }

    /// Sorts by key; equal keys keep their column order (a stable sort): key and
    /// column packed into one u128, so the comparison sort reads no other memory.
    fn sort(&mut self) {
        self.packed.clear();
        self.packed.extend(self.keys.iter().zip(&self.idx).map(|(&k, &j)| (k as u128) << 32 | j as u128));
        self.packed.sort_unstable();
        for (i, &x) in self.packed.iter().enumerate() {
            (self.keys[i], self.idx[i]) = ((x >> 32) as u64, x as u32);
        }
    }
}

/// Average ranks (0-based, ties averaged) of `vals`, written to `out[k]` for the
/// k-th value. `order` is scratch space.
fn avg_ranks(vals: &[f64], order: &mut Vec<usize>, out: &mut [f64]) {
    let m = vals.len();
    order.clear();
    order.extend(0..m);
    order.sort_by(|&a, &b| vals[a].partial_cmp(&vals[b]).unwrap_or(std::cmp::Ordering::Equal));
    let mut i = 0;
    while i < m {
        let mut j = i + 1;
        while j < m && vals[order[j]] == vals[order[i]] {
            j += 1;
        }
        let avg = ((i + j - 1) as f64) / 2.0;
        for p in i..j {
            out[order[p]] = avg;
        }
        i = j;
    }
}

/// Two-pass Pearson over the paired values, in order (as `_pearson_pair`).
fn pearson(x: &[f64], y: &[f64]) -> f64 {
    let cnt = x.len();
    if cnt < 2 {
        return f64::NAN;
    }
    let (mut sx, mut sy) = (0.0f64, 0.0f64);
    for i in 0..cnt {
        sx += x[i];
        sy += y[i];
    }
    let (mx, my) = (sx / cnt as f64, sy / cnt as f64);
    let (mut cov, mut vx, mut vy) = (0.0f64, 0.0f64, 0.0f64);
    for i in 0..cnt {
        let dx = x[i] - mx;
        let dy = y[i] - my;
        cov += dx * dy;
        vx += dx * dx;
        vy += dy * dy;
    }
    if vx <= 0.0 || vy <= 0.0 {
        return f64::NAN;
    }
    cov / (vx * vy).sqrt()
}

/// Ranks of `s` (row-major, `n` columns) as [`cs_rank`] does, and with `order` / `count`
/// also each row's sorted columns (written into `order`, as [`row_orders`] lays out).
fn rank_rows(s: &[f64], n: usize, out: &mut [f64], order: Option<(&mut [u32], &mut [u32])>) {
    let rank = |so: &mut Sorter, row: usize, o: &mut [f64]| {
        so.load(&s[row * n..row * n + n], |v| !v.is_nan());
        so.sort();
        rank_sorted(so, o);
    };
    match order {
        None => out.par_chunks_mut(n.max(1)).enumerate().for_each_init(Sorter::default, |so, (row, o)| rank(so, row, o)),
        Some((ord, cnt)) => out
            .par_chunks_mut(n.max(1))
            .zip(ord.par_chunks_mut(n.max(1)))
            .zip(cnt.par_iter_mut())
            .enumerate()
            .for_each_init(Sorter::default, |so, (row, ((o, od), c))| {
                rank(so, row, o);
                od[..so.idx.len()].copy_from_slice(&so.idx);
                *c = so.idx.len() as u32;
            }),
    }
}

/// Writes the average ranks / (m - 1) of a sorted row into `o` (0.5 for a lone value).
fn rank_sorted(so: &Sorter, o: &mut [f64]) {
    let m = so.keys.len();
    if m == 1 {
        o[so.idx[0] as usize] = 0.5;
    }
    if m < 2 {
        return;
    }
    let d = (m - 1) as f64;
    let mut i = 0;
    while i < m {
        let mut j = i + 1;
        while j < m && so.keys[j] == so.keys[i] {
            j += 1;
        }
        let r = ((i + j - 1) as f64) / 2.0 / d;
        for &c in &so.idx[i..j] {
            o[c as usize] = r;
        }
        i = j;
    }
}

/// `cs_rank`: per row, average rank / (n - 1) over the non-NaN entries; 0.5 for a
/// lone value, NaN where the input is NaN.
#[pyfunction]
fn cs_rank<'py>(py: Python<'py>, x: PyReadonlyArray2<'py, f64>) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (s, t, n) = rows(&x)?;
    let mut out = vec![f64::NAN; t * n];
    py.allow_threads(|| rank_rows(s, n, &mut out, None));
    Ok(PyArray1::from_vec_bound(py, out).reshape([t, n])?)
}

/// [`cs_rank`] and the [`row_orders`] of its result, `(ranks, order, count)`, from the one
/// sort: the ranks follow the values, so the result sorts in the input's order (ties by
/// column), and a factor that ends in `cs_rank` needs no second sort to be scored.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn cs_rank_ordered<'py>(
    py: Python<'py>,
    x: PyReadonlyArray2<'py, f64>,
) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<u32>>, Bound<'py, PyArray1<u32>>)> {
    let (s, t, n) = rows(&x)?;
    let mut out = vec![f64::NAN; t * n];
    let mut order = vec![0u32; t * n];
    let mut count = vec![0u32; t];
    py.allow_threads(|| rank_rows(s, n, &mut out, Some((&mut order, &mut count))));
    Ok((
        PyArray1::from_vec_bound(py, out).reshape([t, n])?,
        PyArray1::from_vec_bound(py, order).reshape([t, n])?,
        PyArray1::from_vec_bound(py, count),
    ))
}

/// The [`row_orders`] `(order, count)` of a matrix restricted to the entries where `keep`
/// is true (as `np.where(keep, x, nan)` would sort): each row's order filtered in place.
#[pyfunction]
fn orders_where<'py>(
    py: Python<'py>,
    order: PyReadonlyArray2<'py, u32>,
    count: PyReadonlyArray1<'py, u32>,
    keep: PyReadonlyArray2<'py, bool>,
) -> PyResult<(Bound<'py, PyArray2<u32>>, Bound<'py, PyArray1<u32>>)> {
    let (t, n) = (keep.shape()[0], keep.shape()[1]);
    let (o, c) = orders(&order, &count, t, n)?;
    let k = keep.as_slice().map_err(|_| PyValueError::new_err("keep must be a C-contiguous bool matrix"))?;
    let mut out = vec![0u32; t * n];
    let mut cnt = vec![0u32; t];
    py.allow_threads(|| {
        out.par_chunks_mut(n.max(1)).zip(cnt.par_iter_mut()).enumerate().for_each(|(row, (od, m))| {
            let kr = &k[row * n..row * n + n];
            let mut p = 0;
            for &j in &o[row * n..row * n + c[row] as usize] {
                if kr[j as usize] {
                    od[p] = j;
                    p += 1;
                }
            }
            *m = p as u32;
        });
    });
    Ok((PyArray1::from_vec_bound(py, out).reshape([t, n])?, PyArray1::from_vec_bound(py, cnt)))
}

/// Per-row Spearman RankIC between `x` and `y` over the entries finite in both.
/// `lag` > 0 correlates row t of `y` with row t - lag of `x` (rank autocorrelation);
/// the first `lag` rows are NaN.
#[pyfunction]
#[pyo3(signature = (x, y, lag=0))]
fn rank_ic_series<'py>(
    py: Python<'py>,
    x: PyReadonlyArray2<'py, f64>,
    y: PyReadonlyArray2<'py, f64>,
    lag: usize,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    same_shape(&x, &y)?;
    let (xs, t, n) = rows(&x)?;
    let (ys, _, _) = rows(&y)?;
    let mut out = vec![f64::NAN; t];
    py.allow_threads(|| {
        out.par_iter_mut().enumerate().for_each_init(
            || (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new()),
            |(a, b, order, ra, rb), (row, o)| {
                if row < lag {
                    return;
                }
                let xr = &xs[(row - lag) * n..(row - lag) * n + n];
                let yr = &ys[row * n..row * n + n];
                a.clear();
                b.clear();
                for j in 0..n {
                    if xr[j].is_finite() && yr[j].is_finite() {
                        a.push(xr[j]);
                        b.push(yr[j]);
                    }
                }
                let m = a.len();
                if m < 2 {
                    return;
                }
                ra.resize(m, 0.0);
                rb.resize(m, 0.0);
                avg_ranks(a, order, ra);
                avg_ranks(b, order, rb);
                *o = pearson(ra, rb);
            },
        );
    });
    Ok(PyArray1::from_vec_bound(py, out))
}

/// Per row, the column indices of the finite entries sorted by value (stable), and
/// how many there are: `(order (T, N) uint32, count (T,) uint32)`. Sorting once lets
/// [`rank_ic_ordered`] rank a factor against several horizons (and its own lag) by
/// a linear scan instead of a sort per pair.
#[pyfunction]
fn row_orders<'py>(
    py: Python<'py>,
    x: PyReadonlyArray2<'py, f64>,
) -> PyResult<(Bound<'py, PyArray2<u32>>, Bound<'py, PyArray1<u32>>)> {
    let (s, t, n) = rows(&x)?;
    if n > u32::MAX as usize {
        return Err(PyValueError::new_err("too many columns"));
    }
    let mut order = vec![0u32; t * n];
    let mut count = vec![0u32; t];
    py.allow_threads(|| {
        order.par_chunks_mut(n.max(1)).zip(count.par_iter_mut()).enumerate().for_each_init(
            Sorter::default,
            |so, (row, (o, c))| {
                so.load(&s[row * n..row * n + n], f64::is_finite);
                so.sort();
                o[..so.idx.len()].copy_from_slice(&so.idx);
                *c = so.idx.len() as u32;
            },
        );
    });
    Ok((PyArray1::from_vec_bound(py, order).reshape([t, n])?, PyArray1::from_vec_bound(py, count)))
}

/// Bitset row: is column `j` in it.
fn has(bits: &[u64], j: u32) -> bool {
    (bits[(j >> 6) as usize] >> (j & 63)) & 1 == 1
}

/// Average ranks (as [`avg_ranks`]) of the entries of `vals` in the column set `keep`,
/// read off the precomputed value `order`, written to `out[j]`; returns how many there
/// are and Σ rank².
fn joint_ranks(vals: &[f64], order: &[u32], keep: &[u64], out: &mut [f32]) -> (usize, f64) {
    let m = order.len();
    let (mut pos, mut i, mut ss) = (0usize, 0usize, 0.0f64);
    while i < m {
        let v = vals[order[i] as usize];
        let (mut end, mut cnt) = (i, 0usize);
        while end < m && vals[order[end] as usize] == v {
            cnt += has(keep, order[end]) as usize;
            end += 1;
        }
        if cnt > 0 {
            let avg = ((pos + pos + cnt - 1) as f64) / 2.0;
            for &j in &order[i..end] {
                if has(keep, j) {
                    out[j as usize] = avg as f32; // a multiple of 1/2 below 2^23: exact
                }
            }
            ss += cnt as f64 * avg * avg;
            pos += cnt;
        }
        i = end;
    }
    (pos, ss)
}

/// Per-row ranks of one matrix among a column set, kept for the next call. A forward
/// return is ranked against every factor among the same names (the universe members
/// that day), and a factor against each horizon, so most rows find their ranks here
/// and RankIC reduces to one dot product. A row is reused only when its column set is
/// the same, bit for bit.
#[pyclass(module = "assay_kernels")]
struct RankCache {
    t: usize,
    n: usize,
    words: usize,
    mask: Vec<u64>,
    ranks: Vec<f32>,
    count: Vec<u32>,
    ss: Vec<f64>,
    valid: Vec<bool>,
    hits: usize,
    misses: usize,
}

#[pymethods]
impl RankCache {
    #[new]
    fn new(t: usize, n: usize) -> Self {
        let words = n.div_ceil(64);
        RankCache {
            t,
            n,
            words,
            mask: vec![0; t * words],
            ranks: vec![0.0; t * n],
            count: vec![0; t],
            ss: vec![0.0; t],
            valid: vec![false; t],
            hits: 0,
            misses: 0,
        }
    }

    /// `(rows reused, rows ranked)` over the cache's life.
    fn stats(&self) -> (usize, usize) {
        (self.hits, self.misses)
    }
}

impl RankCache {
    /// Row `r`'s ranks and Σ rank², when it was ranked on exactly the set `keep`.
    fn lookup(&self, r: usize, keep: &[u64]) -> Option<(&[f32], f64)> {
        let w = self.words;
        (self.valid[r] && self.mask[r * w..(r + 1) * w] == *keep).then(|| (&self.ranks[r * self.n..(r + 1) * self.n], self.ss[r]))
    }
}

/// Raw row access to a [`RankCache`] for tasks that each own distinct rows.
#[derive(Clone, Copy)]
struct Slots {
    mask: *mut u64,
    ranks: *mut f32,
    count: *mut u32,
    ss: *mut f64,
    valid: *mut bool,
    n: usize,
    words: usize,
}
unsafe impl Send for Slots {}
unsafe impl Sync for Slots {}

impl Slots {
    fn of(c: &mut RankCache) -> Self {
        Slots {
            mask: c.mask.as_mut_ptr(),
            ranks: c.ranks.as_mut_ptr(),
            count: c.count.as_mut_ptr(),
            ss: c.ss.as_mut_ptr(),
            valid: c.valid.as_mut_ptr(),
            n: c.n,
            words: c.words,
        }
    }

    /// Ranks of `vals` among `keep` on row `r`: from the cache when the row was ranked
    /// on the same set, else computed and stored. Returns (ranks, count, Σ rank², hit).
    ///
    /// SAFETY: `r` is in range and no other task uses row `r` meanwhile.
    unsafe fn ranks<'a>(&self, r: usize, vals: &[f64], order: &[u32], keep: &[u64]) -> (&'a [f32], usize, f64, bool) {
        let mask = std::slice::from_raw_parts_mut(self.mask.add(r * self.words), self.words);
        let ranks = std::slice::from_raw_parts_mut(self.ranks.add(r * self.n), self.n);
        let (count, ss, valid) = (&mut *self.count.add(r), &mut *self.ss.add(r), &mut *self.valid.add(r));
        if *valid && mask == keep {
            return (ranks, *count as usize, *ss, true);
        }
        let (m, s) = joint_ranks(vals, order, keep, ranks);
        mask.copy_from_slice(keep);
        (*count, *ss, *valid) = (m as u32, s, true);
        (ranks, m, s, false)
    }
}

/// Checks the shapes of `(order, count)` from [`row_orders`] against a `(t, n)` matrix.
/// The entries are not scanned (that would cost as much as using them): an index out of
/// range stops the kernel at its bounds check and raises in Python.
fn orders<'a>(
    o: &'a PyReadonlyArray2<'_, u32>,
    c: &'a PyReadonlyArray1<'_, u32>,
    t: usize,
    n: usize,
) -> PyResult<(&'a [u32], &'a [u32])> {
    let bad = || PyValueError::new_err("orders must come from row_orders of the same matrices");
    if o.shape() != [t, n] || c.len() != t {
        return Err(bad());
    }
    let (o, c) = (o.as_slice().map_err(|_| bad())?, c.as_slice().map_err(|_| bad())?);
    if c.iter().any(|&k| k as usize > n) {
        return Err(bad());
    }
    Ok((o, c))
}

/// [`rank_ic_series`] with the per-row orders of `x` and `y` from [`row_orders`]: no
/// sorting, and with `xcache` / `ycache` ([`RankCache`] of `x` / `y`) the ranks of rows
/// already seen on the same names are reused. Ranks are multiples of 1/2, so Σ rx·ry,
/// Σ rx² and Σ ry² are exact in f64 (for rows of up to ~100k names), as are the centred
/// sums Pearson's two passes would form: the values match bit for bit. One cache passed
/// as both (a factor against its own lag) is only read: a row is then the x side of
/// one task and the y side of another, so rows it does not hold are ranked aside.
#[pyfunction]
#[pyo3(signature = (x, xo, xc, y, yo, yc, lag=0, xcache=None, ycache=None))]
#[allow(clippy::too_many_arguments)]
fn rank_ic_ordered<'py>(
    py: Python<'py>,
    x: PyReadonlyArray2<'py, f64>,
    xo: PyReadonlyArray2<'py, u32>,
    xc: PyReadonlyArray1<'py, u32>,
    y: PyReadonlyArray2<'py, f64>,
    yo: PyReadonlyArray2<'py, u32>,
    yc: PyReadonlyArray1<'py, u32>,
    lag: usize,
    xcache: Option<Bound<'py, RankCache>>,
    ycache: Option<Bound<'py, RankCache>>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    same_shape(&x, &y)?;
    let (xs, t, n) = rows(&x)?;
    let (ys, _, _) = rows(&y)?;
    let (xo, xc) = orders(&xo, &xc, t, n)?;
    let (yo, yc) = orders(&yo, &yc, t, n)?;
    // a cache another thread is using right now is skipped (same values, ranked afresh)
    let shared = matches!((&xcache, &ycache), (Some(a), Some(b)) if a.is(b));
    let sg = if shared { xcache.as_ref().and_then(|c| c.try_borrow().ok()) } else { None };
    let mut xg = if shared { None } else { xcache.as_ref().and_then(|c| c.try_borrow_mut().ok()) };
    let mut yg = if shared { None } else { ycache.as_ref().and_then(|c| c.try_borrow_mut().ok()) };
    for (gt, gn) in xg.iter().chain(yg.iter()).map(|g| (g.t, g.n)).chain(sg.iter().map(|g| (g.t, g.n))) {
        if (gt, gn) != (t, n) {
            return Err(PyValueError::new_err("RankCache shape differs from the matrices"));
        }
    }
    let ro: Option<&RankCache> = sg.as_deref();
    let (xslots, yslots) = (xg.as_deref_mut().map(Slots::of), yg.as_deref_mut().map(Slots::of));
    let words = n.div_ceil(64);
    let mut out = vec![f64::NAN; t];
    let mut hit = vec![[0u8; 2]; t];
    py.allow_threads(|| {
        out.par_iter_mut().zip(hit.par_iter_mut()).enumerate().for_each_init(
            || (vec![0u64; words], vec![0.0f32; n], vec![0.0f32; n]),
            |(keep, rx0, ry0), (row, (o, h))| {
                if row < lag {
                    return;
                }
                let xrow = row - lag;
                let (xr, yr) = (&xs[xrow * n..xrow * n + n], &ys[row * n..row * n + n]);
                keep.fill(0);
                let mut m = 0;
                for j in 0..n {
                    let k = (xr[j].is_finite() && yr[j].is_finite()) as u64;
                    keep[j >> 6] |= k << (j & 63);
                    m += k as usize;
                }
                if m < 2 {
                    return;
                }
                let (xord, yord) = (&xo[xrow * n..xrow * n + xc[xrow] as usize], &yo[row * n..row * n + yc[row] as usize]);
                // SAFETY: row `row` (x: `row - lag`) is handled by this task only
                let (ry, syy, hy) = match (yslots, ro.and_then(|c| c.lookup(row, keep))) {
                    (Some(sl), _) => {
                        let (r, _, s, h) = unsafe { sl.ranks(row, yr, yord, keep) };
                        (r, s, h)
                    }
                    (None, Some((r, s))) => (r, s, true),
                    (None, None) => {
                        let s = joint_ranks(yr, yord, keep, ry0).1;
                        (&ry0[..], s, false)
                    }
                };
                let (rx, sxx, hx) = match (xslots, ro.and_then(|c| c.lookup(xrow, keep))) {
                    (Some(sl), _) => {
                        let (r, _, s, h) = unsafe { sl.ranks(xrow, xr, xord, keep) };
                        (r, s, h)
                    }
                    (None, Some((r, s))) => (r, s, true),
                    (None, None) => {
                        let s = joint_ranks(xr, xord, keep, rx0).1;
                        (&rx0[..], s, false)
                    }
                };
                *h = [hx as u8, hy as u8];
                let mut sxy = 0.0f64;
                for (w, &bits) in keep.iter().enumerate() {
                    let mut b = bits;
                    while b != 0 {
                        let j = w * 64 + b.trailing_zeros() as usize;
                        sxy += rx[j] as f64 * ry[j] as f64;
                        b &= b - 1;
                    }
                }
                let mean = (m - 1) as f64 / 2.0;
                let mm = m as f64 * mean * mean;
                let (cov, vx, vy) = (sxy - mm, sxx - mm, syy - mm);
                if vx > 0.0 && vy > 0.0 {
                    *o = cov / (vx * vy).sqrt();
                }
            },
        );
    });
    let ranked = t.saturating_sub(lag);
    let reused = |side: usize| hit.iter().map(|h| h[side] as usize).sum::<usize>();
    for (g, side) in [(&mut xg, 0), (&mut yg, 1)] {
        if let Some(g) = g.as_deref_mut() {
            g.hits += reused(side);
            g.misses += ranked - reused(side);
        }
    }
    if sg.is_some() {
        drop(sg);
        if let Some(mut g) = xcache.as_ref().and_then(|c| c.try_borrow_mut().ok()) {
            g.hits += reused(0) + reused(1);
            g.misses += 2 * ranked - reused(0) - reused(1);
        }
    }
    Ok(PyArray1::from_vec_bound(py, out))
}

/// Per-row Pearson IC over the entries finite in both rows.
#[pyfunction]
fn ic_series<'py>(
    py: Python<'py>,
    x: PyReadonlyArray2<'py, f64>,
    y: PyReadonlyArray2<'py, f64>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    same_shape(&x, &y)?;
    let (xs, t, n) = rows(&x)?;
    let (ys, _, _) = rows(&y)?;
    let mut out = vec![f64::NAN; t];
    py.allow_threads(|| {
        out.par_iter_mut().enumerate().for_each_init(
            || (Vec::new(), Vec::new()),
            |(a, b), (row, o)| {
                let xr = &xs[row * n..row * n + n];
                let yr = &ys[row * n..row * n + n];
                a.clear();
                b.clear();
                for j in 0..n {
                    if xr[j].is_finite() && yr[j].is_finite() {
                        a.push(xr[j]);
                        b.push(yr[j]);
                    }
                }
                *o = pearson(a, b);
            },
        );
    });
    Ok(PyArray1::from_vec_bound(py, out))
}

/// Quantile-bucket sums and counts per row (as `group_returns`): the entries finite
/// in both rows, sorted by factor (stable), cut into `g` equal-count buckets by
/// position; rows with fewer than `g` entries stay zero.
#[pyfunction]
fn group_sums<'py>(
    py: Python<'py>,
    factor: PyReadonlyArray2<'py, f64>,
    fwd: PyReadonlyArray2<'py, f64>,
    g: usize,
) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>)> {
    same_shape(&factor, &fwd)?;
    if g < 2 {
        return Err(PyValueError::new_err("n_groups must be >= 2"));
    }
    let (fs, t, n) = rows(&factor)?;
    let (rs, _, _) = rows(&fwd)?;
    let mut sums = vec![0.0f64; t * g];
    let mut counts = vec![0.0f64; t * g];
    py.allow_threads(|| {
        sums.par_chunks_mut(g)
            .zip(counts.par_chunks_mut(g))
            .enumerate()
            .for_each_init(
                || (Vec::new(), Vec::new(), Vec::new()),
                |(fv, rv, order), (row, (s, c))| {
                    let fr = &fs[row * n..row * n + n];
                    let rr = &rs[row * n..row * n + n];
                    fv.clear();
                    rv.clear();
                    for j in 0..n {
                        if fr[j].is_finite() && rr[j].is_finite() {
                            fv.push(fr[j]);
                            rv.push(rr[j]);
                        }
                    }
                    let m = fv.len();
                    if m < g {
                        return;
                    }
                    order.clear();
                    order.extend(0..m);
                    // stable, like mergesort: ties keep their column order
                    order.sort_by(|&a, &b| fv[a].partial_cmp(&fv[b]).unwrap_or(std::cmp::Ordering::Equal));
                    for (pos, &k) in order.iter().enumerate() {
                        let b = pos * g / m;
                        s[b] += rv[k];
                        c[b] += 1.0;
                    }
                },
            );
    });
    Ok((
        PyArray1::from_vec_bound(py, sums).reshape([t, g])?,
        PyArray1::from_vec_bound(py, counts).reshape([t, g])?,
    ))
}

/// [`group_sums`] with the factor's [`row_orders`]: the order is already stable, so
/// each row is one scan (the same buckets and the same sums).
#[pyfunction]
fn group_sums_ordered<'py>(
    py: Python<'py>,
    factor: PyReadonlyArray2<'py, f64>,
    fo: PyReadonlyArray2<'py, u32>,
    fc: PyReadonlyArray1<'py, u32>,
    fwd: PyReadonlyArray2<'py, f64>,
    g: usize,
) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>)> {
    same_shape(&factor, &fwd)?;
    if g < 2 {
        return Err(PyValueError::new_err("n_groups must be >= 2"));
    }
    let (_, t, n) = rows(&factor)?;
    let (rs, _, _) = rows(&fwd)?;
    let (fo, fc) = orders(&fo, &fc, t, n)?;
    let mut sums = vec![0.0f64; t * g];
    let mut counts = vec![0.0f64; t * g];
    py.allow_threads(|| {
        sums.par_chunks_mut(g).zip(counts.par_chunks_mut(g)).enumerate().for_each(|(row, (s, c))| {
            let rr = &rs[row * n..row * n + n];
            let o = &fo[row * n..row * n + fc[row] as usize];
            let m = o.iter().filter(|&&j| rr[j as usize].is_finite()).count();
            if m < g {
                return;
            }
            let mut pos = 0;
            for &j in o {
                let r = rr[j as usize];
                if r.is_finite() {
                    let b = pos * g / m;
                    s[b] += r;
                    c[b] += 1.0;
                    pos += 1;
                }
            }
        });
    });
    Ok((
        PyArray1::from_vec_bound(py, sums).reshape([t, g])?,
        PyArray1::from_vec_bound(py, counts).reshape([t, g])?,
    ))
}

/// Rolling time-series kernel `op` (one of `ts.rs`'s `UNARY`) over windows of `d` rows,
/// swept row by row over blocks of columns.
#[pyfunction]
fn ts_rolling<'py>(
    py: Python<'py>,
    op: &str,
    x: PyReadonlyArray2<'py, f64>,
    d: usize,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    if !ts::UNARY.contains(&op) {
        return Err(PyValueError::new_err(format!("unknown rolling kernel {op:?}")));
    }
    if d < 1 {
        return Err(PyValueError::new_err("window length must be >= 1"));
    }
    let (s, t, n) = rows(&x)?;
    let out = py.allow_threads(|| ts::sweep(&[s], t, n, |c| ts::unary(op, c, d)));
    Ok(PyArray1::from_vec_bound(py, out).reshape([t, n])?)
}

/// Two-series rolling kernel `op` (`cov`, `corr`).
#[pyfunction]
fn ts_rolling2<'py>(
    py: Python<'py>,
    op: &str,
    x: PyReadonlyArray2<'py, f64>,
    y: PyReadonlyArray2<'py, f64>,
    d: usize,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    if !ts::BINARY.contains(&op) {
        return Err(PyValueError::new_err(format!("unknown rolling kernel {op:?}")));
    }
    if d < 1 {
        return Err(PyValueError::new_err("window length must be >= 1"));
    }
    same_shape(&x, &y)?;
    let (xs, t, n) = rows(&x)?;
    let (ys, _, _) = rows(&y)?;
    let out = py.allow_threads(|| ts::sweep(&[xs, ys], t, n, |c| ts::binary(op, c, d)));
    Ok(PyArray1::from_vec_bound(py, out).reshape([t, n])?)
}

/// Names of the rolling kernels: (one-series, two-series).
#[pyfunction]
fn ts_ops() -> (Vec<&'static str>, Vec<&'static str>) {
    (ts::UNARY.to_vec(), ts::BINARY.to_vec())
}

/// Size of the rayon pool (set ``RAYON_NUM_THREADS`` before the first call to change it).
#[pyfunction]
fn threads() -> usize {
    rayon::current_num_threads()
}

#[pymodule]
fn assay_kernels(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(cs_rank, m)?)?;
    m.add_function(wrap_pyfunction!(cs_rank_ordered, m)?)?;
    m.add_function(wrap_pyfunction!(orders_where, m)?)?;
    m.add_function(wrap_pyfunction!(rank_ic_series, m)?)?;
    m.add_function(wrap_pyfunction!(ic_series, m)?)?;
    m.add_function(wrap_pyfunction!(row_orders, m)?)?;
    m.add_function(wrap_pyfunction!(rank_ic_ordered, m)?)?;
    m.add_function(wrap_pyfunction!(group_sums, m)?)?;
    m.add_function(wrap_pyfunction!(group_sums_ordered, m)?)?;
    m.add_function(wrap_pyfunction!(ts_rolling, m)?)?;
    m.add_function(wrap_pyfunction!(ts_rolling2, m)?)?;
    m.add_function(wrap_pyfunction!(ts_ops, m)?)?;
    m.add_function(wrap_pyfunction!(threads, m)?)?;
    m.add_class::<RankCache>()?;
    Ok(())
}
