"""test_human_socket_guard.py — the API's Unix socket in the tenant-api chart (ADR-027 §2)

tenant-api serves its API only on a pod-internal Unix socket, the oauth2-proxy
sidecar's upstream; port 8080 serves only /health, /ready and /metrics, and
tenant-api refuses to start without the socket or the oauth2-proxy config file.
This test guards the Helm surface of that:

  * static (no helm needed) — values.yaml defaults humanSocket on with a path,
    the oauth2-proxy config's upstream is the socket and nothing else, and the
    da-portal netpol selector is present;
  * render (helm-gated) — the default render points oauth2-proxy at unix:// and
    mounts the socket volume into BOTH containers with NO per-container
    runAsUser (uid alignment); turning humanSocket or oauth2Proxy off fails
    the render instead of shipping a pod that never starts; the netpol admits
    da-portal; an empty daPortal selector aborts the render.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _render_baseline import baseline_args


def _ta_env(out: str, name: str):
    """The tenant-api container's env value *name* in a render (ADR-036 §12:
    settings with a TA_* variable are env, not args); None when absent."""
    for d in yaml.safe_load_all(out):
        if d and d.get("kind") == "Deployment":
            for c in d["spec"]["template"]["spec"]["containers"]:
                if c["name"] == "tenant-api":
                    vals = [e.get("value") for e in c.get("env") or [] if e["name"] == name]
                    return vals[0] if vals else None
    return None

_CHART = "helm/tenant-api"
_VALUES = "helm/tenant-api/values.yaml"
_DEPLOYMENT = "helm/tenant-api/templates/deployment.yaml"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── static layer ────────────────────────────────────────────────────────────
def test_values_default_socket_on_with_path(repo_root: Path):
    """values.yaml defaults humanSocket on, with a concrete socket path: tenant-api
    refuses to start without the socket (ADR-027 §2)."""
    values = yaml.safe_load((repo_root / _VALUES).read_text(encoding="utf-8"))
    hs = values.get("humanSocket") or {}
    assert hs.get("enabled") is True, "humanSocket.enabled must default to true"
    assert hs.get("path") == "/run/ta-human/human.sock", (
        "values.yaml must default humanSocket.path to /run/ta-human/human.sock"
    )


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
    assert "TA_HUMAN_SOCKET" in directives and "--human-socket" not in directives, (
        "deployment must pass TA_HUMAN_SOCKET (env, ADR-036 §12)"
    )
    assert 'include "tenant-api.identityPlaneGuard"' in directives, (
        "deployment must refuse a render with the socket or oauth2-proxy turned off"
    )
    # The upstream itself lives in the oauth2-proxy config file (ADR-027
    # §10, _helpers.tpl tenant-api.oauth2ProxyConfig); the render tests below
    # read it from there.
    helpers = (repo_root / "helm/tenant-api/templates/_helpers.tpl").read_text(encoding="utf-8")
    assert 'printf "unix://%s" .Values.humanSocket.path' in helpers, (
        "the oauth2-proxy config must switch its upstream to unix:// when enabled"
    )
    assert "http://localhost" not in helpers, (
        "the oauth2-proxy config must not keep an http upstream: port 8080 has no API (ADR-027 §3)"
    )


# ── render layer (helm-gated) ───────────────────────────────────────────────
def _upstream_uris(out: str) -> list[str]:
    """The upstream URIs of the rendered oauth2-proxy config file."""
    for doc in yaml.safe_load_all(out):
        if doc and doc.get("kind") == "ConfigMap" and "-oauth2-proxy-" in doc["metadata"]["name"]:
            cfg = yaml.safe_load(doc["data"]["oauth2-proxy.yaml"])
            return [u["uri"] for u in cfg["upstreamConfig"]["upstreams"]]
    raise AssertionError("no oauth2-proxy config in the render")


def _helm_template(repo_root: Path, *set_args: str, template: str | None = None):
    cmd = ["helm", "template", "t", str(repo_root / _CHART), *baseline_args(_CHART)]
    if template:
        cmd += ["-s", template]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


@pytest.mark.parametrize("override", ["humanSocket.enabled=false", "oauth2Proxy.enabled=false"])
@_needs_helm
def test_render_without_socket_or_proxy_fails(repo_root: Path, override: str):
    """tenant-api refuses to start without the socket or the oauth2-proxy config
    (ADR-027 §2), so the chart refuses the render instead of shipping that pod."""
    res = _helm_template(repo_root, override)
    assert res.returncode != 0, f"{override} must fail the render"
    assert override in res.stderr and "ADR-027" in res.stderr, (
        f"the failure must name {override} and ADR-027: {res.stderr}"
    )


@_needs_helm
def test_render_default_points_upstream_at_socket(repo_root: Path):
    """Default render: unix:// upstream, TA_HUMAN_SOCKET, and the human-sock
    volume mounted into BOTH the tenant-api and oauth2-proxy containers."""
    res = _helm_template(repo_root)
    assert res.returncode == 0, f"enabled render must succeed: {res.stderr}"
    out = res.stdout
    assert _upstream_uris(out) == ["unix:///run/ta-human/human.sock"], (
        "enabled render must point oauth2-proxy at the unix socket"
    )
    assert "http://localhost:8080" not in out, (
        "enabled render must NOT leave the http upstream behind (else both would render)"
    )
    assert _ta_env(out, "TA_HUMAN_SOCKET") == "/run/ta-human/human.sock", (
        "enabled render must pass TA_HUMAN_SOCKET to tenant-api"
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
    res = _helm_template(repo_root)
    assert res.returncode == 0, f"default render must succeed: {res.stderr}"
    # Exactly one runAsUser line = the pod-level securityContext. Any second one
    # would be a per-container override that desyncs the socket uid.
    assert res.stdout.count("runAsUser:") == 1, (
        "enabled render must have exactly one runAsUser (pod-level 65534); a "
        "per-container runAsUser would break the shared-socket uid alignment"
    )


@_needs_helm
def test_render_netpol_admits_da_portal_as_fourth_entry(repo_root: Path):
    """The 8080 ingress rule admits exactly the pod-scoped clients listed, incl. da-portal."""
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
    # Four: Prometheus is admitted by the raw manifest's label AND a Prometheus
    # Operator's (kube-prometheus-stack), one entry each (#1595 D1b); the
    # threshold-govern CronJob's entry went with its write path (ADR-027 §4).
    assert selectors == [
        {"app": "prometheus"},
        {"app.kubernetes.io/name": "prometheus"},
        {"app.kubernetes.io/name": "recipe-preview"},
        {"app.kubernetes.io/name": "da-portal"},
    ], f"8080 ingress must admit exactly these pod-scoped clients; got {selectors}"


@_needs_helm
def test_render_empty_da_portal_selector_aborts(repo_root: Path):
    """An empty daPortal selector matches ALL monitoring pods → render MUST fail
    (fail-closed guard extension, GHSA-3g2h-rf85-5rrv)."""
    res = _helm_template(repo_root, "networkPolicy.internalPortAllow.daPortal=null")
    assert res.returncode != 0, "empty daPortal selector must abort the render"
    assert "daportal" in res.stderr.lower() or "da-portal" in res.stderr.lower() or "bypass" in res.stderr.lower(), (
        f"render failure must name the daPortal/bypass guard: {res.stderr}"
    )
