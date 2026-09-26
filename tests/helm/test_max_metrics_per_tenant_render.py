"""test_max_metrics_per_tenant_render.py — threshold-exporter chart (#2028)

The exporter now honours `max_metrics_per_tenant` from the ROOT
`_defaults.yaml` in directory mode, and that file is written by this chart —
so without a chart value, Helm users still could not set the cap. Pins:

  * unset / null / 0 → the key is NOT written (the exporter's built-in 500);
  * an integer (from a values file OR `--set`, which arrive as float64 vs
    int64) is written as a TOP-LEVEL sibling of `defaults:`, never nested
    under it — nested, the exporter reads it as a threshold key and arms a
    bogus threshold for every tenant (ADR-017);
  * a non-integer fails the render instead of reaching the exporter, whose
    decode of the whole `_defaults.yaml` would fail with it.

Helm-gated like its siblings; CI installs helm via setup-helm.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/threshold-exporter"
_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _render(repo_root: Path, *args: str):
    return subprocess.run(
        ["helm", "template", "t", str(repo_root / _CHART), *args],
        capture_output=True, text=True, timeout=60,
    )


def _root_defaults(stdout: str) -> dict:
    for doc in yaml.safe_load_all(stdout):
        if doc and doc.get("kind") == "ConfigMap" and "_defaults.yaml" in doc.get("data", {}):
            return yaml.safe_load(doc["data"]["_defaults.yaml"])
    raise AssertionError("no ConfigMap carrying _defaults.yaml in the render")


def test_values_declare_the_key_unset(repo_root: Path):
    values = yaml.safe_load((repo_root / _CHART / "values.yaml").read_text(encoding="utf-8"))
    assert "max_metrics_per_tenant" in values["thresholdConfig"]
    assert values["thresholdConfig"]["max_metrics_per_tenant"] is None, (
        "default must stay unset — the exporter's built-in cap applies"
    )


@_needs_helm
@pytest.mark.parametrize("args", [(), ("--set", "thresholdConfig.max_metrics_per_tenant=0")])
def test_unset_or_zero_writes_nothing(repo_root: Path, args):
    res = _render(repo_root, *args)
    assert res.returncode == 0, res.stderr
    assert "max_metrics_per_tenant" not in _root_defaults(res.stdout)


@_needs_helm
def test_set_value_lands_top_level_as_int(repo_root: Path, tmp_path: Path):
    # Both input paths: --set (int64) and a values file (float64).
    vf = tmp_path / "v.yaml"
    vf.write_text("thresholdConfig:\n  max_metrics_per_tenant: 2000\n", encoding="utf-8")
    for args in (("--set", "thresholdConfig.max_metrics_per_tenant=2000"), ("-f", str(vf))):
        res = _render(repo_root, *args)
        assert res.returncode == 0, res.stderr
        doc = _root_defaults(res.stdout)
        assert doc["max_metrics_per_tenant"] == 2000, args
        assert isinstance(doc["max_metrics_per_tenant"], int), args
        assert "max_metrics_per_tenant" not in (doc.get("defaults") or {}), (
            "nested under defaults: the exporter would read it as a threshold key"
        )


@_needs_helm
@pytest.mark.parametrize("args", [
    ("--set", "thresholdConfig.max_metrics_per_tenant=1.5"),
    ("--set-string", "thresholdConfig.max_metrics_per_tenant=abc"),
])
def test_non_integer_fails_the_render(repo_root: Path, args):
    res = _render(repo_root, *args)
    assert res.returncode != 0
    assert "thresholdConfig.max_metrics_per_tenant must be an integer" in res.stderr
