"""Local MASSIVE source readers: day-aggregate parquet and corporate-action JSONL.

The pipeline transforms a locally-downloaded MASSIVE mirror into the Assay
parquet stores; nothing here touches the network.
"""

from assay.data.massive.corpactions import LocalCorpActions
from assay.data.massive.flatfiles import DayAggFile, LocalFlatFiles

__all__ = ["LocalFlatFiles", "LocalCorpActions", "DayAggFile"]
