"""MASSIVE provider clients: S3 flat files and the REST corporate-actions API."""

from assay.data.massive.flatfiles import FlatFilesClient
from assay.data.massive.rest import RestClient

__all__ = ["FlatFilesClient", "RestClient"]
