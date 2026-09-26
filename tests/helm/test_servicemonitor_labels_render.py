"""test_servicemonitor_labels_render.py — threshold-exporter chart (#2075)

A Prometheus Operator only scrapes a ServiceMonitor whose labels match its
serviceMonitorSelector; kube-prometheus-stack's default selector matches
`release: <its Helm release name>`. The chart's ServiceMonitor had no way to
carry such a label. Pins:

  * by default → the chart's own two labels plus the same
    `release: kube-prometheus-stack` that `da-tools operator-generate` sets;
  * `rules.operator.serviceMonitor.labels` lands on the ServiceMonitor, and
    `release: null` drops the default;
  * a key the chart already sets fails the render (a duplicate YAML key would
    otherwise be silently resolved by whichever parser reads it).

Helm-gated like its siblings; CI installs helm via setup-helm.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/threshold-exporter"
_BASE = {"app": "threshold-exporter", "app.kubernetes.io/part-of": "dynamic-alerting"}
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _render(repo_root: Path, *args: str):
    return subprocess.run(
        ["helm", "template", "t", str(repo_root / _CHART), "--set", "rules.mode=operator", *args],
        capture_output=True, text=True, timeout=60,
    )


def _monitor_labels(stdout: str) -> dict:
    for doc in yaml.safe_load_all(stdout):
        if doc and doc.get("kind") == "ServiceMonitor":
            return doc["metadata"]["labels"]
    raise AssertionError("no ServiceMonitor in the operator-mode render")


def test_default_matches_the_operator_generate_default(repo_root: Path):
    # Same `release` default as `da-tools operator-generate`'s ServiceMonitor,
    # so a chart-default install is not silently left unscraped.
    res = _render(repo_root)
    assert res.returncode == 0, res.stderr
    assert _monitor_labels(res.stdout) == {**_BASE, "release": "kube-prometheus-stack"}


def test_configured_labels_land_on_the_servicemonitor(repo_root: Path):
    res = _render(repo_root, "--set", "rules.operator.serviceMonitor.labels.release=my-prom",
                  "--set", "rules.operator.serviceMonitor.labels.team=db")
    assert res.returncode == 0, res.stderr
    assert _monitor_labels(res.stdout) == {**_BASE, "release": "my-prom", "team": "db"}


def test_the_default_release_label_can_be_dropped(repo_root: Path):
    res = _render(repo_root, "--set", "rules.operator.serviceMonitor.labels.release=null")
    assert res.returncode == 0, res.stderr
    assert _monitor_labels(res.stdout) == _BASE


@pytest.mark.parametrize("key", ["app", "app\\.kubernetes\\.io/part-of"])
def test_a_label_the_chart_sets_fails_the_render(repo_root: Path, key: str):
    res = _render(repo_root, "--set", f"rules.operator.serviceMonitor.labels.{key}=x")
    assert res.returncode != 0
    assert "must not set" in res.stderr
