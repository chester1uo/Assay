"""Point-in-time read layer over the prepared parquet stores."""

from assay.data.store.adjust import forward_adjust
from assay.data.store.datastore import DataStore

__all__ = ["DataStore", "forward_adjust"]
