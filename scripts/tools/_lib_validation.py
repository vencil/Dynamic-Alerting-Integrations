"""Validation and parsing helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import os
from typing import Any, Optional, Union

from _lib_constants import (
    _DISABLED_VALUES,
    _DURATION_MULTIPLIERS,
    _DURATION_RE,
    GUARDRAILS,
    PLATFORM_DEFAULTS,
)


def detect_cli_lang() -> str:
    """Detect CLI language from environment variables.

    Checks in order: ``DA_LANG``, ``LC_ALL``, ``LANG``.
    Returns ``'zh'`` if any starts with ``zh``, ``'en'`` otherwise.
    """
    for var in ("DA_LANG", "LC_ALL", "LANG"):
        val: str = os.environ.get(var, "")
        if val.startswith("zh"):
            return "zh"
        if val.startswith("en"):
            return "en"
    return "en"


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


def is_disabled(value: Any) -> bool:
    """Check if *value* represents a disabled state.

    Recognises: ``disable``, ``disabled``, ``off``, ``false``
    (case-insensitive, stripped). Consistent with the Go
    ``IsDisabledValue()`` implementation.
    """
    if not value or not isinstance(value, str):
        return False
    return value.strip().lower() in _DISABLED_VALUES


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


def i18n_text(zh: str, en: str) -> str:
    """Return *zh* or *en* based on ``detect_cli_lang()`` result.

    Intended as a lightweight ``window.__t`` equivalent for Python CLI tools,
    replacing ad-hoc ``msg = zh if _LANG == 'zh' else en`` patterns.
    """
    return zh if detect_cli_lang() == "zh" else en
