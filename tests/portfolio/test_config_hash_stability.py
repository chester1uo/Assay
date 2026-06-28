"""Config-identity contract: frozen daily ``config_hash`` + additive-field isolation.

``config_hash`` drives ``PortfolioReport.run_id``; it must stay byte-stable for
the daily path so additive (future intraday) fields never change an existing run
identity. The pin was computed from the pre-minute code.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields

from assay.portfolio.config import _HASH_FIELDS_V1, PortfolioBacktestConfig

# Pinned daily config-identity digest — frozen contract. Do not edit to pass.
_PINNED_US = "efe2746a864e"
# Pinned full serialized-payload digest (json.dumps sort_keys). Freezes the
# daily to_dict() byte-shape so any additive field (e.g. M6 schema_version) is a
# conscious, test-breaking change rather than silent drift (design §10).
_PINNED_US_PAYLOAD = "efe2746a864e3818"


def test_config_hash_preset_us_frozen():
    assert PortfolioBacktestConfig.preset("US").config_hash() == _PINNED_US


def test_config_payload_byte_shape_frozen():
    payload = json.dumps(
        PortfolioBacktestConfig.preset("US").to_dict(), sort_keys=True, separators=(",", ":")
    )
    assert hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16] == _PINNED_US_PAYLOAD


def test_hash_allowlist_names_are_real_fields():
    # Every allowlisted name must be an actual field (guards V1 typos; a stale
    # name would silently drop a field from the preimage and drift the hash).
    field_names = {f.name for f in fields(PortfolioBacktestConfig)}
    assert _HASH_FIELDS_V1 <= field_names


def test_additive_field_excluded_via_real_config_hash(monkeypatch):
    # Exercise the REAL config_hash() (not an inline re-implementation) on a
    # config whose to_dict() carries a non-V1 field, proving the allowlist filter
    # is load-bearing: config_hash() must ignore the extra field, while an
    # unfiltered full-dict hash would differ.
    cfg = PortfolioBacktestConfig.preset("US")
    full = cfg.to_dict()
    full_with_extra = {**full, "bar_interval": "minute"}  # simulate an M6 intraday field
    monkeypatch.setattr(cfg, "to_dict", lambda: full_with_extra)

    assert cfg.config_hash() == _PINNED_US  # extra field filtered out by the allowlist

    unfiltered = hashlib.sha256(
        json.dumps(full_with_extra, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    assert unfiltered != _PINNED_US  # the filter is genuinely load-bearing
