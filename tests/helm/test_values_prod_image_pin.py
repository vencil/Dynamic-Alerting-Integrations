"""#682 — each chart's values-prod.yaml image.tag must equal "v" + Chart.appVersion.

The component charts default to non-pullable image tags (threshold-exporter :dev,
tenant-api :2.7.0, da-portal :2.8.0); values-prod.yaml pins the published, v-prefixed
tag for real cluster deploys. This test keeps that pin in lockstep with Chart.appVersion
so it can't silently drift at release — an operator following the doc's
`-f values-prod.yaml` always gets a pullable image.
"""
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHARTS = ["threshold-exporter", "tenant-api", "da-portal"]


@pytest.mark.parametrize("chart", CHARTS)
def test_values_prod_exists(chart):
    assert (REPO_ROOT / "helm" / chart / "values-prod.yaml").is_file(), (
        f"helm/{chart}/values-prod.yaml is missing (the documented prod deploy "
        f"path, #682)")


@pytest.mark.parametrize("chart", CHARTS)
def test_values_prod_tag_matches_appversion(chart):
    chart_dir = REPO_ROOT / "helm" / chart
    chart_yaml = yaml.safe_load((chart_dir / "Chart.yaml").read_text(encoding="utf-8"))
    prod = yaml.safe_load((chart_dir / "values-prod.yaml").read_text(encoding="utf-8"))
    app_version = str(chart_yaml["appVersion"])
    tag = str(prod["image"]["tag"])
    assert tag == f"v{app_version}", (
        f"{chart}/values-prod.yaml image.tag={tag!r} but Chart.appVersion="
        f"{app_version!r} -> expected 'v{app_version}'. Bump values-prod.yaml in "
        f"lockstep with appVersion (#682).")
