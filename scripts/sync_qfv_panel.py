"""Copy the QFinVerse daily-panel reader into assay/data/qfv_panel.

Assay reads the store QFinVerse builds (format ``qfv-daily-panel/1``) without
depending on the QFinVerse package: the reader is three small files, vendored.
Run after a reader change in QFinVerse:

    python scripts/sync_qfv_panel.py ../QFinVerse/src/qfinverse/data/daily
"""

import re
import sys
from pathlib import Path

HEADER = ('# Vendored from QFinVerse src/qfinverse/data/daily/{name} by scripts/sync_qfv_panel.py.\n'
          '# Do not edit here: change QFinVerse and sync.\n')


def main(src: str) -> None:
    src_dir = Path(src)
    out = Path(__file__).resolve().parents[1] / "src" / "assay" / "data" / "qfv_panel"
    out.mkdir(parents=True, exist_ok=True)
    for name in ("layout.py", "reader.py"):
        (out / name).write_text(HEADER.format(name=name) + (src_dir / name).read_text())
    sec = (src_dir / "securities.py").read_text()
    run_index = sec[sec.index("class RunIndex"):]
    head = '"""Ticker runs: which security a ticker meant on a date."""\n\nfrom __future__ import annotations\n\n' \
           'from typing import Optional\n\n\n'
    (out / "securities.py").write_text(HEADER.format(name="securities.py") + head + run_index)
    (out / "__init__.py").write_text(HEADER.format(name="__init__.py") +
                                     '"""Reader of the QFinVerse daily panel store."""\n\n'
                                     'from .reader import DailyPanel, Panel\n\n__all__ = ["DailyPanel", "Panel"]\n')
    print(f"synced {src_dir} -> {out}")


if __name__ == "__main__":
    main(sys.argv[1])
