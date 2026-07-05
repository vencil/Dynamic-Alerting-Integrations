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
# PSS namespace-label rule (#1018) — informational, WARN-only
# ---------------------------------------------------------------------------
def _ns_doc(name, labels):
    return {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": name, "labels": labels}}


class TestPssLabelRule:
    def test_missing_both_labels_flagged(self):
        """Doc-level positive canary: an unlabeled Namespace yields 2 findings."""
        out = k8s.pss_label_findings(_ns_doc("app-x", {"purpose": "x"}), "k8s/x.yaml")
        assert len(out) == 2
        assert any("pod-security.kubernetes.io/warn" in x for x in out)
        assert any("pod-security.kubernetes.io/audit" in x for x in out)

    def test_warn_audit_restricted_clean(self):
        labels = {"pod-security.kubernetes.io/warn": "restricted",
                  "pod-security.kubernetes.io/audit": "restricted"}
        assert k8s.pss_label_findings(_ns_doc("app-x", labels), "k8s/x.yaml") == []

    def test_enforce_optional_but_validated(self):
        base = {"pod-security.kubernetes.io/warn": "privileged",
                "pod-security.kubernetes.io/audit": "privileged"}
        # enforce absent -> clean (phased rollout: flip is a follow-up PR)
        assert k8s.pss_label_findings(_ns_doc("v", dict(base)), "k8s/v.yaml") == []
        # enforce present + valid -> clean
        ok = dict(base, **{"pod-security.kubernetes.io/enforce": "privileged"})
        assert k8s.pss_label_findings(_ns_doc("v", ok), "k8s/v.yaml") == []
        # enforce present + bogus -> flagged
        bad = dict(base, **{"pod-security.kubernetes.io/enforce": "Restricted"})
        out = k8s.pss_label_findings(_ns_doc("v", bad), "k8s/v.yaml")
        assert len(out) == 1 and "enforce" in out[0]

    def test_invalid_level_value_flagged(self):
        labels = {"pod-security.kubernetes.io/warn": "restrictedd",
                  "pod-security.kubernetes.io/audit": "restricted"}
        out = k8s.pss_label_findings(_ns_doc("app-x", labels), "k8s/x.yaml")
        assert len(out) == 1 and "not a valid PSS level" in out[0]

    def test_non_namespace_docs_ignored(self):
        assert k8s.pss_label_findings({"kind": "Deployment", "metadata": {}}, "k8s/d.yaml") == []
        assert k8s.pss_label_findings(None, "k8s/d.yaml") == []  # empty YAML doc
        assert k8s.pss_label_findings("scalar", "k8s/d.yaml") == []

    def test_metadata_absent_namespace_still_flagged(self):
        """A structurally degenerate Namespace doc (no metadata at all) must
        still yield the 2 missing-label findings — `metadata or {}` guards the
        traversal, it must not silently pass the doc."""
        out = k8s.pss_label_findings({"apiVersion": "v1", "kind": "Namespace"}, "k8s/x.yaml")
        assert len(out) == 2
        assert all("ns/?" in x for x in out)

    def test_pyyaml_missing_is_explicit_warn_not_silent(self, monkeypatch):
        """Fail-visible contract: PyYAML absent => ONE explicit WARN note,
        never a silent empty result (a silently skipped lint is fail-open).
        sys.modules['yaml']=None makes `import yaml` raise ImportError."""
        monkeypatch.setitem(sys.modules, "yaml", None)
        out = k8s.collect_pss_findings()
        assert len(out) == 1 and "PyYAML unavailable" in out[0]

    def test_collector_positive_canary(self, tmp_path):
        """FILE-level positive canary: a real on-disk Namespace manifest
        missing the labels IS caught end-to-end through the collector."""
        bad = tmp_path / "namespace-bad.yaml"
        bad.write_text(
            "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: canary-ns\n"
            "  labels:\n    purpose: canary\n", encoding="utf-8")
        out = k8s.collect_pss_findings([bad])
        assert len(out) == 2
        assert all("canary-ns" in x for x in out)

    def test_collector_multi_doc_and_parse_error_soft(self, tmp_path):
        # multi-doc file: only the Namespace doc is evaluated
        multi = tmp_path / "multi.yaml"
        multi.write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\n---\n"
            "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: n2\n",
            encoding="utf-8")
        out = k8s.collect_pss_findings([multi])
        assert len(out) == 2 and all("ns/n2" in x for x in out)
        # unparseable file degrades to an explicit note, never raises
        broken = tmp_path / "broken.yaml"
        broken.write_text("kind: Namespace\nmetadata: [unclosed", encoding="utf-8")
        out = k8s.collect_pss_findings([broken])
        assert len(out) == 1 and "skipped this file" in out[0]

    def test_misencoded_manifest_degrades_to_warn_not_crash(self, tmp_path):
        # invalid UTF-8 raises UnicodeDecodeError (a ValueError subclass, NOT
        # OSError/YAMLError) on read_text; the L4 contract requires it degrade
        # to an explicit WARN, never crash the whole run (CodeRabbit #1027).
        bad = tmp_path / "misencoded.yaml"
        bad.write_bytes(b"\xff\xfe kind: Namespace\n")
        out = k8s.collect_pss_findings([bad])
        assert len(out) == 1 and "skipped this file" in out[0]
        assert "UnicodeDecodeError" in out[0]

    def test_repo_namespaces_all_labeled(self):
        """Live baseline: every Namespace manifest under k8s/ ships labeled
        (db-a / db-b / monitoring / tenant-api warn+audit=restricted; the
        vector carve-out enforce+warn+audit=privileged).

        NB enforcement topology: the L4 pre-commit hook is MANUAL-stage (needs
        the kube-linter engine), so on a hookless local commit the PSS rule
        never runs there — THIS pytest (Python Tests CI, every PR) is the
        always-on guard that keeps the repo's own namespaces labeled; the CI
        SAST job additionally surfaces the WARN lines in its log."""
        assert k8s.collect_pss_findings() == []

    def test_pss_findings_are_warn_never_block(self, monkeypatch):
        """Contract: the rule is informational — findings land in WARN and
        --ci still exits 0 (in-flight PRs must not be retro-gated)."""
        _stub_engine(monkeypatch, [])
        monkeypatch.setattr(k8s, "collect_pss_findings",
                            lambda files=None: ["[pss] k8s/x.yaml ns/x: missing ..."])
        f = k8s.collect_findings(strict=True)
        assert any("[pss]" in x for x in f["WARN"])
        assert f["BLOCK"] == []
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert k8s.main() == 0


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

    def test_kube_linter_unparseable_is_caller_error(self, monkeypatch):
        # kube-linter failing/unparseable is "couldn't run the check" =>
        # caller-error (2), NOT a non-compliant-manifest finding (1).
        # (#452 / CodeRabbit #737)
        _stub_engine(monkeypatch, None)  # kube_linter_lint_dir -> None
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert k8s.main() == 2


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
