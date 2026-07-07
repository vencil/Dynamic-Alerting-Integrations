"""test_identity_claim_headers_guard.py — ADR-027 / LD-6 P2 identity claim headers (Helm)

The tenant-api chart gains an OPT-IN identity claim-header seam (issue #962,
LD-6 P2): ``identity.claimHeaders`` maps claim keys to the trusted-hop HTTP
headers carrying their values (e.g. ``org=X-Auth-Request-Org``). When any pair
is set, the deployment passes a SINGLE ``--identity-claim-headers=key=Header,...``
arg with keys sorted (helm map range iterates key-sorted). P2 is carriage-only:
the binary loads the named claims onto the verified principal; no authz
behavior changes until P3 match evaluation consumes them.

Two layers, mirroring test_machine_identity_guard.py:
  * static (no helm needed) — values.yaml ships the seam OFF
    (``claimHeaders: {}``) and the deployment template carries the guarded arg
    block, so a regression is caught even on helm-less runners;
  * render (helm-gated) — proves the default render emits NO claim flag (the
    off state must stay byte-identical to pre-P2 — #1036: a stray blank line
    shifts the checksum annotation and triggers a pointless rollout) and that
    two ``--set`` pairs render as exactly ONE key-sorted argv entry.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/tenant-api"
_VALUES = "helm/tenant-api/values.yaml"
_DEPLOYMENT = "helm/tenant-api/templates/deployment.yaml"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── static layer ────────────────────────────────────────────────────────────
def test_values_ship_seam_off(repo_root: Path):
    """values.yaml carries the identity block with claimHeaders defaulting to {}
    (seam OFF = zero behavior change)."""
    values = yaml.safe_load((repo_root / _VALUES).read_text(encoding="utf-8"))
    assert "identity" in values, "values.yaml missing top-level identity block"
    assert values["identity"]["claimHeaders"] == {}, (
        "identity.claimHeaders must default to {} (seam OFF; any default pair "
        "would change runtime behavior on upgrade)"
    )


def test_deployment_carries_guarded_claim_arg_block(repo_root: Path):
    """The deployment template gates the claim-header arg on identity.claimHeaders."""
    raw = (repo_root / _DEPLOYMENT).read_text(encoding="utf-8")
    # Strip comment lines so we assert on real template directives, not prose
    # that happens to mention the flag names.
    directives = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    assert "identity.claimHeaders" in directives, (
        "deployment must gate the claim-header arg on .Values.identity.claimHeaders"
    )
    assert "--identity-claim-headers=" in directives, (
        "deployment must pass --identity-claim-headers when claimHeaders is set"
    )


# ── render layer (helm-gated) ───────────────────────────────────────────────
def _helm_template(repo_root: Path, *set_args: str):
    cmd = ["helm", "template", "t", str(repo_root / _CHART)]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


@_needs_helm
def test_render_default_omits_claim_flag(repo_root: Path):
    """Default ({}) render must NOT emit the flag anywhere — the off state stays
    byte-identical to pre-P2 (the full-baseline diff is checked at integration;
    this pins the flag's absence)."""
    res = _helm_template(repo_root)
    assert res.returncode == 0, f"default render must succeed: {res.stderr}"
    assert "identity-claim-headers" not in res.stdout, (
        "default render leaked --identity-claim-headers (seam must be OFF)"
    )


@_needs_helm
def test_render_two_pairs_emit_single_sorted_arg(repo_root: Path):
    """Two --set pairs render as exactly ONE argv entry with keys in sorted
    (alphabetical) order — org before region, deterministically."""
    res = _helm_template(
        repo_root,
        "identity.claimHeaders.org=X-Auth-Request-Org",
        "identity.claimHeaders.region=X-R",
    )
    assert res.returncode == 0, f"claim-header render must succeed: {res.stderr}"
    out = res.stdout
    expected = "--identity-claim-headers=org=X-Auth-Request-Org,region=X-R"
    assert out.count("--identity-claim-headers") == 1, (
        "the claim headers must render as a SINGLE flag occurrence"
    )
    assert expected in out, f"expected key-sorted single arg {expected!r} in render"
    # Structural proof: the tenant-api container carries it as ONE argv entry
    # (a substring match alone could pass on a broken multi-line render).
    docs = [d for d in yaml.safe_load_all(out) if d]
    dep = next(d for d in docs if d.get("kind") == "Deployment")
    ta = next(
        c
        for c in dep["spec"]["template"]["spec"]["containers"]
        if c["name"] == "tenant-api"
    )
    claim_args = [a for a in ta["args"] if a.startswith("--identity-claim-headers")]
    assert claim_args == [expected], (
        f"tenant-api argv must carry exactly [{expected!r}], got {claim_args!r}"
    )
