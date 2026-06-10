"""Opt-in network smoke tests against the live MASSIVE API.

Run explicitly with:  pytest -m network
Skipped automatically when credentials are absent.
"""

import datetime as dt
import os

import pytest

pytestmark = pytest.mark.network

_HAS_CREDS = all(
    os.environ.get(k)
    for k in ("MASSIVE_API_KEY", "MASSIVE_S3_ACCESS_KEY_ID", "MASSIVE_S3_SECRET_ACCESS_KEY")
)
# config also loads .env, so check that path too
if not _HAS_CREDS:
    try:
        from assay.config import MassiveConfig

        MassiveConfig.from_env()
        _HAS_CREDS = True
    except Exception:
        _HAS_CREDS = False

pytest.importorskip("boto3")
if not _HAS_CREDS:
    pytest.skip("MASSIVE credentials not configured", allow_module_level=True)


@pytest.fixture(scope="module")
def massive():
    from assay.config import MassiveConfig

    return MassiveConfig.from_env()


def test_flatfiles_discover_and_columns(massive):
    from assay.data.massive import FlatFilesClient
    from assay.data.schemas import DAY_AGG_CSV_COLUMNS

    client = FlatFilesClient(massive)
    prefixes = client.list_top_level_prefixes()
    assert any("us_stocks_sip" in p for p in prefixes)

    recent = client.list_day_aggs(dt.date.today() - dt.timedelta(days=14), dt.date.today())
    assert recent, "expected at least one recent day-aggregate file"
    df = client.read_day_agg(recent[-1].date, symbols={"AAPL"})
    assert df is not None and df.height >= 1
    assert set(DAY_AGG_CSV_COLUMNS).issubset(set(df.columns))
    assert "date" in df.columns


def test_rest_splits_and_dividends(massive):
    from assay.data.massive import RestClient

    client = RestClient(massive)
    splits = list(client.iter_splits(ticker="AAPL"))
    assert any(s["execution_date"] == "2020-08-31" and s["split_to"] == 4 for s in splits)

    divs = list(client.iter_dividends(ticker="AAPL", **{"ex_dividend_date.gte": "2022-01-01",
                                                        "ex_dividend_date.lte": "2022-12-31"}))
    assert divs and all(d["ticker"] == "AAPL" for d in divs)
