"""Alpha158 — qlib's canonical 158-feature catalog, expressed on the Assay engine.

Mirrors ``qlib.contrib.data.handler.Alpha158`` ``get_feature_config()``: 9 K-bar
shape features, 4 normalised price features, and 29 rolling features over the five
windows ``[5, 10, 20, 30, 60]`` — 9 + 4 + 29*5 = 158. Expressions use the qlib
dialect (``$close``, ``Mean``/``Std``/``Corr``/``Slope``/``Resi``/``Rsquare``/
``Quantile``/``IdxMax``/``Greater`` …), all of which the Assay parser accepts.

Grounding: ``VWAP0`` references ``$vwap``, which the daily OHLCV store does not
provide — it is kept for fidelity but fails to evaluate (the demo seeder skips
unevaluable features). Every other feature is pure OHLCV and evaluates.

Public surface mirrors :mod:`assay.factors.alpha101`: :data:`ALPHA_158`
(``name -> expr``), :func:`all_exprs`, :func:`get`.
"""

from __future__ import annotations

WINDOWS = (5, 10, 20, 30, 60)


def _build() -> dict[str, str]:
    f: dict[str, str] = {}

    # --- 2.1  K-bar shape (9) ----------------------------------------------
    f["KMID"] = "($close-$open)/$open"
    f["KLEN"] = "($high-$low)/$open"
    f["KMID2"] = "($close-$open)/($high-$low+1e-12)"
    f["KUP"] = "($high-Greater($open,$close))/$open"
    f["KUP2"] = "($high-Greater($open,$close))/($high-$low+1e-12)"
    f["KLOW"] = "(Less($open,$close)-$low)/$open"
    f["KLOW2"] = "(Less($open,$close)-$low)/($high-$low+1e-12)"
    f["KSFT"] = "(2*$close-$high-$low)/$open"
    f["KSFT2"] = "(2*$close-$high-$low)/($high-$low+1e-12)"

    # --- 2.2  normalised price (4) -----------------------------------------
    f["OPEN0"] = "$open/$close"
    f["HIGH0"] = "$high/$close"
    f["LOW0"] = "$low/$close"
    f["VWAP0"] = "$vwap/$close"  # no vwap in the OHLCV store — kept for fidelity

    # --- 2.3  rolling (29 x 5 windows) -------------------------------------
    for d in WINDOWS:
        f[f"ROC{d}"] = f"Ref($close,{d})/$close"
        f[f"MA{d}"] = f"Mean($close,{d})/$close"
        f[f"STD{d}"] = f"Std($close,{d})/$close"
        f[f"BETA{d}"] = f"Slope($close,{d})/$close"
        f[f"RSQR{d}"] = f"Rsquare($close,{d})"
        f[f"RESI{d}"] = f"Resi($close,{d})/$close"
        f[f"MAX{d}"] = f"Max($high,{d})/$close"
        f[f"MIN{d}"] = f"Min($low,{d})/$close"
        f[f"QTLU{d}"] = f"Quantile($close,{d},0.8)/$close"
        f[f"QTLD{d}"] = f"Quantile($close,{d},0.2)/$close"
        f[f"RANK{d}"] = f"Rank($close,{d})"
        f[f"RSV{d}"] = f"($close-Min($low,{d}))/(Max($high,{d})-Min($low,{d})+1e-12)"
        f[f"IMAX{d}"] = f"IdxMax($high,{d})/{d}"
        f[f"IMIN{d}"] = f"IdxMin($low,{d})/{d}"
        f[f"IMXD{d}"] = f"(IdxMax($high,{d})-IdxMin($low,{d}))/{d}"
        f[f"CORR{d}"] = f"Corr($close,Log($volume+1),{d})"
        f[f"CORD{d}"] = f"Corr($close/Ref($close,1),Log($volume/Ref($volume,1)+1),{d})"
        f[f"CNTP{d}"] = f"Mean($close>Ref($close,1),{d})"
        f[f"CNTN{d}"] = f"Mean($close<Ref($close,1),{d})"
        f[f"CNTD{d}"] = f"Mean($close>Ref($close,1),{d})-Mean($close<Ref($close,1),{d})"
        f[f"SUMP{d}"] = (
            f"Sum(Greater($close-Ref($close,1),0),{d})"
            f"/(Sum(Abs($close-Ref($close,1)),{d})+1e-12)"
        )
        f[f"SUMN{d}"] = (
            f"Sum(Greater(Ref($close,1)-$close,0),{d})"
            f"/(Sum(Abs($close-Ref($close,1)),{d})+1e-12)"
        )
        f[f"SUMD{d}"] = (
            f"(Sum(Greater($close-Ref($close,1),0),{d})-Sum(Greater(Ref($close,1)-$close,0),{d}))"
            f"/(Sum(Abs($close-Ref($close,1)),{d})+1e-12)"
        )
        f[f"VMA{d}"] = f"Mean($volume,{d})/($volume+1e-12)"
        f[f"VSTD{d}"] = f"Std($volume,{d})/($volume+1e-12)"
        f[f"WVMA{d}"] = (
            f"Std(Abs($close/Ref($close,1)-1)*$volume,{d})"
            f"/(Mean(Abs($close/Ref($close,1)-1)*$volume,{d})+1e-12)"
        )
        f[f"VSUMP{d}"] = (
            f"Sum(Greater($volume-Ref($volume,1),0),{d})"
            f"/(Sum(Abs($volume-Ref($volume,1)),{d})+1e-12)"
        )
        f[f"VSUMN{d}"] = (
            f"Sum(Greater(Ref($volume,1)-$volume,0),{d})"
            f"/(Sum(Abs($volume-Ref($volume,1)),{d})+1e-12)"
        )
        f[f"VSUMD{d}"] = (
            f"(Sum(Greater($volume-Ref($volume,1),0),{d})-Sum(Greater(Ref($volume,1)-$volume,0),{d}))"
            f"/(Sum(Abs($volume-Ref($volume,1)),{d})+1e-12)"
        )
    return f


#: ``name -> qlib expression`` for all 158 Alpha158 features.
ALPHA_158: dict[str, str] = _build()


def get(name: str) -> str:
    """Return the expression for feature ``name`` (e.g. ``"KMID"``, ``"MA20"``)."""
    try:
        return ALPHA_158[name]
    except KeyError:
        raise KeyError(f"unknown Alpha158 feature {name!r}") from None


def all_exprs() -> dict[str, str]:
    """A copy of the full ``name -> expression`` catalog."""
    return dict(ALPHA_158)
