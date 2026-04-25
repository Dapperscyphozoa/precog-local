"""
state_io.py — Atomic disk snapshot. JSON only, no DB.

Saves & restores:
  - cooldowns per coin
  - WR history per coin
  - open signal cache
  - consec losses

Atomic write pattern: write to .tmp → rename → never partial.
"""

import json
import os
import tempfile
import logging

log = logging.getLogger("state_io")


def save_state(path, state_dict):
    """Atomic JSON write. Returns True on success."""
    try:
        d = os.path.dirname(os.path.abspath(path)) or '.'
        os.makedirs(d, exist_ok=True)
        # Write to temp file in same directory
        fd, tmp = tempfile.mkstemp(prefix='.precog_state_', suffix='.tmp', dir=d)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(state_dict, f, indent=2, default=str)
            os.replace(tmp, path)  # atomic on POSIX
            return True
        except Exception:
            try: os.unlink(tmp)
            except Exception: pass
            raise
    except Exception as e:
        log.warning(f"save_state err: {e}")
        return False


def load_state(path):
    """Returns dict or empty dict if file missing/corrupt."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"load_state err: {e}")
        return {}
