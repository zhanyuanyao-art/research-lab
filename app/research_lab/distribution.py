"""Distribution policy comes from the bundle, not a workspace setting."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

BUILD_FILE = Path(__file__).parent / "web/static/build.json"


def is_trial() -> bool:
    try:
        info = json.loads(BUILD_FILE.read_text())
    except (OSError, ValueError):
        info = {}
    # A runtime flag may restrict a source checkout, never unlock a trial bundle.
    return (info.get("distribution") == "trial" or os.environ.get("RL_DISTRIBUTION") == "trial"
            or (getattr(sys, "frozen", False) and not info))
