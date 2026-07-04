"""test_machine_identity_guard.py — ADR-027 machine-identity audit baseline (Helm)

The tenant-api chart gains an OPT-IN machine-identity audit path (issue #962):
when `machineIdentity.enabled=true`, tenant-api verifies a machine caller's
projected ServiceAccount token via TokenReview and audits (log+metric) which
workload is behind each Bearer-bearing request. It is audit-only (never blocks
authz); this test guards the Helm surface of that feature.

Two layers, mirroring test_single_writer_guard.py:
  * static (no helm needed) — values.yaml carries the opt-in default
    (enabled: false, audience: tenant-api) and the ClusterRole template grants
    ONLY tokenreviews/create (never subjectaccessreviews / auth-delegator), so a
    least-privilege regression is caught even on helm-less runners;
  * render (helm-gated) — proves `--set machineIdentity.enabled=true` actually
    renders the ClusterRole + ClusterRoleBinding, flips automount on, and passes
    the two flags; that an empty audience aborts the render (ADR-027 G4 required
    gate); and that the default (opt-out) render carries none of it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/tenant-api"
_RBAC_TEMPLATE = "helm/tenant-api/templates/rbac-machine-identity.yaml"
_VALUES = "helm/tenant-api/values.yaml"
_DEPLOYMENT = "helm/tenant-api/templates/deployment.yaml"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── static layer ────────────────────────────────────────────────────────────
def test_values_carry_optout_default_and_audience(repo_root: Path):
    """values.yaml ships the audit path opt-out with a concrete audience."""
    txt = (repo_root / _VALUES).read_text(encoding="utf-8")
    assert "machineIdentity:" in txt, "values.yaml missing machineIdentity block"
    assert "audience: tenant-api" in txt, (
        "values.yaml must default machineIdentity.audience to tenant-api"
    )
    # opt-in: enabled must default to false (apiserver coupling + new ClusterRole).
    assert "enabled: false" in txt, "machineIdentity.enabled must default to false"


def test_clusterrole_is_least_privilege(repo_root: Path):
    """The ClusterRole grants ONLY tokenreviews/create — not the broader
    system:auth-delegator (which also carries subjectaccessreviews)."""
    raw = (repo_root / _RBAC_TEMPLATE).read_text(encoding="utf-8")
    # Strip `#` comment lines before the least-privilege substring checks: the
    # explanatory comments legitimately NAME subjectaccessreviews/auth-delegator
    # to say why they're excluded, and we must not false-fail on that prose. The
    # remaining directive lines are what actually grant privilege.
    directives = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    assert "kind: ClusterRole" in directives, "template must declare a ClusterRole"
    assert "tokenreviews" in directives, "ClusterRole must grant tokenreviews"
    assert '["create"]' in directives or "- create" in directives, (
        "ClusterRole must grant the create verb"
    )
    # least-privilege guards: neither SAR nor the built-in delegator role.
    assert "subjectaccessreviews" not in directives, (
        "ClusterRole must NOT grant subjectaccessreviews (over-broad)"
    )
    assert "auth-delegator" not in directives, (
        "must NOT bind system:auth-delegator (it carries subjectaccessreviews)"
    )


def test_deployment_automount_gate_includes_machine_identity(repo_root: Path):
    """The automount opt-in must fire for machineIdentity, not federation only."""
    raw = (repo_root / _DEPLOYMENT).read_text(encoding="utf-8")
    # Strip comment lines so we assert on real template directives, not prose
    # that happens to mention the flag names.
    directives = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    assert "machineIdentity.enabled" in directives, (
        "deployment must reference machineIdentity.enabled (automount + args gate)"
    )
    assert "--machine-identity-audit" in directives, (
        "deployment must pass --machine-identity-audit when enabled"
    )


# ── render layer (helm-gated) ───────────────────────────────────────────────
def _helm_template(repo_root: Path, *set_args: str):
    cmd = ["helm", "template", "t", str(repo_root / _CHART)]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


@_needs_helm
def test_render_enabled_emits_full_surface(repo_root: Path):
    res = _helm_template(repo_root, "machineIdentity.enabled=true")
    assert res.returncode == 0, f"enabled render must succeed: {res.stderr}"
    out = res.stdout
    assert "kind: ClusterRole" in out, "enabled render must emit a ClusterRole"
    assert "kind: ClusterRoleBinding" in out, (
        "enabled render must emit a ClusterRoleBinding"
    )
    assert "automountServiceAccountToken: true" in out, (
        "enabled render must flip automount on"
    )
    assert "--machine-identity-audit" in out, (
        "enabled render must pass --machine-identity-audit"
    )
    assert "--machine-identity-audience=tenant-api" in out, (
        "enabled render must pass the default audience"
    )
    # roleRef must reference the ClusterRole by the SAME dynamic (chart-ns-release)
    # name — a typo in any of the 3 name occurrences would leave the binding
    # dangling (privilege silently not granted). Assert they match structurally.
    docs = [d for d in yaml.safe_load_all(out) if d]
    cr = next(d for d in docs if d.get("kind") == "ClusterRole")
    crb = next(d for d in docs if d.get("kind") == "ClusterRoleBinding")
    assert crb["roleRef"]["name"] == cr["metadata"]["name"], (
        f"binding roleRef {crb['roleRef']['name']!r} must match ClusterRole "
        f"name {cr['metadata']['name']!r}"
    )


@_needs_helm
def test_render_empty_audience_aborts(repo_root: Path):
    """ADR-027 G4: an empty audience would accept ANY SA token → render MUST fail."""
    res = _helm_template(
        repo_root,
        "machineIdentity.enabled=true",
        "machineIdentity.audience=",
    )
    assert res.returncode != 0, "empty machineIdentity.audience must abort the render"
    assert "audience" in res.stderr.lower(), (
        f"render failure must name the audience gate: {res.stderr}"
    )


@_needs_helm
def test_render_default_omits_everything(repo_root: Path):
    """Default (opt-out, federation also off) renders none of the audit surface."""
    res = _helm_template(repo_root)
    assert res.returncode == 0, f"default render must succeed: {res.stderr}"
    out = res.stdout
    assert "kind: ClusterRole" not in out, "default render must NOT emit a ClusterRole"
    assert "kind: ClusterRoleBinding" not in out, (
        "default render must NOT emit a ClusterRoleBinding"
    )
    assert "--machine-identity-audit" not in out, (
        "default render must NOT pass --machine-identity-audit"
    )
    # federation is also false by default, so automount must not appear at all.
    assert "automountServiceAccountToken: true" not in out, (
        "default render must NOT enable token automount"
    )


@_needs_helm
def test_render_federation_only_keeps_automount_without_audit(repo_root: Path):
    """automount is an OR(federation, machineIdentity) gate. Federation-only must
    still flip automount on (unchanged by this PR) and emit NONE of the
    machine-identity surface — proving the OR change didn't disturb federation."""
    res = _helm_template(repo_root, "federation.enabled=true")
    assert res.returncode == 0, f"federation-only render must succeed: {res.stderr}"
    out = res.stdout
    assert "automountServiceAccountToken: true" in out, (
        "federation-only must still enable automount (OR-gate regression)"
    )
    assert "--machine-identity-audit" not in out, (
        "federation-only must NOT emit machine-identity flags"
    )
    assert "tokenreviews" not in out, (
        "federation-only must NOT emit the tokenreviews ClusterRole (machineIdentity-only)"
    )
