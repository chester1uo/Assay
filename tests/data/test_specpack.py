"""Spec packs: the same inputs as gathering from the panel, mapped from disk."""

import json

import numpy as np
import pytest

pytest.importorskip("pyarrow")

from assay.ades import slice as sl  # noqa: E402
from assay.data import specpack  # noqa: E402
from assay.data.store import QFVPanelStore  # noqa: E402
from assay.engine import parse  # noqa: E402

from tests.data.test_qfv_store import write_store  # noqa: E402

SPEC = {"key": "k1", "universe": "TOP500", "start": "2024-12-02", "end": "2025-01-24", "horizons": [1, 5],
        "execution": "next_open"}
EXPRS = ["ts_delta(close, 2)", "cs_rank(volume)", "ts_corr(close, open, 3)", "ts_rank(high, 4) - ts_mean(low, 2)"]


@pytest.fixture
def panel(tmp_path):
    root = tmp_path / "panel"
    root.mkdir()
    write_store(root)
    return root


def _same(a, b):
    np.testing.assert_array_equal(np.asarray(a), np.asarray(b))


def test_pack_matches_the_store(panel, tmp_path, monkeypatch):
    store = QFVPanelStore(panel)
    monkeypatch.delenv("ASSAY_PACK_DIR", raising=False)
    plain = sl.load_spec(store, SPEC)
    monkeypatch.setenv("ASSAY_PACK_DIR", str(tmp_path / "packs"))
    packed = sl.load_spec(store, SPEC)
    a, b = plain.engine, packed.engine
    _same(a.dates, b.dates)
    assert list(a.symbols) == list(b.symbols) and a.snapshot == b.snapshot
    _same(a.membership, b.membership)
    for f in sl.FIELDS:
        _same(a._matrix(f), b._matrix(f))
    assert sorted(plain.fwd) == sorted(packed.fwd) == [1, 5]
    for h in plain.fwd:
        _same(plain.fwd[h], packed.fwd[h])
    for e in EXPRS:                                     # operators never write into the mapped inputs
        _same(a.evaluate(parse(e)).values, b.evaluate(parse(e)).values)
    close = b._matrix("close")
    assert type(close) is np.ndarray and isinstance(close.base, np.memmap) and not close.flags.writeable
    # one array per horizon for the life of the spec, so the evaluator's caches keyed by
    # identity (forward-return orders and ranks) hit from the second factor on
    assert np.ascontiguousarray(packed.fwd[1], dtype=np.float64) is packed.fwd[1]


def test_pack_is_built_once_and_grows_horizons(panel, tmp_path):
    store, root = QFVPanelStore(panel), tmp_path / "packs"
    p1 = specpack.open_or_build(store, "TOP500", "2024-12-02", "2025-01-24", fields=sl.FIELDS, root=root)
    built = json.loads((p1.path / "meta.json").read_text())["built_at"]
    p1.fwd([1], "next_open")
    p2 = specpack.open_or_build(store, "TOP500", "2024-12-02", "2025-01-24", fields=["close"], root=root)
    assert p2.path == p1.path and p2.meta["built_at"] == built
    assert sorted(f.name for f in p2.path.glob("fwd-*")) == ["fwd-next_open-1.npy"]
    p2.fwd([1, 10], "next_open")
    assert sorted(f.name for f in p2.path.glob("fwd-*")) == ["fwd-next_open-1.npy", "fwd-next_open-10.npy"]
    assert not [f for f in p2.path.parent.iterdir() if f.name.startswith(".")]      # no temporaries left


def test_a_new_snapshot_gets_a_new_pack_and_drops_the_old(panel, tmp_path):
    root = tmp_path / "packs"
    old = specpack.open_or_build(QFVPanelStore(panel), "TOP500", "2024-12-02", "2025-01-24",
                                 fields=sl.FIELDS, root=root)
    keep = np.asarray(old.fields["close"]).copy()
    write_store(panel, version="v000002", last="2025-01-31")
    new = specpack.open_or_build(QFVPanelStore(panel), "TOP500", "2024-12-02", "2025-01-31",
                                 fields=sl.FIELDS, root=root)
    assert new.path.parent.name.endswith("v000002") and not old.path.exists()
    assert new.fields["close"].shape[0] > keep.shape[0]
    _same(old.fields["close"], keep)                   # a reader of the old pack is unaffected


def test_no_pack_dir_means_no_packs(panel, monkeypatch):
    monkeypatch.delenv("ASSAY_PACK_DIR", raising=False)
    assert specpack.pack_root() is None
    with pytest.raises(ValueError):
        specpack.open_or_build(QFVPanelStore(panel), "TOP500", "2024-12-02", "2025-01-24", fields=["close"])


def test_next_close_does_not_reuse_signal_close_labels(panel,tmp_path):
    pack=specpack.open_or_build(QFVPanelStore(panel),'TOP500','2024-12-02','2025-01-24',
                               fields=sl.FIELDS,root=tmp_path/'packs')
    # Simulate the persistent file produced by the pre-upgrade implementation.
    legacy=pack.path/'fwd-next_close-1.npy'
    np.save(legacy,np.full(pack.fields['close'].shape,99.))
    result=pack.fwd([1],'next_close')[1]
    close=pack.fields['close']
    want=np.full(close.shape,np.nan)
    want[:-2]=close[2:]/close[1:-1]-1
    np.testing.assert_allclose(result,want,equal_nan=True)
    assert (pack.path/'fwd-next_close-v2-1.npy').exists()
    assert np.all(np.load(legacy)==99.)
