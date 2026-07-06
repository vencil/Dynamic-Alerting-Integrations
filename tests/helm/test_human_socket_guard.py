"""test_human_socket_guard.py — ADR-027 D2-B human-plane Unix socket (Helm)

The tenant-api chart gains an OPT-IN human-plane Unix socket (issue #962,
GHSA-3g2h-rf85-5rrv L7 root-cause series): when `humanSocket.enabled=true`,
tenant-api serves the same router on a pod-internal Unix socket and the
oauth2-proxy sidecar points its --upstream there instead of the network 8080
plane. It is opt-in (default false = safe no-op); this test guards the Helm
surface of that feature plus the da-portal netpol allow entry it depends on.

Two layers, mirroring test_machine_identity_guard.py:
  * static (no helm needed) — values.yaml carries the opt-in default
    (humanSocket.enabled: false, path set) and the da-portal netpol selector;
  * render (helm-gated) — proves the default (off) render stays free of the
    socket wiring (upstream stays http, no volume/flag), the enabled render
    switches upstream to unix:// and mounts the socket volume into BOTH
    containers with NO per-container runAsUser (uid alignment), that the netpol
    always admits da-portal as a fourth pod-scoped entry, and that an empty
    daPortal selector aborts the render (fail-closed guard extension).
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
def test_values_carry_optout_default_and_path(repo_root: Path):
    """values.yaml ships humanSocket opt-out with a concrete socket path."""
    txt = (repo_root / _VALUES).read_text(encoding="utf-8")
    assert "humanSocket:" in txt, "values.yaml missing humanSocket block"
    assert "path: /run/ta-human/human.sock" in txt, (
        "values.yaml must default humanSocket.path to /run/ta-human/human.sock"
    )
    # opt-in: enabled must default to false (image-flag coupling; safe no-op).
    assert "enabled: false" in txt, "humanSocket.enabled must default to false"


def test_values_carry_daportal_netpol_selector(repo_root: Path):
    """The da-portal port-8080 allow selector is present (ADR-027 D2-B O1)."""
    txt = (repo_root / _VALUES).read_text(encoding="utf-8")
    assert "daPortal:" in txt, "values.yaml missing internalPortAllow.daPortal"
    assert "app.kubernetes.io/name: da-portal" in txt, (
        "daPortal selector must target the da-portal pod label"
    )


def test_deployment_gates_socket_on_flag(repo_root: Path):
    """The socket wiring must be gated on humanSocket.enabled, and the upstream
    switch must be present as a conditional (not an unconditional edit)."""
    raw = (repo_root / _DEPLOYMENT).read_text(encoding="utf-8")
    directives = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    assert "humanSocket.enabled" in directives, (
        "deployment must gate the socket wiring on humanSocket.enabled"
    )
    assert "--human-socket=" in directives, (
        "deployment must pass --human-socket when enabled"
    )
    assert "unix://" in directives, (
        "deployment must switch the oauth2-proxy upstream to unix:// when enabled"
    )
    # the http upstream must survive as the else-branch (off state unchanged).
    assert "http://localhost:8080" in directives, (
        "deployment must keep the http upstream for the disabled (default) path"
    )


# ── render layer (helm-gated) ───────────────────────────────────────────────
def _helm_template(repo_root: Path, *set_args: str, template: str | None = None):
    cmd = ["helm", "template", "t", str(repo_root / _CHART)]
    if template:
        cmd += ["-s", template]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


@_needs_helm
def test_render_default_omits_socket_wiring(repo_root: Path):
    """Default (opt-out) render: upstream stays http, no socket flag/volume."""
    res = _helm_template(repo_root)
    assert res.returncode == 0, f"default render must succeed: {res.stderr}"
    out = res.stdout
    assert "--upstream=http://localhost:8080" in out, (
        "default render must keep the http upstream"
    )
    assert "unix://" not in out, "default render must NOT emit a unix:// upstream"
    assert "--human-socket=" not in out, (
        "default render must NOT pass --human-socket"
    )
    assert "name: human-sock" not in out, (
        "default render must NOT mount the human-sock volume"
    )


@_needs_helm
def test_render_enabled_switches_upstream_and_mounts_socket(repo_root: Path):
    """Enabled render: unix:// upstream, --human-socket flag, and the human-sock
    volume mounted into BOTH the tenant-api and oauth2-proxy containers."""
    res = _helm_template(repo_root, "humanSocket.enabled=true")
    assert res.returncode == 0, f"enabled render must succeed: {res.stderr}"
    out = res.stdout
    assert "--upstream=unix:///run/ta-human/human.sock" in out, (
        "enabled render must point oauth2-proxy at the unix socket"
    )
    assert "http://localhost:8080" not in out, (
        "enabled render must NOT leave the http upstream behind (else both would render)"
    )
    assert "--human-socket=/run/ta-human/human.sock" in out, (
        "enabled render must pass --human-socket to tenant-api"
    )
    # the volume is mounted into both containers (2 mounts) + defined once (1).
    assert out.count("name: human-sock") == 3, (
        "human-sock must appear 3× (tenant-api mount + oauth2-proxy mount + volume def); "
        f"got {out.count('name: human-sock')}"
    )


@_needs_helm
def test_render_enabled_no_per_container_runasuser(repo_root: Path):
    """uid alignment (§2.5 / R2): both containers must inherit the pod-level
    runAsUser 65534 — a per-container runAsUser would break the 0660 socket dial.
    So the ONLY runAsUser in the render is the pod securityContext one."""
    res = _helm_template(repo_root, "humanSocket.enabled=true")
    assert res.returncode == 0, f"enabled render must succeed: {res.stderr}"
    # Exactly one runAsUser line = the pod-level securityContext. Any second one
    # would be a per-container override that desyncs the socket uid.
    assert res.stdout.count("runAsUser:") == 1, (
        "enabled render must have exactly one runAsUser (pod-level 65534); a "
        "per-container runAsUser would break the shared-socket uid alignment"
    )


@_needs_helm
def test_render_netpol_admits_da_portal_as_fourth_entry(repo_root: Path):
    """The 8080 ingress rule admits exactly four pod-scoped clients, incl. da-portal."""
    res = _helm_template(repo_root, template="templates/networkpolicy.yaml")
    assert res.returncode == 0, f"netpol render must succeed: {res.stderr}"
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    netpol = next(
        d for d in docs
        if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"].endswith("-netpol")
    )
    # Collect the pod selectors on the 8080 ingress rule.
    selectors = []
    for rule in netpol["spec"]["ingress"]:
        ports = rule.get("ports", [])
        if any(p.get("port") == 8080 for p in ports):
            selectors = [f["podSelector"]["matchLabels"] for f in rule.get("from", [])]
    assert {"app.kubernetes.io/name": "da-portal"} in selectors, (
        f"8080 ingress must admit da-portal; got selectors {selectors}"
    )
    assert len(selectors) == 4, (
        f"8080 ingress must have exactly 4 pod-scoped allow entries; got {len(selectors)}"
    )


@_needs_helm
def test_render_empty_da_portal_selector_aborts(repo_root: Path):
    """An empty daPortal selector matches ALL monitoring pods → render MUST fail
    (fail-closed guard extension, GHSA-3g2h-rf85-5rrv)."""
    res = _helm_template(repo_root, "networkPolicy.internalPortAllow.daPortal=null")
    assert res.returncode != 0, "empty daPortal selector must abort the render"
    assert "daportal" in res.stderr.lower() or "da-portal" in res.stderr.lower() or "bypass" in res.stderr.lower(), (
        f"render failure must name the daPortal/bypass guard: {res.stderr}"
    )
