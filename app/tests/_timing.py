"""How much slower than a developer's Mac this machine may be. The hostile-input tests give each scan a second or so
to catch work that grows faster than the input (that costs tens of seconds, not a few tenths); a shared CI runner
sets RL_TIMING_SCALE (docs/public/ci.yml) so ordinary slowness there is not mistaken for it."""
from __future__ import annotations

import os

SLOW = max(1.0, float(os.environ.get("RL_TIMING_SCALE") or 1))
