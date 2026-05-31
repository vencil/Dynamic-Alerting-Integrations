"""Tests for check_ksm_version_allowlist.py — ADR-024 KSM allowlist invariant.

Pinned contracts
----------------
1. **Pass**: a KSM container whose allowlist includes app.kubernetes.io/version
   (alone, alongside other pod labels, or with other resources) is compliant.
2. **Catch no-allowlist**: a KSM container with no --metric-labels-allowlist arg
   at all is flagged (the default-deployment silent-inert case).
3. **Catch partial-misconfig (Gemini Pass-4)**: an allowlist set to a DIFFERENT
   label only (pods=[app.kubernetes.io/managed-by]) is flagged — this is the
   exact gap the runtime sentinel cannot see.
4. **Live dogfood**: the committed k8s/ KSM Deployment allowlists the version
   label (gates a regression of the ADR-024 PR3b deployment fix).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_ksm_version_allowlist as lint  # noqa: E402


def _ok(args):
    return lint.ksm_allowlist_ok(args)[0]


def test_version_allowlisted_alone():
    assert _ok(["--metric-labels-allowlist=pods=[app.kubernetes.io/version]"]) is True


def test_version_allowlisted_among_other_pod_labels():
    assert _ok(["--metric-labels-allowlist=pods=[app.kubernetes.io/name,app.kubernetes.io/version]"]) is True


def test_version_allowlisted_with_other_resources():
    assert _ok(["--metric-labels-allowlist=nodes=[kubernetes.io/role],pods=[app.kubernetes.io/version]"]) is True


def test_no_allowlist_flagged():
    # Default KSM deployment — emits zero kube_pod_labels → feature inert.
    assert _ok([]) is False
    assert _ok(["--telemetry-port=8081"]) is False


def test_partial_misconfig_flagged():
    # Gemini Pass-4: allowlist set to a DIFFERENT label only. KSM emits
    # kube_pod_labels (so the runtime sentinel stays silent) but WITHOUT the
    # version key → feature inert. The static lint MUST catch this.
    assert _ok(["--metric-labels-allowlist=pods=[app.kubernetes.io/managed-by]"]) is False


def test_allowlist_without_pods_entry_flagged():
    assert _ok(["--metric-labels-allowlist=nodes=[kubernetes.io/role]"]) is False


def test_live_repo_ksm_deployment_compliant():
    repo = lint._repo_root()
    targets = lint._ksm_deployment_files(repo)
    assert targets, "expected at least one KSM Deployment manifest under k8s/"
    for path in targets:
        assert lint.check_file(path) == [], f"{path} KSM deployment missing version allowlist"
