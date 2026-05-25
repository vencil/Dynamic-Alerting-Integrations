"""Self-test for check_dev_bypass_manifest.py (ADR-022 Layer 4).

Verifies the deploy-time guard flags the tenant-api dev-auth-bypass switch in
helm/k8s/operator manifests, allows comment mentions, and ignores other dirs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_dev_bypass_manifest as guard  # noqa: E402


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_flags_env_var_in_helm_template(tmp_path: Path):
    _write(
        tmp_path,
        "helm/tenant-api/templates/deployment.yaml",
        'env:\n  - name: TA_DEV_BYPASS_AUTH\n    value: "true"\n',
    )
    v = guard.find_violations(tmp_path)
    assert len(v) == 1
    assert "deployment.yaml" in v[0][0]


def test_flags_flag_form_in_k8s_manifest(tmp_path: Path):
    _write(tmp_path, "k8s/04-tenant-api/deploy.yaml", "args: [--dev-bypass-auth]\n")
    assert len(guard.find_violations(tmp_path)) == 1


def test_flags_in_operator_manifests(tmp_path: Path):
    _write(tmp_path, "operator-manifests/ta.yaml", "  TA_DEV_BYPASS_AUTH: yes\n")
    assert len(guard.find_violations(tmp_path)) == 1


def test_clean_manifest_passes(tmp_path: Path):
    _write(
        tmp_path,
        "helm/tenant-api/values.yaml",
        "replicaCount: 1\nimage:\n  tag: v2.8.0\n",
    )
    assert guard.find_violations(tmp_path) == []


def test_comment_mention_allowed(tmp_path: Path):
    _write(
        tmp_path,
        "helm/tenant-api/values.yaml",
        "# NEVER set TA_DEV_BYPASS_AUTH here — local dev only\nreplicaCount: 1\n",
    )
    assert guard.find_violations(tmp_path) == []


def test_ignores_non_scanned_dirs(tmp_path: Path):
    _write(tmp_path, "docs/foo.yaml", "name: TA_DEV_BYPASS_AUTH\n")
    assert guard.find_violations(tmp_path) == []


# ---------------------------------------------------------------------------
# main() exit-code contract. Both the pre-commit hook and the CI Lint job
# invoke this script with --ci, so the HARD block depends on: clean => 0,
# findings under --ci => 1. Without --ci it stays advisory (0) so a bare
# local run never blocks unexpectedly.
# ---------------------------------------------------------------------------
def test_main_clean_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(guard, "find_violations", lambda: [])
    monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
    assert guard.main() == 0
    assert "OK" in capsys.readouterr().out


def test_main_ci_blocks_on_violation(monkeypatch, capsys):
    monkeypatch.setattr(
        guard,
        "find_violations",
        lambda: [("helm/tenant-api/templates/deployment.yaml", 3, "TA_DEV_BYPASS_AUTH")],
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
    assert guard.main() == 1
    out = capsys.readouterr().out
    assert "Layer 4" in out
    assert "deployment.yaml" in out


def test_main_without_ci_is_advisory(monkeypatch):
    monkeypatch.setattr(
        guard,
        "find_violations",
        lambda: [("k8s/04-tenant-api/deploy.yaml", 1, "--dev-bypass-auth")],
    )
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert guard.main() == 0
