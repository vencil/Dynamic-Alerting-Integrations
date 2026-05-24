"""Tests for check_k8s_manifests.py — Container SAST Layer 4 (#448 / TRK-314).

Pinned contracts (engine integration is covered by the CI "Container SAST L4
(raw k8s)" step; the stubbed tests below need no docker/kube-linter):

1. **normalize_relpath**: docker `/repo/k8s/...`, absolute host path, and bare
   `k8s/...` all normalise to the same `k8s/...` key the registry uses.
2. **Severity routing** (reused L2 classify): CRITICAL => BLOCK no-escape;
   registered HIGH => baseline-WARN; unregistered HIGH => BLOCK; LOW => INFO.
3. **De-dup**: two findings sharing (path, check) collapse to one entry.
4. **EXEMPTIONS**: the 1 known baseline-High (path, check) is registered
   (the maintenance-scheduler CronJob's 2 were cleared by a hardened
   securityContext — TRK-314 follow-up — and de-registered).
5. **main() exit codes**: 0 clean / 1 block (--ci) / 3 engine-required-missing.
6. **Baseline**: routing the REAL kube-linter report shape (2 findings in 1
   workload) yields 0 BLOCK / 1 baseline-High; a live engine run (when
   available) confirms the repo ships at 0 BLOCK.
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_k8s_manifests as k8s  # noqa: E402


# The REAL kube-linter report shape for the repo (2 findings, 1 workload) —
# pinned so routing is tested against ground truth without invoking the engine.
# The maintenance-scheduler CronJob's 2 findings were eliminated by a hardened
# securityContext (TRK-314 follow-up), leaving only tenant-api's pair.
_REAL_REPORTS = [
    {"Check": "no-read-only-root-fs",
     "Diagnostic": {"Message": 'container "git-clone" does not have a read-only root file system'},
     "Object": {"Metadata": {"FilePath": "/repo/k8s/04-tenant-api/deployment.yaml"}}},
    {"Check": "no-read-only-root-fs",
     "Diagnostic": {"Message": 'container "tenant-api" does not have a read-only root file system'},
     "Object": {"Metadata": {"FilePath": "/repo/k8s/04-tenant-api/deployment.yaml"}}},
]


# ---------------------------------------------------------------------------
# normalize_relpath
# ---------------------------------------------------------------------------
class TestNormalizeRelpath:
    @pytest.mark.parametrize("raw", [
        "/repo/k8s/04-tenant-api/deployment.yaml",                 # docker mount
        "/home/runner/work/repo/repo/k8s/04-tenant-api/deployment.yaml",  # abs host
        "k8s/04-tenant-api/deployment.yaml",                       # binary, cwd=repo
        "C:\\Users\\x\\repo\\k8s\\04-tenant-api\\deployment.yaml", # windows sep
    ])
    def test_all_normalise_to_repo_relative(self, raw):
        assert k8s.normalize_relpath(raw) == "k8s/04-tenant-api/deployment.yaml"

    def test_no_k8s_segment_falls_back(self):
        assert k8s.normalize_relpath("/repo/helm/x/values.yaml") == "repo/helm/x/values.yaml"

    def test_dir_named_like_k8s_not_falsely_matched(self):
        # "k8stuff" contains "k8s" but not the literal "k8s/" root segment.
        assert k8s.normalize_relpath("/srv/k8stuff/k8s/x.yaml") == "k8s/x.yaml"


# ---------------------------------------------------------------------------
# EXEMPTIONS registry
# ---------------------------------------------------------------------------
class TestExemptions:
    @pytest.mark.parametrize("relpath,check", [
        ("k8s/04-tenant-api/deployment.yaml", "no-read-only-root-fs"),
    ])
    def test_known_baseline_high_registered(self, relpath, check):
        assert k8s.is_exempt(relpath, check) is True
        assert (relpath, check) in k8s.EXEMPTIONS

    def test_unregistered_not_exempt(self):
        assert k8s.is_exempt("k8s/04-tenant-api/deployment.yaml", "run-as-non-root") is False
        assert k8s.is_exempt("k8s/new-thing.yaml", "no-read-only-root-fs") is False

    def test_maintenance_scheduler_no_longer_exempt(self):
        # Cleared by a hardened securityContext (TRK-314 follow-up); the keys
        # were removed so a regression re-introducing the finding now BLOCKS.
        sched = "k8s/03-monitoring/cronjob-maintenance-scheduler.yaml"
        assert k8s.is_exempt(sched, "run-as-non-root") is False
        assert k8s.is_exempt(sched, "no-read-only-root-fs") is False

    def test_critical_never_in_registry(self):
        for (_, check) in k8s.EXEMPTIONS:
            assert k8s.classify_check(check) == "HIGH"


# ---------------------------------------------------------------------------
# _emit routing
# ---------------------------------------------------------------------------
def _fresh():
    return {"BLOCK": [], "WARN": [], "INFO": []}


class TestEmitRouting:
    def test_critical_blocks_no_escape(self):
        f, seen = _fresh(), set()
        k8s._emit(f, "k8s/x.yaml", "host-network", "uses host net", seen)
        assert any("host-network" in x and "CRITICAL" in x for x in f["BLOCK"])

    def test_registered_high_is_baseline_warn(self):
        f, seen = _fresh(), set()
        k8s._emit(f, "k8s/04-tenant-api/deployment.yaml", "no-read-only-root-fs", "x", seen)
        assert f["BLOCK"] == []
        assert any("baseline-exempt" in x for x in f["WARN"])

    def test_unregistered_high_blocks(self):
        f, seen = _fresh(), set()
        k8s._emit(f, "k8s/04-tenant-api/deployment.yaml", "run-as-non-root", "x", seen)
        assert any("UNREGISTERED" in x for x in f["BLOCK"])

    def test_low_is_info(self):
        f, seen = _fresh(), set()
        k8s._emit(f, "k8s/x.yaml", "dangling-service", "x", seen)
        assert any("dangling-service" in x for x in f["INFO"])
        assert f["BLOCK"] == []

    def test_dedup_same_path_check(self):
        f, seen = _fresh(), set()
        # two containers, same (path, check) — collapses to one WARN
        k8s._emit(f, "k8s/04-tenant-api/deployment.yaml", "no-read-only-root-fs", "git-clone", seen)
        k8s._emit(f, "k8s/04-tenant-api/deployment.yaml", "no-read-only-root-fs", "tenant-api", seen)
        assert len(f["WARN"]) == 1


# ---------------------------------------------------------------------------
# collect_findings orchestration (engine stubbed)
# ---------------------------------------------------------------------------
def _stub_engine(monkeypatch, reports):
    monkeypatch.setattr(k8s, "manifest_root_exists", lambda: True)
    monkeypatch.setattr(k8s, "engine_available", lambda: True)
    monkeypatch.setattr(k8s, "kube_linter_lint_dir", lambda *a, **k: reports)


class TestCollectFindings:
    def test_real_report_shape_ships_at_zero(self, monkeypatch):
        """The repo's actual 2 findings (1 workload) => 0 BLOCK / 1 WARN."""
        _stub_engine(monkeypatch, _REAL_REPORTS)
        f = k8s.collect_findings(strict=True)
        assert f["BLOCK"] == []
        assert len(f["WARN"]) == 1  # tenant-api's 2 containers collapse to 1
        assert "__engine_error__" not in f

    def test_injected_critical_blocks(self, monkeypatch):
        _stub_engine(monkeypatch, _REAL_REPORTS + [
            {"Check": "privileged-container", "Diagnostic": {"Message": "priv"},
             "Object": {"Metadata": {"FilePath": "/repo/k8s/03-monitoring/deployment-prometheus.yaml"}}}])
        f = k8s.collect_findings(strict=True)
        assert any("privileged-container" in x and "CRITICAL" in x for x in f["BLOCK"])

    def test_new_unregistered_high_blocks(self, monkeypatch):
        _stub_engine(monkeypatch, [
            {"Check": "run-as-non-root", "Diagnostic": {"Message": "x"},
             "Object": {"Metadata": {"FilePath": "/repo/k8s/03-monitoring/deployment-grafana.yaml"}}}])
        f = k8s.collect_findings(strict=True)
        assert any("UNREGISTERED" in x for x in f["BLOCK"])

    def test_engine_unavailable_strict_sets_sentinel(self, monkeypatch):
        monkeypatch.setattr(k8s, "manifest_root_exists", lambda: True)
        monkeypatch.setattr(k8s, "engine_available", lambda: False)
        f = k8s.collect_findings(strict=True)
        assert "__engine_error__" in f

    def test_engine_unavailable_lenient_warns(self, monkeypatch):
        monkeypatch.setattr(k8s, "manifest_root_exists", lambda: True)
        monkeypatch.setattr(k8s, "engine_available", lambda: False)
        f = k8s.collect_findings(strict=False)
        assert "__engine_error__" not in f
        assert any("L4 skipped" in x for x in f["WARN"])

    def test_missing_manifest_root_is_idle(self, monkeypatch):
        monkeypatch.setattr(k8s, "manifest_root_exists", lambda: False)
        f = k8s.collect_findings(strict=True)
        assert f["BLOCK"] == []
        assert any("Layer 4 idle" in x for x in f["INFO"])


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
class TestDiscovery:
    def test_finds_known_manifests(self):
        rels = {p.relative_to(k8s.REPO_ROOT).as_posix() for p in k8s.find_manifest_files()}
        assert "k8s/04-tenant-api/deployment.yaml" in rels
        assert "k8s/03-monitoring/cronjob-maintenance-scheduler.yaml" in rels
        assert "k8s/03-monitoring/secret-grafana.yaml" in rels
        # nested dir (crd/) is reached by rglob
        assert any(r.startswith("k8s/crd/") for r in rels)
        # worktrees excluded
        assert not any(".claude" in r for r in rels)

    def test_root_exists(self):
        assert k8s.manifest_root_exists() is True


# ---------------------------------------------------------------------------
# main() exit codes
# ---------------------------------------------------------------------------
class TestMainExitCodes:
    def test_list(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["prog", "--list"])
        assert k8s.main() == 0

    def test_clean_zero(self, monkeypatch):
        _stub_engine(monkeypatch, _REAL_REPORTS)  # all exempt => 0 BLOCK
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert k8s.main() == 0

    def test_block_one(self, monkeypatch):
        _stub_engine(monkeypatch, [
            {"Check": "host-pid", "Diagnostic": {"Message": "host pid"},
             "Object": {"Metadata": {"FilePath": "/repo/k8s/x.yaml"}}}])
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert k8s.main() == 1

    def test_engine_missing_three(self, monkeypatch):
        monkeypatch.setattr(k8s, "manifest_root_exists", lambda: True)
        monkeypatch.setattr(k8s, "engine_available", lambda: False)
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert k8s.main() == 3


# ---------------------------------------------------------------------------
# Live baseline (engine-gated) — confirms the real repo ships at 0 BLOCK
# ---------------------------------------------------------------------------
class TestLiveBaseline:
    @pytest.mark.skipif(not k8s.engine_available(),
                        reason="kube-linter/docker unavailable — covered by CI L4 step")
    def test_repo_ships_at_zero_block(self):
        f = k8s.collect_findings(strict=True)
        assert f.get("__engine_error__") is None
        assert f["BLOCK"] == [], f"unexpected BLOCK findings: {f['BLOCK']}"
