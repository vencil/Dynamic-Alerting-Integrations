"""test_tenant_api_scope_switches_removed.py — ADR-027 §6, §7, §9 item 5.

Every RBAC scope axis (metadata read, metadata write, org) is enforced and an
empty or absent _rbac.yaml denies everything; the switches that used to turn
those off are gone. A values file that still sets one must fail the render
instead of being silently ignored, and the rendered tenant-api container must
carry none of the removed variables (tenant-api would refuse to start on an
unknown TA_* anyway — cmd/server/env_check.go — but the chart must not be the
one to hand it one).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _helm_gate import helm_required
from _render_baseline import baseline_args

_REPO = Path(__file__).resolve().parents[2]
_CHART = _REPO / "helm" / "tenant-api"
_HAS_HELM = shutil.which("helm") is not None
_REQUIRE_HELM = helm_required()

#: The chart keys ADR-027 §7 removed.
REMOVED_KEYS = (
    "rbac.metadataScopeEnforce",
    "rbac.metadataWriteScopeEnforce",
    "rbac.orgScopeEnforce",
    "rbac.emptyOpen",
)

#: The tenant-api variables ADR-027 §7 removed.
REMOVED_ENV = (
    "TA_RBAC_EMPTY_OPEN",
    "TA_RBAC_METADATA_SCOPE_ENFORCE",
    "TA_RBAC_METADATA_WRITE_SCOPE_ENFORCE",
    "TA_RBAC_ORG_SCOPE_ENFORCE",
)

def _helm_or_skip() -> None:
    if not _HAS_HELM:
        if _REQUIRE_HELM:
            pytest.fail("helm is required in this environment but not on PATH")
        pytest.skip("helm CLI not on PATH")


def _render(*set_args: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(_CHART), *baseline_args(_CHART)]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


# adr-check: ADR-027/9.5b ADR-027/9.6d
def test_values_yaml_carries_no_removed_key() -> None:
    values = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
    rbac = values.get("rbac") or {}
    present = [k for k in REMOVED_KEYS if k.split(".", 1)[1] in rbac]
    assert present == [], present
    # Vacuity guard: the block this test inspects must exist.
    assert "_rbacYaml" in rbac


# adr-check: ADR-027/9.5b ADR-027/9.6d
@pytest.mark.parametrize("key", REMOVED_KEYS)
def test_a_removed_key_fails_the_render(key: str) -> None:
    _helm_or_skip()
    res = _render(f"{key}=true")
    assert res.returncode != 0, f"{key}=true rendered:\n{res.stdout[:400]}"
    assert key.split(".", 1)[1] in res.stderr, res.stderr


# adr-check: ADR-027/9.6d
def test_the_default_render_sets_no_removed_variable() -> None:
    _helm_or_skip()
    res = _render()
    assert res.returncode == 0, res.stderr
    names: list[str] = []
    for doc in yaml.safe_load_all(res.stdout):
        if not doc or doc.get("kind") != "Deployment":
            continue
        spec = doc["spec"]["template"]["spec"]
        for c in spec.get("initContainers", []) + spec["containers"]:
            names += [e["name"] for e in c.get("env", [])]
    # Vacuity guard: a render with no env at all would pass trivially.
    assert "TA_RBAC_PATH" in names, names
    assert sorted(set(names) & set(REMOVED_ENV)) == []
