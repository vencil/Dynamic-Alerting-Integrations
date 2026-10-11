"""Render baseline for helm/tenant-api (#2855).

The tenant-api chart fails to render until ``writeBack.mode`` is set. Tests
that render it for another reason pass :func:`baseline_args` right after the
chart path, so their own ``--set`` / ``-f`` arguments still win.
"""
from __future__ import annotations

from pathlib import Path

_TENANT_API_BASELINE = Path(__file__).with_name("tenant_api_render_baseline.yaml")


def baseline_args(chart: str | Path) -> list[str]:
    """``["-f", <baseline>]`` for helm/tenant-api, ``[]`` for any other chart."""
    if Path(chart).parts[-2:] == ("helm", "tenant-api"):
        return ["-f", str(_TENANT_API_BASELINE)]
    return []
