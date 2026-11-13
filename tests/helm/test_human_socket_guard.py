"""test_human_socket_guard.py — the API's Unix socket in the tenant-api chart (ADR-027 §2)

tenant-api serves its API only on a pod-internal Unix socket, the oauth2-proxy
sidecar's upstream; port 8080 serves only /health, /ready and /metrics, and
tenant-api refuses to start without the socket or the oauth2-proxy config file.
This test guards the Helm surface of that:

  * static (no helm needed) — the socket path is a chart constant
    (tenant-api.humanSocketPath; ADR-027 §6 removed the humanSocket key, which
    test_tenant_api_machine_identity_removed.py guards), and the oauth2-proxy
    config's api upstream is the socket (the other is the portal container);
  * render (helm-gated) — the default render points oauth2-proxy at unix:// and
    mounts the socket volume into BOTH containers with NO per-container
    runAsUser (uid alignment); turning oauth2Proxy off fails the render instead
    of shipping a pod that never starts.
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
# adr-check: ADR-027/9.3d
def test_socket_path_is_a_chart_constant(repo_root: Path):
    """The socket path is fixed by the chart, not a value (ADR-027 §6)."""
    helpers = (repo_root / "helm/tenant-api/templates/_helpers.tpl").read_text(encoding="utf-8")
    assert '{{- define "tenant-api.humanSocketPath" -}}\n/run/ta-human/human.sock\n{{- end -}}' in helpers, (
        "_helpers.tpl must define tenant-api.humanSocketPath as /run/ta-human/human.sock"
    )


# adr-check: ADR-027/9.3d
def test_deployment_wires_the_socket(repo_root: Path):
    """tenant-api gets the socket as TA_HUMAN_SOCKET, the guard refuses a render
    without oauth2-proxy, and the oauth2-proxy upstream is the socket."""
    raw = (repo_root / _DEPLOYMENT).read_text(encoding="utf-8")
    directives = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    assert "TA_HUMAN_SOCKET" in directives and "--human-socket" not in directives, (
        "deployment must pass TA_HUMAN_SOCKET (env, ADR-036 §12)"
    )
    assert 'include "tenant-api.identityPlaneGuard"' in directives, (
        "deployment must refuse a render with oauth2-proxy turned off"
    )
    # The upstream itself lives in the oauth2-proxy config file (ADR-027
    # §10, _helpers.tpl tenant-api.oauth2ProxyConfig); the render tests below
    # read it from there.
    helpers = (repo_root / "helm/tenant-api/templates/_helpers.tpl").read_text(encoding="utf-8")
    assert 'printf "unix://%s" (include "tenant-api.humanSocketPath" .)' in helpers, (
        "the oauth2-proxy config's upstream must be the unix socket"
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


def _pod_specs(out: str):
    """(workload name, pod spec) for every workload kind in a render."""
    for d in yaml.safe_load_all(out):
        if not d:
            continue
        spec = d.get("spec") or {}
        if d.get("kind") == "CronJob":
            spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
        pod = (spec.get("template") or {}).get("spec")
        if pod:
            yield d["metadata"]["name"], pod


def _socket_mounters(out: str) -> list[str]:
    """`workload/container` for every container (init containers included) in
    every workload that mounts the human-sock volume."""
    found = []
    for workload, pod in _pod_specs(out):
        for c in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            if any(m.get("name") == "human-sock" for m in c.get("volumeMounts") or []):
                found.append(f"{workload}/{c['name']}")
    return sorted(found)


def _helm_template(repo_root: Path, *set_args: str, template: str | None = None):
    cmd = ["helm", "template", "t", str(repo_root / _CHART), *baseline_args(_CHART)]
    if template:
        cmd += ["-s", template]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


# adr-check: ADR-027/9.3d
@_needs_helm
def test_render_without_proxy_fails(repo_root: Path):
    """tenant-api refuses to start without the oauth2-proxy config (ADR-027
    §2), so the chart refuses the render instead of shipping that pod."""
    override = "oauth2Proxy.enabled=false"
    res = _helm_template(repo_root, override)
    assert res.returncode != 0, f"{override} must fail the render"
    assert override in res.stderr and "ADR-027" in res.stderr, (
        f"the failure must name {override} and ADR-027: {res.stderr}"
    )


# adr-check: ADR-027/9.3c ADR-027/9.3d
@_needs_helm
def test_render_default_points_upstream_at_socket(repo_root: Path):
    """Default render: unix:// upstream, TA_HUMAN_SOCKET, and the human-sock
    volume mounted into BOTH the tenant-api and oauth2-proxy containers."""
    res = _helm_template(repo_root)
    assert res.returncode == 0, f"enabled render must succeed: {res.stderr}"
    out = res.stdout
    # ADR-027 §10: /api/ → the socket; / → the portal container on loopback
    # (test_tenant_api_portal_container.py).
    assert _upstream_uris(out) == ["unix:///run/ta-human/human.sock", "http://127.0.0.1:8081"], (
        "enabled render must point oauth2-proxy's api upstream at the unix socket"
    )
    assert "http://localhost:8080" not in out, (
        "enabled render must NOT leave the http upstream behind (else both would render)"
    )
    assert _ta_env(out, "TA_HUMAN_SOCKET") == "/run/ta-human/human.sock", (
        "enabled render must pass TA_HUMAN_SOCKET to tenant-api"
    )
    # Only tenant-api and oauth2-proxy mount the socket: every container of
    # every workload is read, sidecars and init containers included.
    mounters = _socket_mounters(out)
    assert [m.split("/", 1)[1] for m in mounters] == ["oauth2-proxy", "tenant-api"], mounters
    assert len({m.split("/", 1)[0] for m in mounters}) == 1, mounters


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
