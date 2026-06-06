"""test_single_writer_guard.py — ADR-023 layer-1 Helm guard

The tenant-api chart hard-`fail`s at render time if `replicaCount > 1`, because
the write plane is a single writer with no cross-pod coordination (per-process
lock + in-memory PR tracker). This is layer 1 of ADR-023's enforcement; the
commit-time static guard is check_single_writer_invariant.py (layer 2).

Two layers, mirroring test_image_tag_derivation.py (#682):
  * static (no helm needed) — the template carries the `fail` guard and pins
    strategy: Recreate, so a regression is caught even on helm-less runners;
  * render (helm-gated) — proves `--set replicaCount=2` actually aborts the
    render and `replicaCount=1` renders a Recreate-strategy Deployment.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_CHART = "helm/tenant-api"
_TEMPLATE = "helm/tenant-api/templates/deployment.yaml"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")

_GUARD_RE = re.compile(r"if\s+gt\s+\(int\s+\.Values\.replicaCount\)\s+1")
_FAIL_RE = re.compile(r"\bfail\b")
_RECREATE_RE = re.compile(r"strategy:\s*\n(?:\s*#[^\n]*\n)*\s*type:\s*Recreate\b")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── static layer ────────────────────────────────────────────────────────────
def test_template_has_fail_guard(repo_root: Path):
    txt = (repo_root / _TEMPLATE).read_text(encoding="utf-8")
    assert _GUARD_RE.search(txt), "missing `if gt (int .Values.replicaCount) 1` guard"
    assert _FAIL_RE.search(txt), "guard must call `fail`"


def test_template_pins_recreate(repo_root: Path):
    txt = (repo_root / _TEMPLATE).read_text(encoding="utf-8")
    assert _RECREATE_RE.search(txt), "template must pin strategy: type: Recreate"


# ── render layer (helm-gated) ───────────────────────────────────────────────
def _helm_template(repo_root: Path, replica_count: int):
    return subprocess.run(
        ["helm", "template", "t", str(repo_root / _CHART),
         "--set", f"replicaCount={replica_count}"],
        capture_output=True, text=True, timeout=60,
    )


@_needs_helm
def test_render_aborts_on_multi_replica(repo_root: Path):
    res = _helm_template(repo_root, 2)
    assert res.returncode != 0, "replicaCount=2 must abort the render"
    assert "ADR-023" in res.stderr or "single-writer" in res.stderr.lower()


@_needs_helm
def test_render_ok_on_single_replica(repo_root: Path):
    res = _helm_template(repo_root, 1)
    assert res.returncode == 0, f"replicaCount=1 must render: {res.stderr}"
    assert _RECREATE_RE.search(res.stdout), "rendered Deployment must pin Recreate"
