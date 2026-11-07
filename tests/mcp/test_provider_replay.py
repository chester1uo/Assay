"""Replay must never fall back to live calls or accept a changed request."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def replay(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/smoke_alphabench_reuse.py"
    spec = importlib.util.spec_from_file_location("assay_smoke_replay", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def atomic(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    ab = SimpleNamespace(provenance=SimpleNamespace(atomic_json=atomic, payload_hash=digest),
                         privacy=SimpleNamespace(validate_blind_prompt=lambda *a, **kw: None))
    source = tmp_path / "source"
    decoding = dict(model="test", seed=17, temperature=0.7, max_tokens=8192)
    for i in range(3):
        atomic(source / "provider-calls" / f"{i:02d}.json", dict(
            status="response_received", prompt_hash=digest(f"prompt-{i}"), decoding=decoding,
            raw_response='{"expressions": ["ts_returns(close,5)"]}',
            metadata=dict(returned_model="observed-test", usage={"total_tokens": 10})))
    provider = module.ReplayProvider(source, tmp_path / "output", ab, {}, ["observed-test"])
    assert provider.client is None
    return provider, decoding, source


def test_all_responses_replay_without_a_live_client(replay):
    provider, decoding, _ = replay
    for i in range(3):
        raw, metadata = provider.complete(f"prompt-{i}", **decoding)
        assert raw and metadata["returned_model"] == "observed-test"
    assert len(provider.calls) == 3 and all(c["replayed"] for c in provider.calls)
    with pytest.raises(RuntimeError, match="no live provider"):
        provider.complete("fourth", **decoding)


@pytest.mark.parametrize("change", ["prompt", "seed", "source"])
def test_changed_request_or_source_fails_without_consuming_a_response(replay, change):
    provider, decoding, source = replay
    prompt = "prompt-0"
    if change == "prompt":
        prompt = "different"
    elif change == "seed":
        decoding["seed"] = 18
    else:
        path = source / "provider-calls/00.json"
        path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError):
        provider.complete(prompt, **decoding)
    assert provider.calls == [] and provider.client is None
