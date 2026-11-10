"""Bounded reuse of observed market panels and pure built-in factor computations."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading
import sys

import numpy as np

from assay.engine import operators as _operators

_BUILTINS = {name:s.fn for name,s in _operators.all_specs().items()
             if s.fn.__module__.startswith('assay.engine.operators.')}


class _Memo(OrderedDict):
    def __init__(self, max_bytes):
        super().__init__()
        self.max_bytes, self.bytes, self.hits = max_bytes, 0, 0

    def get(self, key, default=None):
        if key not in self:
            return default
        self.move_to_end(key)
        self.hits += 1
        return self[key]

    def __setitem__(self, key, value):
        value = np.array(value, dtype=np.float64, copy=True)
        if value.nbytes > self.max_bytes:
            return
        if key in self:
            self.bytes -= self.pop(key).nbytes
        while self and self.bytes + value.nbytes > self.max_bytes:
            self.bytes -= self.popitem(last=False)[1].nbytes
        value.flags.writeable = False
        super().__setitem__(key, value)
        self.bytes += value.nbytes

    def reset(self):
        self.clear()
        self.bytes = 0


def _implementation_digest():
    from assay import _native
    root = Path(__file__).parent
    paths = [*sorted((root/'engine').rglob('*.py')), root/'_native.py']
    if _native.kernels is not None:
        paths.append(Path(_native.kernels.__file__))
    digest = hashlib.sha256()
    digest.update(('research-values-v1:' + np.__version__).encode())
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class FactorValueCache:
    """Store raw subtrees before final membership masking; custom ops stay uncached."""
    def __init__(self, disk, panel_hash, *, max_bytes=32*1024**2):
        self.disk, self.panel_hash = disk, panel_hash
        self.memo = _Memo(max_bytes)
        self.implementation = _implementation_digest()
        self.registry = None
        self.requests = self.disk_hits = self.computations = 0

    def evaluate(self, engine, node, ctx):
        from assay.engine import operators
        from assay.engine.ast import hash_tree, iter_ops
        specs = operators.all_specs()
        registry = tuple((name, id(s.fn), s.needs_ctx, json.dumps(s.schema, sort_keys=True))
                         for name,s in sorted(specs.items()))
        if self.registry != registry:
            self.memo.reset()
            self.registry = registry
        used = [specs[name] for name in sorted(iter_ops(node))]
        if any(s.fn is not _BUILTINS.get(s.name) or
               getattr(s.fn, '__globals__', None) is not vars(sys.modules[s.fn.__module__]) for s in used):
            return engine._eval(node, ctx)
        self.requests += 1
        signature = [(s.name, s.fn.__module__, s.fn.__qualname__, s.needs_ctx, s.schema) for s in used]
        key = hashlib.sha256(json.dumps([self.implementation, self.panel_hash, str(node), signature],
                                       sort_keys=True).encode()).hexdigest()
        hashes = {}
        hash_tree(node, hashes)
        root_hash = hashes[id(node)]
        hit = self.memo.get(root_hash)
        if hit is not None:
            return hit
        hit = self.disk.get(key)
        if hit is not None and hit.shape == engine._shape:
            self.disk_hits += 1
            self.memo[root_hash] = hit
            return hit
        self.computations += 1
        raw = engine._eval_cse(node, ctx, self.memo, hashes)
        if np.isscalar(raw) or np.ndim(raw) == 0:
            raw = np.full(engine._shape, float(raw), dtype=np.float64)
        # A cache write failure must not invalidate a completed computation.
        try:
            self.disk.put(key, raw)
        except OSError:
            pass
        return raw


class ResearchExecutionPool:
    """One leased engine per local-data revision, with bounded retained memory.

    Entries serialize use of an engine's mutable memo. Separate panels can run
    concurrently. Unknown/custom stores retain their existing cold behavior.
    """
    def __init__(self, service, *, max_entries=4, max_bytes=256*1024**2):
        self.service, self.max_entries, self.max_bytes = service, max_entries, max_bytes
        self.entries = OrderedDict()
        self.lock = threading.RLock()
        self.hits = self.builds = self.evictions = 0

    def stats(self):
        with self.lock:
            return dict(panel_hits=self.hits, panel_builds=self.builds, evictions=self.evictions,
                entries=len(self.entries), retained_bytes=sum(e['bytes'] for e in self.entries.values()),
                factor_computations=sum(e['values'].computations for e in self.entries.values()),
                factor_disk_hits=sum(e['values'].disk_hits for e in self.entries.values()),
                subtree_hits=sum(e['values'].memo.hits for e in self.entries.values()))

    def _size(self, entry):
        """Estimated retained panels/arrays, including additional forward horizons."""
        engine=entry['engine']
        sess=self.service.sessions.get(entry['sid'])
        seen=set()
        def arrays(value):
            if isinstance(value,np.ndarray):
                if id(value) in seen:
                    return 0
                seen.add(id(value))
                return value.nbytes
            if isinstance(value,dict):
                return sum(arrays(v) for v in list(value.values()))
            return 0
        size=arrays(engine._matrix_cache)+arrays(engine._groups)+arrays(engine.membership)
        size+=arrays(engine.dates)+arrays(engine.symbols)+arrays(engine._di)+arrays(engine._sj)
        if sess is not None:
            size+=arrays(sess.panel)+arrays(sess._memo)+arrays(sess._fwd_returns)
        return size+engine.panel_frame().estimated_size()+entry['values'].memo.max_bytes

    @contextmanager
    def lease(self, dataset):
        from assay.data.store import DataStore
        if not callable(getattr(self.service, 'store_for_universe', None)):
            yield None
            return
        store = self.service.store_for_universe(dataset['universe'])
        if type(store) is not DataStore:
            yield None
            return
        token = store.cache_token(dataset['period'])
        spec = (id(store), dataset['universe'], tuple(dataset['period']), dataset['as_of'], dataset['adj'])
        key = (spec, token)
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                sid = self.service.create_session(universe=dataset['universe'],period=tuple(dataset['period']),
                    as_of=dataset['as_of'],adj=dataset['adj'])['session_id']
                try:
                    engine,_ = self.service._session_engine(sid,dataset['horizons'],dataset['execution'])
                    if token != store.cache_token(dataset['period']):
                        raise RuntimeError('Market files changed while loading the research panel; retry a new attempt')
                    panel_hash = engine.content_fingerprint()
                    values = FactorValueCache(self.service.cache, panel_hash)
                    engine.value_cache = values
                    arrays = [engine.field_matrix(f) for f in engine._field_cols]
                    for arr in [*arrays, engine.membership, *engine._groups.values()]:
                        if arr is not None:
                            arr.flags.writeable = False
                    entry = dict(sid=sid,engine=engine,panel_hash=panel_hash,values=values,bytes=0,
                                 lock=threading.RLock(),users=0)
                    entry['bytes']=self._size(entry)
                    self.entries[key] = entry
                    self.builds += 1
                except BaseException:
                    self.service.expire_session(sid)
                    raise
            else:
                self.hits += 1
                self.entries.move_to_end(key)
            entry['users'] += 1
        try:
            with entry['lock']:
                if token != store.cache_token(dataset['period']):
                    raise RuntimeError('Market files changed while waiting for the research panel; retry a new attempt')
                try:
                    yield entry
                finally:
                    entry['bytes']=self._size(entry)
        finally:
            with self.lock:
                entry['users'] -= 1
                stale = token != store.cache_token(dataset['period'])
                for old_key, old in list(self.entries.items()):
                    over = len(self.entries)>self.max_entries or sum(e['bytes'] for e in self.entries.values())>self.max_bytes
                    replaced = old_key[0] == spec and old_key != key
                    if not old['users'] and (over or replaced or old_key==key and stale):
                        self.entries.pop(old_key)
                        self.service.expire_session(old['sid'])
                        self.evictions += 1
