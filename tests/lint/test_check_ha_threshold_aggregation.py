"""Tests for check_ha_threshold_aggregation.py — HA-max invariant lint.

Pinned contracts
----------------
1. **Detection**: `sum`/`avg`/`min`/etc applied directly to `user_threshold`
   is flagged; the offending operator is returned.
2. **max is HA-safe**: `max by(...) (user_threshold...)` → no finding.
3. **Operator-only (Gemini adversarial review)**: the `by(...)` grouping is
   NOT constrained — `max by(tenant, version, env)` passes (dimensions
   preserved); the lint only judges the aggregation operator.
4. **Scope**: only aggregations OF `user_threshold` — real-metric
   aggregation (`sum(rate(mysql_...))`) is never flagged.
5. **Live dogfood**: every committed rule pack / operator / configmap copy
   already aggregates user_threshold with max (gates a regression of the
   ADR-024 PR3-pre HA fix).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_ha_threshold_aggregation as lint  # noqa: E402


def _ops(expr: str):
    return [op for op, _excerpt in lint.nonmax_aggregations(expr)]


def test_sum_flagged():
    assert _ops('sum by(tenant) (user_threshold{metric="redis_memory", severity="warning"})') == ["sum"]


def test_max_clean():
    assert _ops('max by(tenant) (user_threshold{metric="redis_memory", severity="warning"})') == []


def test_avg_and_min_flagged():
    assert _ops('avg by(tenant) (user_threshold{metric="x"})') == ["avg"]
    assert _ops('min by(tenant) (user_threshold{metric="x"})') == ["min"]


def test_max_with_extra_dimensions_clean():
    # Gemini point: dimensions in by() must NOT cause a flag — operator only.
    assert _ops('max by(tenant, version, severity) (user_threshold{metric="cpu"})') == []
    assert _ops('max by(tenant, env) (user_threshold{metric="cpu", env="prod"})') == []


def test_sum_with_dimensions_still_flagged():
    # sum is the HA danger regardless of grouping.
    assert _ops('sum by(tenant, version) (user_threshold{metric="cpu"})') == ["sum"]


def test_inline_comment_does_not_hide_sum():
    # Gemini adversarial review: a `#` line comment between `(` and
    # user_threshold must NOT swallow the token and hide the sum (false neg).
    expr = 'sum by(tenant) (\n  # threshold value\n  user_threshold{metric="x"}\n)'
    assert _ops(expr) == ["sum"]


def test_commented_out_sum_not_flagged():
    # A fully commented-out sum line is not a live aggregation.
    expr = ('# sum by(tenant) (user_threshold{metric="x"})\n'
            'max by(tenant) (user_threshold{metric="x"})')
    assert _ops(expr) == []


def test_real_metric_aggregation_not_flagged():
    # Real metric aggregation is legitimate; only user_threshold is in scope.
    assert _ops('sum by(namespace, pod) (rate(container_cpu_usage_seconds_total[5m]))') == []
    assert _ops('sum by(tenant) (rate(mysql_connections[5m]))') == []


def test_no_user_threshold_clean():
    assert _ops('max by(tenant) (tenant:alert_threshold:foo)') == []


def test_whitespace_variants_flagged():
    # Token-adjacency whitespace must not hide a sum.
    assert _ops('sum by (tenant) ( user_threshold{metric="x"} )') == ["sum"]
    assert _ops('sum(user_threshold{metric="x"})') == ["sum"]


_RAW = """\
groups:
  - name: t-threshold-normalization
    interval: 15s
    rules:
      - record: tenant:alert_threshold:x
        expr: {op} by(tenant) (user_threshold{{metric="x", severity="warning"}})
"""
_CRD = """\
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
spec:
  groups:
    - name: t-threshold-normalization
      rules:
        - record: tenant:alert_threshold:x
          expr: {op} by(tenant) (user_threshold{{metric="x"}})
"""
_CM = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-rules-t
data:
  t-recording.yml: |
    groups:
      - name: t-threshold-normalization
        rules:
          - record: tenant:alert_threshold:x
            expr: {op} by(tenant) (user_threshold{{metric="x"}})
"""


@pytest.mark.parametrize("template", [_RAW, _CRD, _CM], ids=["raw", "crd", "configmap"])
def test_check_file_all_three_formats(tmp_path, template):
    """_iter_rule_groups must parse raw rule-pack / CRD / ConfigMap alike."""
    sum_file = tmp_path / "sum.yaml"
    sum_file.write_text(template.format(op="sum"), encoding="utf-8")
    findings = lint.check_file(sum_file)
    assert len(findings) == 1 and findings[0][1] == "sum"

    max_file = tmp_path / "max.yaml"
    max_file.write_text(template.format(op="max"), encoding="utf-8")
    assert lint.check_file(max_file) == []


def test_iter_rule_groups_non_dict_safe(tmp_path):
    """A YAML scalar / list document must not crash the parser."""
    p = tmp_path / "scalar.yaml"
    p.write_text("just a string\n", encoding="utf-8")
    assert list(lint._iter_rule_groups(p)) == []


def test_main_exit_codes(tmp_path, monkeypatch):
    """main(--ci) returns 1 on a violation, 0 when clean."""
    (tmp_path / "rule-packs").mkdir()
    (tmp_path / "operator-manifests").mkdir()
    (tmp_path / "k8s" / "03-monitoring").mkdir(parents=True)
    pack = tmp_path / "rule-packs" / "rule-pack-t.yaml"
    monkeypatch.setattr(lint, "_repo_root", lambda: tmp_path)

    pack.write_text(_RAW.format(op="sum"), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_ha_threshold_aggregation.py", "--ci"])
    assert lint.main() == 1

    pack.write_text(_RAW.format(op="max"), encoding="utf-8")
    assert lint.main() == 0


def test_live_repo_all_max():
    """Dogfood: every committed copy aggregates user_threshold with max."""
    repo = Path(__file__).resolve().parents[2]
    targets = (
        sorted((repo / "rule-packs").glob("rule-pack-*.yaml"))
        + sorted((repo / "operator-manifests").glob("da-rule-pack-*.yaml"))
        + sorted((repo / "k8s" / "03-monitoring").glob("configmap-rules-*.yaml"))
    )
    offenders = {}
    for path in targets:
        findings = lint.check_file(path)
        if findings:
            offenders[str(path.relative_to(repo))] = findings
    assert not offenders, f"non-max user_threshold aggregation(s): {offenders}"
