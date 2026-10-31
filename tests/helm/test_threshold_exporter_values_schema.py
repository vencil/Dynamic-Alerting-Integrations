"""test_threshold_exporter_values_schema.py — #1425 a key the chart lacks fails, not silently passes

The defect: the chart had no values.schema.json, so `helm` took any `--set`
or values-file key and ignored the ones no template reads. `init` printed
`--set thresholdConfig.existingConfigMap=threshold-config` against chart
2.9.0, which has no such key: both ConfigMaps were rendered, rc 0. Three more
keys documented in the README (`max_metrics_per_tenant`, `startupProbe`,
`rules.operator.serviceMonitor.labels`) were in the same position against
the published chart.

The schema constrains KEY SETS only (helm/tenant-api's #1594 shape), so it is
a second statement of the values.yaml key tree. Two halves keep it from
becoming a hand-kept copy that drifts:

  * parity (no helm): every key path values.yaml declares under a CLOSED
    schema object is a property there, and every property is a key in
    values.yaml — in both directions, so neither side can grow alone;
  * behaviour (helm): an unknown key fails at every closed level, while the
    documented nulls and the open maps still render.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _helm_gate import helm_required

_REPO_ROOT = Path(__file__).parent.parent.parent
_CHART = _REPO_ROOT / "helm" / "threshold-exporter"
_SCHEMA = json.loads((_CHART / "values.schema.json").read_text(encoding="utf-8"))
_VALUES = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))

# Helm's own keys for a subchart (`global`, and `enabled` from the umbrella's
# `condition: threshold-exporter.enabled` convention); values.yaml declares neither.
_HELM_ONLY = {"global", "enabled"}

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")
_REQUIRE_HELM = helm_required()


def _closed(node: dict) -> bool:
    return node.get("additionalProperties") is False


def _parity_gaps(schema: dict, values: dict, path: str = "") -> list[str]:
    """Key-set differences between `values` and every closed schema object."""
    gaps: list[str] = []
    if not _closed(schema):
        return gaps
    props = schema.get("properties", {})
    declared = set(values) if isinstance(values, dict) else set()
    for key in sorted(declared - set(props)):
        gaps.append(f"values.yaml {path}{key} is not in the schema (helm would refuse the default)")
    for key in sorted(set(props) - declared - (_HELM_ONLY if not path else set())):
        gaps.append(f"schema {path}{key} is not in values.yaml (a key no default documents)")
    for key in sorted(declared & set(props)):
        child = props[key]
        if isinstance(child, dict) and _closed(child):
            gaps.extend(_parity_gaps(child, values.get(key) or {}, f"{path}{key}."))
    return gaps


def test_schema_and_values_declare_the_same_keys() -> None:
    gaps = _parity_gaps(_SCHEMA, _VALUES)
    assert not gaps, "\n".join(gaps)


def test_the_parity_check_sees_a_key_on_either_side() -> None:
    """Counterfactual for the test above: a key added to one side only."""
    extra_value = dict(_VALUES, exporter=dict(_VALUES["exporter"], newKnob=1))
    assert _parity_gaps(_SCHEMA, extra_value) == [
        "values.yaml exporter.newKnob is not in the schema (helm would refuse the default)"]
    schema = json.loads(json.dumps(_SCHEMA))
    schema["properties"]["image"]["properties"]["digest"] = {}
    assert _parity_gaps(schema, _VALUES) == [
        "schema image.digest is not in values.yaml (a key no default documents)"]


def test_every_value_a_template_reads_is_a_schema_path() -> None:
    """A template reading a key the schema refuses could never be set."""
    import re
    read = set()
    # Every file helm renders or includes (`_helpers.tpl`, `NOTES.txt` too),
    # not only *.yaml.
    for tpl in sorted(p for p in (_CHART / "templates").rglob("*") if p.is_file()):
        text = tpl.read_text(encoding="utf-8")
        # `index .Values "x"` names a key this regex cannot see: refuse it
        # rather than let the check pass without having read it.
        assert not re.search(r"index\s+\$?\.Values\b", text), (
            f"{tpl.name} reads .Values through `index`, which this check "
            "cannot follow; spell the path with dots")
        read |= set(re.findall(r"\.Values((?:\.[A-Za-z_][A-Za-z0-9_]*)+)", text))
    missing = []
    for dotted in sorted(read):
        node = _SCHEMA
        for part in dotted.strip(".").split("."):
            if not _closed(node):
                break
            if part not in node.get("properties", {}):
                missing.append(dotted)
                break
            node = node["properties"][part]
    assert not missing, missing


def test_helm_present_when_required() -> None:
    """Fail-closed guard against silent disarmament."""
    if _REQUIRE_HELM:
        assert _HAS_HELM, (
            f"{_REQUIRE_HELM}: but no `helm` on PATH — every render assertion "
            "in this file would have skipped silently")


def _render(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "t", str(_CHART), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)


@_needs_helm
@pytest.mark.parametrize("setting,at", [
    ("bogusTop=1", "''"),
    ("image.bogus=1", "'/image'"),
    ("service.bogus=1", "'/service'"),
    ("exporter.bogus=1", "'/exporter'"),
    ("thresholdConfig.bogus=1", "'/thresholdConfig'"),
    ("rules.bogus=1", "'/rules'"),
    ("networkPolicy.bogus=1", "'/networkPolicy'"),
    # #2073 removed it: it never took effect, and now it says so.
    ("rules.operator.ruleLabels.x=1", "'/rules/operator'"),
    ("rules.operator.serviceMonitor.bogus=1", "'/rules/operator/serviceMonitor'"),
])
def test_a_key_the_chart_lacks_fails_where_it_is(setting: str, at: str) -> None:
    proc = _render("--set", setting)
    assert proc.returncode != 0, f"{setting} rendered: the schema did not close {at}"
    assert "additional properties" in proc.stderr and f"at {at}" in proc.stderr, proc.stderr


@_needs_helm
@pytest.mark.parametrize("args", [
    (),
    ("--set", "startupProbe=null"),
    ("--set", "livenessProbe.httpGet=null", "--set", "livenessProbe.exec.command={true}"),
    ("--set", "rules.mode=operator", "--set", "rules.operator.serviceMonitor.labels.release=null"),
    ("--set", "thresholdConfig.tenants.t1.mysql_connections=5"),
    ("--set", "thresholdConfig.defaults.custom_pack_key=7"),
    ("--set", "thresholdConfig.max_metrics_per_tenant=null"),
    ("--set", "thresholdConfig.existingConfigMap=foo"),
    ("--set", "service.annotations.example\\.com/x=y"),
    ("--set", "global.anything=1"),
    ("--set", "enabled=true"),
    ("--set", "networkPolicy.allowExternal=false",
     "--set", "networkPolicy.prometheusNamespace=monitoring",
     "--set", "networkPolicy.prometheusPodLabels.app\\.kubernetes\\.io/name=prometheus"),
], ids=lambda a: " ".join(a) or "defaults")
def test_documented_values_still_render(args: tuple) -> None:
    proc = _render(*args)
    assert proc.returncode == 0, proc.stderr
