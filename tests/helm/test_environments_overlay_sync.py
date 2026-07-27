"""test_environments_overlay_sync.py — #944/#1200 threshold-overlay drift guard

``environments/{local,ci}/threshold-exporter.yaml`` are chart *values
overlays*: their ``thresholdConfig.defaults`` mirrors
``helm/threshold-exporter/values.yaml``. Helm coalesces value maps
key-by-key, so a key present in an overlay **wins** over the chart's —
which makes a stale overlay a SILENT revert of a calibrated chart default,
not a visible conflict.

This has now bitten the same key three times:

  * #944 calibrated ``mysql_cpu`` 80→30 (host-CPU% → threads_running
    saturation) but missed ``helm/.../values.yaml``;
  * #1200 WS1a's reverse audit caught the chart copy (#1215) but missed
    these two overlays — so ``-f environments/local/threshold-exporter.yaml``
    (the very command ``scaffold_tenant.py`` prints in generated onboarding
    docs) still rendered ``mysql_cpu`` at the stale 80;
  * this test's PR fixed the overlays.

Each round fixed the copy and left the *class* unguarded. Nothing in
pre-commit, CI, or tests compared these files — verified by grepping
``environments`` across ``.pre-commit-config.yaml`` (0 hits), ``.github/``
(0 hits) and ``tests/`` (only ``test_image_tag_derivation.py``, which
checks image tag / pullPolicy only). So the guard is the fix: a 4th
divergence fails here instead of shipping.

Deliberately an EQUALITY assertion, not a subset one. A subset check
("every overlay key matches the chart") would pass for an overlay that
silently drops a key the chart later adds — and a dropped key is exactly
how ``mysql_replication_lag`` went missing from both overlays in #1215.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_VALUES = _REPO_ROOT / "helm" / "threshold-exporter" / "values.yaml"
_OVERLAYS = [
    _REPO_ROOT / "environments" / "local" / "threshold-exporter.yaml",
    _REPO_ROOT / "environments" / "ci" / "threshold-exporter.yaml",
]


def _defaults(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (doc.get("thresholdConfig") or {}).get("defaults") or {}


def test_chart_defaults_are_non_empty() -> None:
    """Fail loud if the chart shape moves — otherwise the comparisons below
    would degenerate into ``{} == {}`` and pass vacuously."""
    chart = _defaults(_CHART_VALUES)
    assert chart, f"no thresholdConfig.defaults in {_CHART_VALUES} — chart shape changed?"


@pytest.mark.parametrize("overlay", _OVERLAYS, ids=lambda p: p.parent.name)
def test_overlay_defaults_match_chart(overlay: Path) -> None:
    """Overlay ``thresholdConfig.defaults`` must equal the chart's.

    A missing key means the overlay silently inherits (fine today, a
    documentation lie tomorrow); a differing value means the overlay
    silently REVERTS the chart default at deploy time.
    """
    chart = _defaults(_CHART_VALUES)
    over = _defaults(overlay)
    rel = overlay.relative_to(_REPO_ROOT).as_posix()

    missing = sorted(set(chart) - set(over))
    extra = sorted(set(over) - set(chart))
    differing = {k: (chart[k], over[k]) for k in set(chart) & set(over) if chart[k] != over[k]}

    assert not differing, (
        f"{rel} SILENTLY OVERRIDES chart defaults (chart value, overlay value): "
        f"{differing} — Helm coalesces maps key-by-key, so the overlay wins. "
        f"Sync it with helm/threshold-exporter/values.yaml."
    )
    assert not missing, (
        f"{rel} is missing threshold key(s) present in the chart: {missing}. "
        f"They still render via coalesce, but the overlay then misrepresents "
        f"the deployed defaults to whoever reads it."
    )
    assert not extra, (
        f"{rel} declares threshold key(s) the chart does not: {extra}. "
        f"Platform defaults belong in helm/threshold-exporter/values.yaml first."
    )
