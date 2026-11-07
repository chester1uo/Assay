#!/bin/bash
# Build the assay_kernels wheel (rust/assay-kernels) with maturin. Run on a build host
# with a Rust toolchain; the wheel is abi3 (CPython >= 3.11) for the host architecture.
#   scripts/build_kernels.sh [python]     -> rust/assay-kernels/target/wheels/*.whl
set -euo pipefail
cd "$(dirname "$0")/../rust/assay-kernels"
PY=${1:-python3}
maturin build --release -i "$PY"
ls -1 target/wheels/*.whl
