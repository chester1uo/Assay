"""Isolated stdio MCP worker for the AlphaBench-reuse development smoke."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import assay
from assay.config import AssayConfig
from assay.integrations.alphabench import AlphaBenchBackend
from assay.mcp.server import mcp
from alphabench_v2.assay import AssayClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    service = assay.init(AssayConfig.for_tests(args.workspace))
    service.research.register_backend("alphabench", AlphaBenchBackend(
        AssayClient("http://127.0.0.1:8002", os.environ.get("ASSAY_API_KEY")),
    ))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
