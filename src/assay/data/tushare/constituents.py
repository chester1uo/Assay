"""Index code constants and vendored Hong Kong index constituents.

A-share index membership (CSI300/500/1000) is fetched point-in-time from
Tushare's ``index_weight`` interface, so it is *not* vendored here — only the
index ts_codes are.

Hong Kong is different: **Tushare exposes no index-membership interface for HSI
or Hang Seng TECH** (``index_member`` / ``index_basic market=HK`` return
nothing). To still provide an HK universe we vendor a single CURRENT snapshot of
each index's constituents below.

    ⚠️  SURVIVORSHIP BIAS: this is one current snapshot, not point-in-time
    history. Backtests over the HK universe that rely on this list will be
    survivorship-biased (names that left the index before today are absent;
    names are present for their whole history even before they joined). Replace
    these lists with dated membership if/when a historical source is available.

Codes are HKEX numeric tickers; Tushare ts_codes are the 5-digit zero-padded
form plus ``.HK`` (e.g. 700 -> ``00700.HK``). Captured 2026-06-27 from Hang Seng
Indexes / Wikipedia / AAStocks.
"""

from __future__ import annotations

# A-share index ts_codes for Tushare ``index_weight``. Each index maps to one or
# more provider codes that are merged into a single membership history: the first
# code is canonical, later codes only fill gaps the canonical one is missing.
#
# CSI300 needs both: ``000300.SH`` has weights only from 2016-01 onward, while the
# SZSE mirror ``399300.SZ`` carries the 2010-2015 history (daily granularity).
CN_INDEX_CODES: dict[str, tuple[str, ...]] = {
    "CSI300": ("000300.SH", "399300.SZ"),
    "CSI500": ("000905.SH",),
    "CSI1000": ("000852.SH",),
}

# HK index value-series ts_codes for Tushare ``index_global``.
# NB: Hang Seng TECH is "HKTECH" on index_global ("HSTECH" returns nothing).
HK_INDEX_CODES: dict[str, str] = {
    "HSI": "HSI",
    "HSTECH": "HKTECH",
}

# Current HSI constituents (HKEX numeric codes), captured 2026-06-27.
HSI_CODES: tuple[int, ...] = (
    1, 2, 3, 5, 6, 12, 16, 27, 66, 101, 175, 241, 267, 285, 288, 291, 300, 316,
    322, 386, 388, 669, 688, 700, 762, 823, 836, 857, 868, 881, 883, 939, 941,
    960, 968, 981, 992, 1024, 1038, 1044, 1088, 1093, 1099, 1109, 1113, 1177,
    1209, 1211, 1299, 1378, 1398, 1810, 1876, 1928, 1929, 1997, 2015, 2020,
    2057, 2269, 2313, 2318, 2319, 2331, 2359, 2382, 2388, 2618, 2628, 2688,
    2899, 3690, 3692, 3968, 3988, 6618, 6690, 6862, 9618, 9633, 9888, 9961,
    9988, 9992, 9999,
)

# Current Hang Seng TECH constituents (HKEX numeric codes), captured 2026-06-27.
HSTECH_CODES: tuple[int, ...] = (
    20, 100, 241, 285, 300, 700, 780, 981, 992, 1024, 1211, 1347, 1698, 1810,
    2015, 2382, 2513, 3690, 6618, 6690, 9618, 9626, 9660, 9863, 9866, 9868,
    9888, 9961, 9988, 9999,
)

_CODES_BY_INDEX: dict[str, tuple[int, ...]] = {
    "HSI": HSI_CODES,
    "HSTECH": HSTECH_CODES,
}


def _ts_code(numeric: int) -> str:
    """HKEX numeric ticker -> Tushare ts_code, e.g. 700 -> ``00700.HK``."""
    return f"{int(numeric):05d}.HK"


def hk_constituents() -> list[tuple[str, str]]:
    """Return ``(ts_code, index)`` rows across HSI + HSTECH (no dedup).

    A name that sits in both indices appears once per index, so the result is a
    membership table, not a unique universe — see :func:`hk_universe` for the
    deduplicated symbol list.
    """
    rows: list[tuple[str, str]] = []
    for index, codes in _CODES_BY_INDEX.items():
        for code in codes:
            rows.append((_ts_code(code), index))
    return rows


def hk_universe() -> list[str]:
    """Deduplicated, sorted union of HSI + HSTECH ts_codes."""
    return sorted({_ts_code(c) for codes in _CODES_BY_INDEX.values() for c in codes})
