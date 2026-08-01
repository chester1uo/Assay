"""Config-store behaviour that is easy to get subtly wrong.

``apply_to_env`` deliberately only pushes *non-empty* values, so a blank field in
the saved config never clobbers a value the operator supplied through the real
environment. ``TUSHARE_API_URL`` is the exception: blank there is a meaningful
instruction ("go back to api.tushare.pro"), and honouring it matters because the
alternative is to keep sending the Tushare token to a relay that was just removed.
"""

from __future__ import annotations

import os

from assay import config_store


def test_blank_api_url_clears_a_stale_relay_from_the_environment(monkeypatch):
    monkeypatch.setenv("TUSHARE_API_URL", "http://relay.internal/v1/tushare/query")
    config_store.apply_to_env(
        {"dirs": {}, "massive_s3": {}, "tushare": {"token": "t", "api_url": ""}}
    )
    assert "TUSHARE_API_URL" not in os.environ

    from assay.data.tushare.client import API_URL, default_base_url

    assert default_base_url() == API_URL


def test_a_configured_api_url_reaches_the_client(monkeypatch):
    monkeypatch.delenv("TUSHARE_API_URL", raising=False)
    config_store.apply_to_env(
        {"dirs": {}, "massive_s3": {}, "tushare": {"token": "t", "api_url": "http://relay/x"}}
    )
    from assay.data.tushare.client import TushareClient, default_base_url

    assert default_base_url() == "http://relay/x"
    assert TushareClient("t")._base_url == "http://relay/x"


def test_hk_gets_its_own_store_dir(monkeypatch):
    """HK used to alias the CN output dir, which silently mixed two markets."""
    monkeypatch.setattr(
        config_store,
        "load",
        lambda: {"dirs": {"assay_us": "/us", "assay_cn": "/cn", "assay_hk": "/hk"}},
    )
    assert config_store.assay_dir("HK") == "/hk"
    assert config_store.assay_dir("CN") == "/cn"
    assert config_store.assay_dir("A") == "/cn"
    assert config_store.assay_dir("US") == "/us"
