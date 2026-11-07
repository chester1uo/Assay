"""Which build is running: the deployment's git commit, shown as ``Build <id>`` in the WebUI.

A deployment sets ``ASSAY_GIT_HASH`` (and optionally ``ASSAY_DEPLOYED_AT``); a source
checkout falls back to ``git rev-parse HEAD``. Without either the id is ``dev``.
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

_HEX = re.compile(r"^[0-9a-f]{7,40}$", re.I)
_REPO = Path(__file__).resolve().parents[3]


def _git_head() -> str | None:
    if not (_REPO / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


@lru_cache(maxsize=1)
def build_info() -> dict[str, str | None]:
    raw = os.environ.get("ASSAY_GIT_HASH", "").strip()
    commit = raw if _HEX.match(raw) else _git_head()
    commit = commit if commit and _HEX.match(commit) else None
    return {
        "id": commit[:7] if commit else "dev",
        "commit": commit,
        "deployed_at": os.environ.get("ASSAY_DEPLOYED_AT") or None,
    }
