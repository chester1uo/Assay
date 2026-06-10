"""Ingesters: normalize loaded MASSIVE data into the Assay parquet stores."""

from assay.data.ingest.corporate_actions import CorpActionIngester
from assay.data.ingest.prices import PriceIngester
from assay.data.ingest.universe import UniverseIngester

__all__ = ["PriceIngester", "CorpActionIngester", "UniverseIngester"]
