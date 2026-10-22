"""Tests for check_single_writer_invariant.py — ADR-023 single-writer lint.

Pinned contracts
----------------
1. **strategy**: a chart template without `strategy.type: Recreate` is flagged
   (replicaCount=1 alone does NOT satisfy the invariant — phantom-writer window,
   ADR-023 §A).
2. **helm values**: replicaCount != 1 is flagged.
3. **template guard**: the layer-1 `fail` guard must be present in the chart
   template; removing it (not just the value) is caught.
4. **autoscaler**: an HPA / KEDA manifest under the chart templates is flagged.
5. **live dogfood**: the real chart honors the invariant (guards a regression).

The raw tenant-api manifests under `k8s/` were removed in #1594; the chart is
tenant-api's only deployment source, so the raw-Deployment checks went with it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_single_writer_invariant as lint  # noqa: E402


# ── helm values core ────────────────────────────────────────────────────────
def test_values_one_clean():
    assert lint.check_helm_values({"replicaCount": 1}) == []


def test_values_two_flagged():
    out = lint.check_helm_values({"replicaCount": 2})
    assert len(out) == 1 and "replicaCount" in out[0]


def test_values_missing_flagged():
    assert lint.check_helm_values({}) != []


# ── helm template text core ─────────────────────────────────────────────────
_GOOD_TPL = """\
{{- if gt (int .Values.replicaCount) 1 }}
{{- fail "single-writer" }}
{{- end }}
spec:
  replicas: {{ .Values.replicaCount }}
  strategy:
    type: Recreate
"""


def test_template_recreate_detected():
    assert lint.template_has_recreate(_GOOD_TPL) is True
    assert lint.template_has_recreate("strategy:\n    type: RollingUpdate\n") is False


def test_template_recreate_tolerates_comment_line():
    txt = "  strategy:\n    # phantom-writer note\n    type: Recreate\n"
    assert lint.template_has_recreate(txt) is True


def test_template_guard_detected():
    assert lint.template_has_guard(_GOOD_TPL) is True
    assert lint.template_has_guard("replicas: {{ .Values.replicaCount }}\n") is False


# ── main() exit codes against a synthetic repo ──────────────────────────────
def _write_repo(tmp_path, *, rc=1, tpl=_GOOD_TPL):
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "helm" / "tenant-api" / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "helm" / "tenant-api" / "values.yaml").write_text(
        f"replicaCount: {rc}\n", encoding="utf-8")
    (tmp_path / "helm" / "tenant-api" / "templates" / "deployment.yaml").write_text(
        tpl, encoding="utf-8")


def test_check_targets_clean(tmp_path):
    _write_repo(tmp_path)
    assert lint.check_targets(tmp_path) == []


def test_check_targets_catches_template_strategy_drift(tmp_path):
    _write_repo(tmp_path, tpl=_GOOD_TPL.replace("type: Recreate", "type: RollingUpdate"))
    findings = lint.check_targets(tmp_path)
    assert any("strategy" in f and "templates/deployment.yaml" in f for f in findings)


def test_check_targets_catches_removed_guard(tmp_path):
    tpl = "\n".join(l for l in _GOOD_TPL.splitlines() if "fail" not in l) + "\n"
    _write_repo(tmp_path, tpl=tpl)
    findings = lint.check_targets(tmp_path)
    assert any("fail" in f and "guard" in f for f in findings)


def test_check_targets_catches_replica_bump(tmp_path):
    _write_repo(tmp_path, rc=2)
    findings = lint.check_targets(tmp_path)
    assert any("replicaCount=2" in f for f in findings)


def test_main_exit_codes(tmp_path, monkeypatch):
    _write_repo(tmp_path, rc=2)
    monkeypatch.setattr(lint, "_THIS_DIR",
                        str(tmp_path / "scripts" / "tools" / "lint"))
    (tmp_path / "scripts" / "tools" / "lint").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv",
                        ["check_single_writer_invariant.py", "--ci"])
    assert lint.main() == 1

    _write_repo(tmp_path)  # overwrite back to clean
    assert lint.main() == 0


# ── no-autoscaler assertion (native HPA + KEDA) ─────────────────────────────
def test_has_autoscaler_text():
    assert lint.has_autoscaler("kind: HorizontalPodAutoscaler\n") is True
    assert lint.has_autoscaler("kind: ScaledObject\n") is True       # KEDA
    assert lint.has_autoscaler("kind: ScaledJob\n") is True          # KEDA
    assert lint.has_autoscaler("kind: Deployment\n") is False


def test_no_autoscaler_clean(tmp_path):
    _write_repo(tmp_path)
    assert lint.find_autoscaler(tmp_path) == []


def test_hpa_under_chart_flagged(tmp_path):
    _write_repo(tmp_path)
    (tmp_path / "helm" / "tenant-api" / "templates" / "hpa.yaml").write_text(
        "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\n"
        "metadata:\n  name: tenant-api\n", encoding="utf-8")
    findings = lint.check_targets(tmp_path)
    assert any("autoscaler" in f and "hpa.yaml" in f for f in findings)


def test_keda_under_chart_templates_flagged(tmp_path):
    _write_repo(tmp_path)
    (tmp_path / "helm" / "tenant-api" / "templates" / "scaledjob.yaml").write_text(
        "apiVersion: keda.sh/v1alpha1\nkind: ScaledJob\n", encoding="utf-8")
    findings = lint.check_targets(tmp_path)
    assert any("autoscaler" in f and "scaledjob.yaml" in f for f in findings)


# ── live dogfood ────────────────────────────────────────────────────────────
def test_live_repo_honors_invariant():
    repo = Path(__file__).resolve().parents[2]
    assert lint.check_targets(repo) == []
