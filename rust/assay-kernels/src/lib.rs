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

/// `cs_rank`: per row, average rank / (n - 1) over the non-NaN entries; 0.5 for a
/// lone value, NaN where the input is NaN.
#[pyfunction]
fn cs_rank<'py>(py: Python<'py>, x: PyReadonlyArray2<'py, f64>) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (s, t, n) = rows(&x)?;
    let mut out = vec![f64::NAN; t * n];
    py.allow_threads(|| {
        out.par_chunks_mut(n.max(1)).enumerate().for_each_init(
            || (Vec::new(), Vec::new(), Vec::new(), Vec::new()),
            |(vals, idx, order, r), (row, o)| {
                let src = &s[row * n..row * n + n];
                vals.clear();
                idx.clear();
                for (j, &v) in src.iter().enumerate() {
                    if !v.is_nan() {
                        vals.push(v);
                        idx.push(j);
                    }
                }
                let m = vals.len();
                if m == 0 {
                    return;
                }
                if m == 1 {
                    o[idx[0]] = 0.5;
                    return;
                }
                r.resize(m, 0.0);
                avg_ranks(vals, order, r);
                let d = (m - 1) as f64;
                for k in 0..m {
                    o[idx[k]] = r[k] / d;
                }
            },
        );
    });
    Ok(PyArray1::from_vec_bound(py, out).reshape([t, n])?)
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
        order.par_chunks_mut(n.max(1)).zip(count.par_iter_mut()).enumerate().for_each(|(row, (o, c))| {
            let src = &s[row * n..row * n + n];
            let mut m = 0;
            for (j, v) in src.iter().enumerate() {
                if v.is_finite() {
                    o[m] = j as u32;
                    m += 1;
                }
            }
            o[..m].sort_by(|&a, &b| src[a as usize].partial_cmp(&src[b as usize]).unwrap());
            *c = m as u32;
        });
    });
    Ok((PyArray1::from_vec_bound(py, order).reshape([t, n])?, PyArray1::from_vec_bound(py, count)))
}

/// Average ranks (as [`avg_ranks`]) of the entries of `vals` with `keep[j]`, read off
/// the precomputed value `order`; written to `out[j]`.
fn ranks_from_order(vals: &[f64], order: &[u32], keep: &[bool], out: &mut [f64]) {
    let m = order.len();
    let (mut pos, mut i) = (0usize, 0usize);
    while i < m {
        let v = vals[order[i] as usize];
        let (mut end, mut cnt) = (i, 0usize);
        while end < m && vals[order[end] as usize] == v {
            cnt += keep[order[end] as usize] as usize;
            end += 1;
        }
        if cnt > 0 {
            let avg = ((pos + pos + cnt - 1) as f64) / 2.0;
            for &j in &order[i..end] {
                if keep[j as usize] {
                    out[j as usize] = avg;
                }
            }
            pos += cnt;
        }
        i = end;
    }
}

/// [`rank_ic_series`] with the per-row orders of `x` and `y` from [`row_orders`]:
/// the same ranks and the same Pearson sums, so the same values bit for bit.
#[pyfunction]
#[pyo3(signature = (x, xo, xc, y, yo, yc, lag=0))]
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
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    same_shape(&x, &y)?;
    let (xs, t, n) = rows(&x)?;
    let (ys, _, _) = rows(&y)?;
    let bad = || PyValueError::new_err("orders must come from row_orders of the same matrices");
    if xo.shape() != [t, n] || yo.shape() != [t, n] || xc.len() != t || yc.len() != t {
        return Err(bad());
    }
    let (xo, yo) = (xo.as_slice().map_err(|_| bad())?, yo.as_slice().map_err(|_| bad())?);
    let (xc, yc) = (xc.as_slice().map_err(|_| bad())?, yc.as_slice().map_err(|_| bad())?);
    if xc.iter().chain(yc).any(|&c| c as usize > n) {
        return Err(bad());
    }
    let mut out = vec![f64::NAN; t];
    py.allow_threads(|| {
        out.par_iter_mut().enumerate().for_each_init(
            || (vec![false; n], vec![0.0f64; n], vec![0.0f64; n], Vec::new(), Vec::new()),
            |(keep, rx, ry, a, b), (row, o)| {
                if row < lag {
                    return;
                }
                let xr0 = (row - lag) * n;
                let (xr, yr) = (&xs[xr0..xr0 + n], &ys[row * n..row * n + n]);
                let mut m = 0;
                for j in 0..n {
                    keep[j] = xr[j].is_finite() && yr[j].is_finite();
                    m += keep[j] as usize;
                }
                if m < 2 {
                    return;
                }
                ranks_from_order(xr, &xo[xr0..xr0 + xc[row - lag] as usize], keep, rx);
                ranks_from_order(yr, &yo[row * n..row * n + yc[row] as usize], keep, ry);
                a.clear();
                b.clear();
                for j in 0..n {
                    if keep[j] {
                        a.push(rx[j]);
                        b.push(ry[j]);
                    }
                }
                *o = pearson(a, b);
            },
        );
    });
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

/// Rolling time-series kernel `op` (one of `ts.rs`'s `UNARY`) over windows of `d` rows,
/// column by column.
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
    let out = py.allow_threads(|| ts::by_column(&[s], t, n, |c, o| ts::unary(op, c[0], d, o)));
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
    let out = py.allow_threads(|| ts::by_column(&[xs, ys], t, n, |c, o| ts::binary(op, c[0], c[1], d, o)));
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
    m.add_function(wrap_pyfunction!(rank_ic_series, m)?)?;
    m.add_function(wrap_pyfunction!(ic_series, m)?)?;
    m.add_function(wrap_pyfunction!(row_orders, m)?)?;
    m.add_function(wrap_pyfunction!(rank_ic_ordered, m)?)?;
    m.add_function(wrap_pyfunction!(group_sums, m)?)?;
    m.add_function(wrap_pyfunction!(ts_rolling, m)?)?;
    m.add_function(wrap_pyfunction!(ts_rolling2, m)?)?;
    m.add_function(wrap_pyfunction!(ts_ops, m)?)?;
    m.add_function(wrap_pyfunction!(threads, m)?)?;
    Ok(())
}
