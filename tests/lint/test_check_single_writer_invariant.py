"""Tests for check_single_writer_invariant.py — ADR-023 single-writer lint.

Pinned contracts
----------------
1. **replicas**: a tenant-api Deployment with replicas != 1 is flagged.
2. **strategy**: missing / non-Recreate `strategy.type` is flagged (replicaCount=1
   alone does NOT satisfy the invariant — phantom-writer window, ADR-023 §A).
3. **helm values**: replicaCount != 1 is flagged.
4. **template guard**: the layer-1 `fail` guard must be present in the chart
   template; removing it (not just the value) is caught.
5. **scope**: a non-Deployment doc is ignored (kind filter).
6. **live dogfood**: the real repo tree honors the invariant across all three
   Deployment sources (guards a regression of this very PR).
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


# ── raw Deployment core ─────────────────────────────────────────────────────
def _deploy(replicas, strategy_type="Recreate"):
    d = {"kind": "Deployment", "spec": {"replicas": replicas}}
    if strategy_type is not None:
        d["spec"]["strategy"] = {"type": strategy_type}
    return d


def test_raw_clean():
    assert lint.check_raw_deployment(_deploy(1, "Recreate")) == []


def test_raw_replicas_two_flagged():
    out = lint.check_raw_deployment(_deploy(2, "Recreate"))
    assert len(out) == 1 and "replicas" in out[0]


def test_raw_missing_strategy_flagged():
    # replicaCount=1 alone is NOT enough — the phantom-writer window.
    out = lint.check_raw_deployment(_deploy(1, strategy_type=None))
    assert len(out) == 1 and "strategy" in out[0]


def test_raw_rollingupdate_flagged():
    out = lint.check_raw_deployment(_deploy(1, "RollingUpdate"))
    assert len(out) == 1 and "Recreate" in out[0]


def test_raw_both_wrong_flags_both():
    out = lint.check_raw_deployment(_deploy(3, "RollingUpdate"))
    assert len(out) == 2


def test_non_deployment_ignored():
    assert lint.check_raw_deployment({"kind": "Service", "spec": {}}) == []
    assert lint.check_raw_deployment("not a dict") == []  # type: ignore


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
def _write_repo(tmp_path, *, replicas=1, strategy="Recreate", rc=1,
                tpl=_GOOD_TPL):
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "helm" / "tenant-api" / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "k8s" / "04-tenant-api").mkdir(parents=True, exist_ok=True)
    (tmp_path / "helm" / "tenant-api" / "values.yaml").write_text(
        f"replicaCount: {rc}\n", encoding="utf-8")
    (tmp_path / "helm" / "tenant-api" / "templates" / "deployment.yaml").write_text(
        tpl, encoding="utf-8")
    strat = f"  strategy:\n    type: {strategy}\n" if strategy else ""
    (tmp_path / "k8s" / "04-tenant-api" / "deployment.yaml").write_text(
        f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: tenant-api\n"
        f"spec:\n  replicas: {replicas}\n{strat}",
        encoding="utf-8")


def test_check_targets_clean(tmp_path):
    _write_repo(tmp_path)
    assert lint.check_targets(tmp_path) == []


def test_check_targets_catches_raw_drift(tmp_path):
    # The exact drift this PR found: raw manifest missing strategy: Recreate.
    _write_repo(tmp_path, strategy=None)
    findings = lint.check_targets(tmp_path)
    assert any("strategy" in f and "04-tenant-api" in f for f in findings)


def test_check_targets_catches_replica_bump(tmp_path):
    _write_repo(tmp_path, rc=2, replicas=2)
    findings = lint.check_targets(tmp_path)
    assert len(findings) >= 2  # both values + raw manifest


def test_main_exit_codes(tmp_path, monkeypatch):
    _write_repo(tmp_path, rc=2, replicas=2)
    monkeypatch.setattr(lint, "_THIS_DIR",
                        str(tmp_path / "scripts" / "tools" / "lint"))
    (tmp_path / "scripts" / "tools" / "lint").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv",
                        ["check_single_writer_invariant.py", "--ci"])
    assert lint.main() == 1

    _write_repo(tmp_path)  # overwrite back to clean
    assert lint.main() == 0


# ── no-HPA assertion ────────────────────────────────────────────────────────
def test_has_hpa_text():
    assert lint.has_hpa("kind: HorizontalPodAutoscaler\n") is True
    assert lint.has_hpa("kind: Deployment\n") is False


def test_no_hpa_clean(tmp_path):
    _write_repo(tmp_path)
    assert lint.find_hpa(tmp_path) == []


def test_hpa_under_chart_flagged(tmp_path):
    _write_repo(tmp_path)
    (tmp_path / "helm" / "tenant-api" / "templates" / "hpa.yaml").write_text(
        "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\n"
        "metadata:\n  name: tenant-api\n", encoding="utf-8")
    findings = lint.check_targets(tmp_path)
    assert any("HorizontalPodAutoscaler" in f and "hpa.yaml" in f for f in findings)


def test_hpa_under_raw_dir_flagged(tmp_path):
    _write_repo(tmp_path)
    (tmp_path / "k8s" / "04-tenant-api" / "hpa.yaml").write_text(
        "kind: HorizontalPodAutoscaler\n", encoding="utf-8")
    assert lint.find_hpa(tmp_path) != []


# ── live dogfood ────────────────────────────────────────────────────────────
def test_live_repo_honors_invariant():
    repo = Path(__file__).resolve().parents[2]
    assert lint.check_targets(repo) == []
