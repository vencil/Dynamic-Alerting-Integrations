"""
_lib_python.py — Shared Python utilities for da-tools scripts.

Canonical implementations of:
  - parse_duration_seconds(): Prometheus-style duration → seconds
  - format_duration(): seconds → Prometheus-style duration
  - is_disabled(): three-state disable check
  - load_yaml_file(): YAML loading with encoding + error handling
  - validate_and_clamp(): Timing parameter guardrail enforcement

Domain constants (single source of truth for Python tools):
  - VALID_RESERVED_KEYS / VALID_RESERVED_PREFIXES: tenant config reserved keys
  - GUARDRAILS / PLATFORM_DEFAULTS: timing parameter bounds
  - RECEIVER_TYPES / RECEIVER_URL_FIELDS: Alertmanager receiver definitions
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional, Union

import yaml

# ---------------------------------------------------------------------------
# Duration parsing + formatting
# ---------------------------------------------------------------------------
_DURATION_RE = re.compile(r"^(\d+\.?\d*)([smhd])$")
_DURATION_MULTIPLIERS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def detect_cli_lang() -> str:
    """Detect CLI language from environment variables.

    Checks in order: ``DA_LANG``, ``LC_ALL``, ``LANG``.
    Returns ``'zh'`` if any starts with ``zh``, ``'en'`` otherwise.
    """
    for var in ('DA_LANG', 'LC_ALL', 'LANG'):
        val = os.environ.get(var, '')
        if val.startswith('zh'):
            return 'zh'
        if val.startswith('en'):
            return 'en'
    return 'en'


def parse_duration_seconds(value: Union[str, int, float, None]) -> Optional[int]:
    """Parse a Prometheus-style duration string to seconds.

    Accepts: ``5s``, ``30s``, ``1m``, ``5m``, ``1h``, ``4h``, ``72h``,
    ``1d``, or numeric ``int``/``float``.

    Returns:
        Seconds as ``int``, or ``None`` if *value* is invalid.
    """
    if isinstance(value, (int, float)):
        return int(value)
    if not value or not isinstance(value, str):
        return None
    m = _DURATION_RE.match(str(value).strip())
    if not m:
        return None
    return int(float(m.group(1)) * _DURATION_MULTIPLIERS[m.group(2)])


def format_duration(seconds: int) -> str:
    """Format seconds back to a Prometheus-compatible duration string.

    Uses the largest whole unit that divides evenly (``h`` → ``m`` → ``s``).

    Note:
        Prometheus/Alertmanager only supports ``s``/``m``/``h`` (not ``d``).
        This function intentionally never produces day units.
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


def is_disabled(value: Any) -> bool:
    """Check if *value* represents a disabled state.

    Recognises: ``disable``, ``disabled``, ``off``, ``false``
    (case-insensitive, stripped). Consistent with the Go
    ``IsDisabledValue()`` implementation.
    """
    if not value or not isinstance(value, str):
        return False
    return value.strip().lower() in _DISABLED_VALUES


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------
def load_yaml_file(path: Optional[str], default: Any = None) -> Any:
    """Load a YAML file with UTF-8 encoding and safe parsing.

    Args:
        path: Filesystem path.  Returns *default* if ``None``, empty,
              or non-existent.
        default: Fallback value when the file is missing or empty.

    Returns:
        Parsed YAML data, or *default*.
    """
    if not path or not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else default


# ============================================================
# Reserved Tenant Config Keys (Python source of truth)
# ============================================================
# Go equivalent: components/threshold-exporter/app/config.go
#   validReservedKeys + validReservedPrefixes — keep in sync.
VALID_RESERVED_KEYS: set[str] = {
    "_silent_mode", "_severity_dedup", "_namespaces", "_metadata", "_profile",
}
VALID_RESERVED_PREFIXES: tuple[str, ...] = ("_state_", "_routing")


# ============================================================
# Timing Guardrails
# ============================================================
# Format: (min_seconds, max_seconds, description)
GUARDRAILS: dict[str, tuple[int, int, str]] = {
    "group_wait": (5, 300, "5s–5m"),
    "group_interval": (5, 300, "5s–5m"),
    "repeat_interval": (60, 259200, "1m–72h"),
}

# Platform defaults (used when tenant doesn't specify)
PLATFORM_DEFAULTS: dict[str, Any] = {
    "group_by": ["alertname", "tenant"],
    "group_wait": "30s",
    "group_interval": "5m",
    "repeat_interval": "4h",
}


# ============================================================
# Receiver Types
# ============================================================
# Each type maps to: (alertmanager_config_key, required_fields, optional_fields)
RECEIVER_TYPES: dict[str, dict[str, Any]] = {
    "webhook": {
        "am_key": "webhook_configs",
        "required": ["url"],
        "optional": ["send_resolved", "http_config"],
    },
    "email": {
        "am_key": "email_configs",
        "required": ["to", "smarthost"],
        "optional": ["from", "auth_username", "auth_password", "require_tls",
                      "html", "text", "headers", "send_resolved"],
    },
    "slack": {
        "am_key": "slack_configs",
        "required": ["api_url"],
        "optional": ["channel", "title", "text", "title_link", "icon_emoji",
                      "send_resolved"],
    },
    "teams": {
        "am_key": "msteams_configs",
        "required": ["webhook_url"],
        "optional": ["title", "text", "send_resolved"],
    },
    "rocketchat": {
        "am_key": "webhook_configs",
        "required": ["url"],
        "optional": ["send_resolved"],
        "metadata": ["channel", "username", "icon_url"],  # documented but not passed to AM
    },
    "pagerduty": {
        "am_key": "pagerduty_configs",
        "required": ["service_key"],
        "optional": ["routing_key", "severity", "description", "client",
                      "client_url", "send_resolved"],
    },
}


# ============================================================
# Webhook Domain Allowlist (SSRF prevention)
# ============================================================
# Maps receiver type → list of fields that contain URLs to validate
RECEIVER_URL_FIELDS: dict[str, list[str]] = {
    "webhook":    ["url"],
    "email":      ["smarthost"],      # host:port format
    "slack":      ["api_url"],
    "teams":      ["webhook_url"],
    "rocketchat": ["url"],
    "pagerduty":  [],                 # service_key only, no URL
}


# ============================================================
# Onboard Hints (pipeline state between onboard → scaffold)
# ============================================================
ONBOARD_HINTS_FILENAME: str = "onboard-hints.json"


def write_onboard_hints(output_dir: str, hints: dict[str, Any]) -> str:
    """Write onboard hints JSON for scaffold consumption.

    Args:
        output_dir: Directory to write ``onboard-hints.json`` into.
        hints: Data dict (tenants, db_types, routing_hints, …).

    Returns:
        Absolute path to the written file.
    """
    import json
    path = os.path.join(output_dir, ONBOARD_HINTS_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hints, f, indent=2, ensure_ascii=False)
    os.chmod(path, 0o600)
    return path


def read_onboard_hints(path: Optional[str]) -> Optional[dict[str, Any]]:
    """Read onboard hints JSON.

    Returns:
        Parsed dict, or ``None`` if file is missing / unreadable.
    """
    import json
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_and_clamp(
    param: str,
    value: Union[str, int, float],
    tenant: str,
) -> tuple[Union[str, int, float], list[str]]:
    """Validate a timing parameter against guardrails and clamp if needed.

    Args:
        param: Parameter name (``group_wait``, ``group_interval``,
               ``repeat_interval``).
        value: Duration string (e.g. ``"30s"``) or numeric seconds.
        tenant: Tenant identifier (used in warning messages).

    Returns:
        A 2-tuple ``(clamped_value, warnings)`` where *warnings* is a
        list of human-readable strings (empty if within bounds).
    """
    warnings: list[str] = []

    if param not in GUARDRAILS:
        return value, warnings

    min_sec, max_sec, desc = GUARDRAILS[param]
    seconds = parse_duration_seconds(value)

    if seconds is None:
        warnings.append(f"  WARN: {tenant}: invalid {param} '{value}', using platform default")
        return PLATFORM_DEFAULTS.get(param, value), warnings

    if seconds < min_sec:
        clamped = format_duration(min_sec)
        warnings.append(f"  WARN: {tenant}: {param} '{value}' below minimum ({desc}), clamped to {clamped}")
        return clamped, warnings

    if seconds > max_sec:
        clamped = format_duration(max_sec)
        warnings.append(f"  WARN: {tenant}: {param} '{value}' above maximum ({desc}), clamped to {clamped}")
        return clamped, warnings

    return value, warnings
