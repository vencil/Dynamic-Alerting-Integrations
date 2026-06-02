"""Tests for check_iac_helm.py — Container SAST Layer 2 (#448 / TRK-312).

Pinned contracts (exercised without invoking helm / kube-linter / docker —
engine integration is covered by the CI "Container SAST L2 (Helm)" job):

1. **Severity classification**: privileged/host-*/docker-sock => CRITICAL;
   run-as-non-root / no-read-only-root-fs / unset-*-requirements /
   capabilities-add => HIGH; everything else => LOW.
2. **Mode A source scan**: ALLOW_EMPTY_PASSWORD / ALLOW_EMPTY_* / INSECURE_*
   true/yes patterns flagged; benign lines not.
3. **capabilities.add detection** (rendered YAML): inline + block form found;
   drop-only / empty-add not.
4. **Central EXEMPTIONS**: registered (chart,check) HIGH => baseline-WARN;
   unregistered HIGH => BLOCK; CRITICAL => BLOCK regardless.
5. **main() exit codes**: 0 clean / 1 block (--ci) / 3 engines-required-missing.
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_iac_helm as helm  # noqa: E402


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------
class TestClassify:
    @pytest.mark.parametrize("check", [
        "privileged-container", "privilege-escalation-container",
        "host-network", "host-pid", "host-ipc", "docker-sock",
    ])
    def test_critical(self, check):
        assert helm.classify_check(check) == "CRITICAL"

    @pytest.mark.parametrize("check", [
        "run-as-non-root", "no-read-only-root-fs",
        "unset-cpu-requirements", "unset-memory-requirements",
        "capabilities-add",
    ])
    def test_high(self, check):
        assert helm.classify_check(check) == "HIGH"

    @pytest.mark.parametrize("check", [
        "pdb-unhealthy-pod-eviction-policy", "dangling-service",
        "deprecated-service-account-field", "anything-else",
    ])
    def test_low(self, check):
        assert helm.classify_check(check) == "LOW"


# ---------------------------------------------------------------------------
# Mode A — dangerous source patterns
# ---------------------------------------------------------------------------
class TestModeA:
    @pytest.mark.parametrize("line", [
        'MARIADB_ALLOW_EMPTY_ROOT_PASSWORD: "yes"',
        '  ALLOW_EMPTY_PASSWORD: "yes"',
        'FOO_ALLOW_EMPTY_BAR: "true"',
        'INSECURE_SKIP_VERIFY: "true"',
        '  INSECURE_TLS: true',
    ])
    def test_flagged(self, line):
        assert helm.scan_source_line(line) != []

    @pytest.mark.parametrize("line", [
        'password: "hunter2"',
        'ALLOW_EMPTY_PASSWORD: "no"',
        'name: my-secure-app',
        'INSECURE_NOTES: "this is a doc string"',  # not =true
        '# comment about ALLOW_EMPTY_PASSWORD',     # not an assignment value
    ])
    def test_not_flagged(self, line):
        assert helm.scan_source_line(line) == []


# ---------------------------------------------------------------------------
# capabilities.add detection (rendered YAML)
# ---------------------------------------------------------------------------
class TestCapabilitiesAdd:
    def test_inline_form(self):
        y = "securityContext:\n  capabilities:\n    drop: [ALL]\n    add: [\"DAC_READ_SEARCH\"]\n"
        out = helm.find_capabilities_add(y)
        assert out and "DAC_READ_SEARCH" in out[0]

    def test_block_form(self):
        y = "    capabilities:\n      add:\n        - NET_ADMIN\n        - SYS_TIME\n"
        out = helm.find_capabilities_add(y)
        assert out and "NET_ADMIN" in out[0]

    def test_drop_only_not_flagged(self):
        y = "    capabilities:\n      drop: [\"ALL\"]\n"
        assert helm.find_capabilities_add(y) == []

    def test_empty_add_not_flagged(self):
        y = "    capabilities:\n      add: []\n"
        assert helm.find_capabilities_add(y) == []


# ---------------------------------------------------------------------------
# Discovery + registry
# ---------------------------------------------------------------------------
class TestDiscovery:
    def test_finds_known_charts(self):
        charts = {helm.chart_name(c) for c in helm.find_charts()}
        for expected in {"mariadb-instance", "vector", "da-portal", "tenant-api"}:
            assert expected in charts

    def test_da_portal_has_tier_variants(self):
        variants = helm.values_variants("helm/da-portal")
        names = {v.rsplit("/", 1)[-1] if v else "values.yaml" for v in variants}
        assert "values.yaml" in names  # default (None)
        assert "values-tier1.yaml" in names
        assert "values-tier2.yaml" in names

    def test_single_values_chart(self):
        assert helm.values_variants("helm/mariadb-instance") == [None]

    def test_exemptions_cover_known_high(self):
        assert ("vector", "run-as-non-root") in helm.EXEMPTIONS
        assert ("vector", "capabilities-add") in helm.EXEMPTIONS
        assert ("mariadb-instance", "no-read-only-root-fs") in helm.EXEMPTIONS


# ---------------------------------------------------------------------------
# Engine location (monkeypatched)
# ---------------------------------------------------------------------------
class TestEngineLocate:
    def test_helm_binary(self, monkeypatch):
        monkeypatch.setattr(helm.shutil, "which",
                            lambda n: "/usr/bin/helm" if n == "helm" else None)
        assert helm.locate_helm() == ("binary", "/usr/bin/helm")

    def test_helm_docker_fallback(self, monkeypatch):
        monkeypatch.setattr(helm.shutil, "which",
                            lambda n: "/usr/bin/docker" if n == "docker" else None)
        assert helm.locate_helm() == ("docker", None)

    def test_none(self, monkeypatch):
        monkeypatch.setattr(helm.shutil, "which", lambda n: None)
        assert helm.locate_helm() == (None, None)
        assert helm.locate_kube_linter() == (None, None)
        assert helm.engines_available() is False


# ---------------------------------------------------------------------------
# collect_findings orchestration (helm/kube-linter stubbed)
# ---------------------------------------------------------------------------
def _stub_engines(monkeypatch, reports, rendered="kind: Deployment\n"):
    monkeypatch.setattr(helm, "helm_source_files", lambda: [])  # skip Mode A
    monkeypatch.setattr(helm, "engines_available", lambda: True)
    monkeypatch.setattr(helm, "helm_render", lambda c, v: (rendered, ""))
    monkeypatch.setattr(helm, "kube_linter_lint", lambda r: reports)


class TestCollectFindings:
    def test_critical_blocks(self, monkeypatch):
        _stub_engines(monkeypatch, [
            {"Check": "host-network", "Diagnostic": {"Message": "uses host network"}}])
        f = helm.collect_findings(["helm/anychart"], strict=True)
        assert any("host-network" in x and "CRITICAL" in x for x in f["BLOCK"])

    def test_registered_high_is_baseline(self, monkeypatch):
        _stub_engines(monkeypatch, [
            {"Check": "run-as-non-root", "Diagnostic": {"Message": "not runAsNonRoot"}}])
        f = helm.collect_findings(["helm/vector"], strict=True)
        assert any("run-as-non-root" in x and "baseline-exempt" in x for x in f["WARN"])
        assert f["BLOCK"] == []

    def test_unregistered_high_blocks(self, monkeypatch):
        _stub_engines(monkeypatch, [
            {"Check": "run-as-non-root", "Diagnostic": {"Message": "x"}}])
        f = helm.collect_findings(["helm/some-new-chart"], strict=True)
        assert any("UNREGISTERED" in x for x in f["BLOCK"])

    def test_low_is_info(self, monkeypatch):
        _stub_engines(monkeypatch, [
            {"Check": "pdb-unhealthy-pod-eviction-policy", "Diagnostic": {"Message": "x"}}])
        f = helm.collect_findings(["helm/anychart"], strict=True)
        assert any("pdb-unhealthy" in x for x in f["INFO"])
        assert f["BLOCK"] == []

    def test_capabilities_add_detected(self, monkeypatch):
        rendered = "spec:\n  capabilities:\n    add: [\"NET_ADMIN\"]\n"
        _stub_engines(monkeypatch, [], rendered=rendered)
        # unregistered chart + caps.add => BLOCK (HIGH unregistered)
        f = helm.collect_findings(["helm/newchart"], strict=True)
        assert any("capabilities-add" in x for x in f["BLOCK"])

    def test_engine_unavailable_strict_sets_sentinel(self, monkeypatch):
        monkeypatch.setattr(helm, "helm_source_files", lambda: [])
        monkeypatch.setattr(helm, "engines_available", lambda: False)
        f = helm.collect_findings(["helm/x"], strict=True)
        assert "__engine_error__" in f

    def test_engine_unavailable_lenient_warns(self, monkeypatch):
        monkeypatch.setattr(helm, "helm_source_files", lambda: [])
        monkeypatch.setattr(helm, "engines_available", lambda: False)
        f = helm.collect_findings(["helm/x"], strict=False)
        assert "__engine_error__" not in f
        assert any("Mode B skipped" in x for x in f["WARN"])


# ---------------------------------------------------------------------------
# main() exit codes
# ---------------------------------------------------------------------------
class TestMainExitCodes:
    def test_list(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["prog", "--list"])
        assert helm.main() == 0

    def test_clean_zero(self, monkeypatch):
        _stub_engines(monkeypatch, [])
        monkeypatch.setattr(helm, "find_charts", lambda: ["helm/vector"])
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert helm.main() == 0

    def test_block_one(self, monkeypatch):
        _stub_engines(monkeypatch, [
            {"Check": "privileged-container", "Diagnostic": {"Message": "priv"}}])
        monkeypatch.setattr(helm, "find_charts", lambda: ["helm/anychart"])
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert helm.main() == 1

    def test_engine_missing_three(self, monkeypatch):
        monkeypatch.setattr(helm, "helm_source_files", lambda: [])
        monkeypatch.setattr(helm, "engines_available", lambda: False)
        monkeypatch.setattr(helm, "find_charts", lambda: ["helm/x"])
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert helm.main() == 3

    def test_render_failure_is_caller_error(self, monkeypatch):
        # helm render failing is "couldn't run the check" => caller-error (2),
        # NOT a non-compliant-chart finding (1). (#452 / CodeRabbit #737)
        monkeypatch.setattr(helm, "helm_source_files", lambda: [])
        monkeypatch.setattr(helm, "engines_available", lambda: True)
        monkeypatch.setattr(helm, "helm_render", lambda c, v: (None, "boom"))
        monkeypatch.setattr(helm, "find_charts", lambda: ["helm/x"])
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert helm.main() == 2

    def test_kube_linter_unparseable_is_caller_error(self, monkeypatch):
        # kube-linter failing/unparseable is "couldn't run" => caller-error (2).
        monkeypatch.setattr(helm, "helm_source_files", lambda: [])
        monkeypatch.setattr(helm, "engines_available", lambda: True)
        monkeypatch.setattr(helm, "helm_render", lambda c, v: ("kind: x\n", ""))
        monkeypatch.setattr(helm, "kube_linter_lint", lambda r: None)
        monkeypatch.setattr(helm, "find_charts", lambda: ["helm/x"])
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert helm.main() == 2

    def test_render_failure_caller_error_wins_over_block(self, monkeypatch):
        # If BOTH a real CRITICAL finding AND a render failure are present,
        # the caller-error (couldn't fully run) wins over the violation exit.
        def _render(chart_dir, values_rel):
            return (None, "boom") if "broken" in chart_dir else ("kind: x\n", "")

        monkeypatch.setattr(helm, "helm_source_files", lambda: [])
        monkeypatch.setattr(helm, "engines_available", lambda: True)
        monkeypatch.setattr(helm, "helm_render", _render)
        monkeypatch.setattr(helm, "kube_linter_lint", lambda r: [
            {"Check": "privileged-container", "Diagnostic": {"Message": "p"}}])
        monkeypatch.setattr(helm, "find_charts",
                            lambda: ["helm/ok", "helm/broken"])
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert helm.main() == 2
