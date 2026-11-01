"""test_dimensions_render.py — threshold-exporter chart `thresholdConfig.dimensions` (#3475)

The chart writes `dimensions:` into the root `_defaults.yaml`. A value of the
wrong shape — `--set thresholdConfig.dimensions.container_cpu=version` gives
a string, not a list — would reach the exporter, whose decode of the WHOLE
`_defaults.yaml` then fails and every platform default goes with it. Pins:

  * the shipped values render the registry's declaration, as a list per base;
  * the values schema refuses a non-list or a non-string label;
  * the template's own guard refuses them too (with the schema skipped),
    so the comment above it stays true.

Helm-gated like its siblings; CI installs helm via setup-helm.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/threshold-exporter"
_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")


def _render(*args: str):
    return subprocess.run(
        ["helm", "template", "t", str(_ROOT / _CHART), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def _root_defaults(stdout: str) -> dict:
    for doc in yaml.safe_load_all(stdout):
        if doc and doc.get("kind") == "ConfigMap" and "_defaults.yaml" in doc.get("data", {}):
            return yaml.safe_load(doc["data"]["_defaults.yaml"])
    raise AssertionError("no ConfigMap carrying _defaults.yaml in the render")


def test_shipped_values_render_the_registry_declaration():
    res = _render()
    assert res.returncode == 0, res.stderr
    registry = yaml.safe_load((_ROOT / "rule-packs/threshold-registry.yaml").read_text(encoding="utf-8"))
    want = {k: e["dimensions"] for k, e in registry["keys"].items() if e.get("dimensions")}
    assert _root_defaults(res.stdout)["dimensions"] == want


def test_a_list_set_on_the_command_line_renders():
    res = _render("--set", "thresholdConfig.dimensions.mysql_connections={db,instance_re}")
    assert res.returncode == 0, res.stderr
    assert _root_defaults(res.stdout)["dimensions"]["mysql_connections"] == ["db", "instance_re"]


_BAD = [
    ("--set", "thresholdConfig.dimensions.container_cpu=version"),
    ("--set-json", "thresholdConfig.dimensions.container_memory=[1]"),
]


@pytest.mark.parametrize("args", _BAD)
def test_the_schema_refuses_a_malformed_entry(args):
    res = _render(*args)
    assert res.returncode != 0 and "/thresholdConfig/dimensions/" in res.stderr, res.stderr


@pytest.mark.parametrize("args", _BAD)
def test_the_template_guard_refuses_a_malformed_entry(args):
    res = _render("--skip-schema-validation", *args)
    assert res.returncode != 0 and "thresholdConfig.dimensions." in res.stderr, res.stderr
