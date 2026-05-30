"""Tests for scripts/tools/lint/check_doc_k8s_refs.py.

Pins the L2+L3 doc-staleness defense added after #141 Track B / TB-F4: docs
called Prometheus a StatefulSet, pointed at a non-existent
prometheus-statefulset.yaml, and ran `kubectl edit statefulset prometheus`
(NotFound vs the shipped Deployment). Covers:
  - build_workload_kind_map: filename-convention SOT, multi-resource manifest
  - check_manifest_paths: missing vs existing k8s/ refs, placeholder skip
  - check_workload_kind: wrong kind flagged, correct passes, customer-side
    (<placeholder>, operator prometheus-k8s) skipped, inline ignore
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_doc_k8s_refs.py"
_spec = importlib.util.spec_from_file_location("check_doc_k8s_refs", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_doc_k8s_refs"] = mod
_spec.loader.exec_module(mod)


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ---- build_workload_kind_map ------------------------------------------------

class TestKindMap:
    def test_filename_convention_is_sot(self, tmp_path):
        k8s = tmp_path / "k8s"
        _write(k8s / "deployment-prometheus.yaml", "kind: Deployment\n")
        # multi-resource manifest leading with a non-workload kind:
        _write(k8s / "deployment-kube-state-metrics.yaml",
               "kind: ServiceAccount\n---\nkind: Deployment\n")
        _write(k8s / "statefulset-foo.yaml", "kind: StatefulSet\n")
        m = mod.build_workload_kind_map(k8s)
        assert m["prometheus"] == "deployment"
        # filename wins over the leading ServiceAccount kind:
        assert m["kube-state-metrics"] == "deployment"
        assert m["foo"] == "statefulset"


# ---- check_manifest_paths ---------------------------------------------------

class TestManifestPaths:
    def test_flags_missing_k8s_path(self, tmp_path):
        _write(tmp_path / "k8s" / "03-monitoring" / "deployment-prometheus.yaml", "")
        doc = _write(tmp_path / "docs" / "g.md",
                     "see k8s/03-monitoring/prometheus-statefulset.yaml\n")
        issues = mod.check_manifest_paths([doc], tmp_path)
        assert len(issues) == 1
        assert issues[0].check == "k8s-manifest-path"

    def test_passes_existing_k8s_path(self, tmp_path):
        _write(tmp_path / "k8s" / "03-monitoring" / "deployment-prometheus.yaml", "")
        doc = _write(tmp_path / "docs" / "g.md",
                     "see k8s/03-monitoring/deployment-prometheus.yaml\n")
        assert mod.check_manifest_paths([doc], tmp_path) == []

    def test_skips_placeholder_line(self, tmp_path):
        doc = _write(tmp_path / "docs" / "g.md",
                     "see k8s/<ns>/whatever.yaml\n")
        assert mod.check_manifest_paths([doc], tmp_path) == []


# ---- check_workload_kind ----------------------------------------------------

KMAP = {"prometheus": "deployment"}


class TestWorkloadKind:
    def _scan(self, tmp_path, body):
        doc = _write(tmp_path / "docs" / "g.md", body)
        return mod.check_workload_kind([doc], KMAP, tmp_path)

    def test_flags_wrong_kubectl_kind(self, tmp_path):
        issues = self._scan(tmp_path,
                            "kubectl edit statefulset prometheus -n monitoring\n")
        assert len(issues) == 1
        assert issues[0].check == "k8s-workload-kind"
        assert "deployment" in issues[0].message

    def test_flags_wrong_filename_kind(self, tmp_path):
        issues = self._scan(tmp_path, "# k8s/.../prometheus-statefulset.yaml\n")
        assert len(issues) == 1

    def test_passes_correct_kind(self, tmp_path):
        body = ("kubectl edit deployment prometheus -n monitoring\n"
                "# deployment-prometheus.yaml\n")
        assert self._scan(tmp_path, body) == []

    def test_skips_customer_placeholder(self, tmp_path):
        # customer's own Prometheus, namespace placeholder → not our manifest
        body = "kubectl rollout restart statefulset prometheus -n <prom-ns>\n"
        assert self._scan(tmp_path, body) == []

    def test_skips_operator_named_workload(self, tmp_path):
        # prometheus-k8s (prometheus-operator) != our 'prometheus'
        body = "kubectl scale statefulset prometheus-k8s --replicas=0 -n monitoring\n"
        assert self._scan(tmp_path, body) == []

    def test_respects_inline_ignore(self, tmp_path):
        body = "kubectl edit statefulset prometheus  # k8s-ref-ignore: byo example\n"
        assert self._scan(tmp_path, body) == []
