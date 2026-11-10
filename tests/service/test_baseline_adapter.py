"""Native checkpoints preserve settings without serializing live MCP sessions."""
import importlib
import pickle
from pathlib import Path
import pytest


def test_native_evaluator_checkpoint_requires_rebinding_and_preserves_constraint(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'experiments/baseline-acceptance'))
    cls=importlib.import_module('adapters').NativeAssayEvaluator
    live=lambda: None  # a deliberately unpicklable stand-in for the live transport
    evaluator=cls(live,{'execution':'next_open'})
    state=pickle.loads(pickle.dumps(dict(evaluator=evaluator,constraint=evaluator.validate_expression)))
    restored=state['evaluator']
    assert restored.tool==evaluator.tool and evaluator.gateway is live
    assert state['constraint'].__self__ is restored
    with pytest.raises(RuntimeError,match='Bind a live Assay MCP gateway'):
        restored._gateway()
    restored.bind(live)
    assert restored._gateway() is live
