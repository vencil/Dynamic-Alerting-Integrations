"""
_lib_python.py — Shared Python utilities for da-tools scripts.

Canonical implementations of:
  - parse_duration_seconds(): Prometheus-style duration → seconds
  - format_duration(): seconds → Prometheus-style duration
  - is_disabled(): three-state disable check
  - load_yaml_file(): YAML loading with encoding + error handling
"""
import os
import re

import yaml

# ---------------------------------------------------------------------------
# Duration parsing + formatting
# ---------------------------------------------------------------------------
_DURATION_RE = re.compile(r"^(\d+\.?\d*)([smhd])$")
_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration_seconds(value):
    """Parse a Prometheus-style duration string to seconds.

    Supports: 5s, 30s, 1m, 5m, 1h, 4h, 72h, 1d, numeric int/float.
    Returns seconds as int, or None if invalid.
    """
    if isinstance(value, (int, float)):
        return int(value)
    if not value or not isinstance(value, str):
        return None
    m = _DURATION_RE.match(str(value).strip())
    if not m:
        return None
    return int(float(m.group(1)) * _DURATION_MULTIPLIERS[m.group(2)])


def format_duration(seconds):
    """Format seconds back to a Prometheus-compatible duration string.

    NOTE: Prometheus/Alertmanager only supports s/m/h (not d/w/y).
    Do NOT convert to days even if evenly divisible.
    """
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


# ---------------------------------------------------------------------------
# Three-state helpers
# ---------------------------------------------------------------------------
_DISABLED_VALUES = frozenset(("disable", "disabled", "off", "false"))


def is_disabled(value):
    """Check if a string value means disabled state (Go-consistent)."""
    if not value or not isinstance(value, str):
        return False
    return value.strip().lower() in _DISABLED_VALUES


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------
def load_yaml_file(path, default=None):
    """Load YAML file with consistent encoding and error handling.

    Returns parsed data, or default if file not found / empty.
    """
    if not path or not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else default
