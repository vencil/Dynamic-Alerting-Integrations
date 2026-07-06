"""test_portal_relay_token_guard.py — ADR-027 D2-B O1 da-portal relay token (image + Helm)

da-portal becomes the third verified relay in tenant-api's machine-identity
audit (#962, GHSA-3g2h-rf85-5rrv L7 root-cause series): with
`portal.relayToken.enabled=true` nginx reads the audience-bound projected SA
token on EVERY /api/v1 request (njs js_set — per-request read + kubelet atomic
rotation = zero staleness) and presents it as `Authorization: Bearer`. Opt-in
(default false = byte-preserving no-op render; the feature is image-coupled —
old images fail loud at startup on js_import).

Three layers, mirroring test_human_socket_guard.py:
  * static (no helm needed) — the image and the chart carry the SAME
    relay_token.js (the helm configmap volume hides the image's conf.d, so
    dual-copy drift is the #1 failure mode), the Dockerfile ships the njs
    main config, the image-side nginx.conf wires js_set, and values default
    the flag off;
  * render off (helm-gated) — the default render keeps the raw
    $http_authorization passthrough and emits NO njs wiring / token volume;
  * render on (helm-gated) — js module shipped in the configmap (content ==
    source), $relay_auth replaces the passthrough, projected volume pinned to
    audience=tenant-api / 900s, readOnly mount on the nginx container ONLY.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/da-portal"
_VALUES = "helm/da-portal/values.yaml"
_CONFIGMAP_TPL = "helm/da-portal/templates/configmap-nginx.yaml"
_JS_IMAGE = "components/da-portal/relay_token.js"
_JS_CHART = "helm/da-portal/files/relay_token.js"
_MAIN_CONF = "components/da-portal/nginx-main.conf"
_IMAGE_CONF = "components/da-portal/nginx.conf"
_DOCKERFILE = "components/da-portal/Dockerfile"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── static layer ────────────────────────────────────────────────────────────
def test_relay_js_dual_copy_byte_identical(repo_root: Path):
    """The chart copy of relay_token.js MUST be byte-identical to the image
    copy — the configmap volume hides the image's /etc/nginx/conf.d, so a
    drifted chart copy would silently ship different token logic in k8s."""
    image_js = (repo_root / _JS_IMAGE).read_bytes()
    chart_js = (repo_root / _JS_CHART).read_bytes()
    assert image_js == chart_js, (
        f"{_JS_IMAGE} and {_JS_CHART} drifted — they must stay byte-identical"
    )


def test_dockerfile_ships_njs_main_config(repo_root: Path):
    """The image must self-carry the njs-loading main config (the stock
    nginx:alpine main config loads no dynamic modules) plus the js module."""
    dockerfile = (repo_root / _DOCKERFILE).read_text(encoding="utf-8")
    assert "nginx-main.conf /etc/nginx/nginx.conf" in dockerfile, (
        "Dockerfile must COPY nginx-main.conf to /etc/nginx/nginx.conf"
    )
    assert "relay_token.js /etc/nginx/conf.d/relay_token.js" in dockerfile, (
        "Dockerfile must COPY relay_token.js into /etc/nginx/conf.d/"
    )
    main_conf = (repo_root / _MAIN_CONF).read_text(encoding="utf-8")
    assert "load_module modules/ngx_http_js_module.so;" in main_conf, (
        "nginx-main.conf must load ngx_http_js_module (njs)"
    )


def test_image_nginx_conf_wires_relay_auth(repo_root: Path):
    """The image-side conf.d wires the per-request js_set variable and pins
    the Authorization header on the /api/v1 proxy (empty value → dropped)."""
    conf = (repo_root / _IMAGE_CONF).read_text(encoding="utf-8")
    assert "js_import relay from conf.d/relay_token.js;" in conf
    assert "js_set $relay_auth relay.relayAuth;" in conf
    assert "proxy_set_header Authorization $relay_auth;" in conf
    # The image conf never passed the raw client Authorization through — the
    # relay variable must not regress into a client passthrough.
    assert "$http_authorization" not in conf, (
        "image nginx.conf must not forward the raw client Authorization"
    )


def test_values_default_relay_token_off(repo_root: Path):
    """relayToken must default OFF: the feature is image-coupled (old images
    crash on js_import), so a chart-only upgrade must stay a no-op."""
    values = yaml.safe_load((repo_root / _VALUES).read_text(encoding="utf-8"))
    assert values["portal"]["relayToken"]["enabled"] is False, (
        "portal.relayToken.enabled must default to false"
    )


def test_configmap_template_gates_on_flag(repo_root: Path):
    """The njs wiring must be conditional on relayToken.enabled, with the raw
    passthrough surviving as the else-branch (off state unchanged)."""
    raw = (repo_root / _CONFIGMAP_TPL).read_text(encoding="utf-8")
    directives = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    assert "portal.relayToken.enabled" in directives, (
        "configmap must gate the njs wiring on portal.relayToken.enabled"
    )
    assert "proxy_set_header Authorization $relay_auth;" in directives
    assert "proxy_set_header Authorization $http_authorization;" in directives, (
        "the raw passthrough must survive as the disabled-path else-branch"
    )


# ── render layer (helm-gated) ───────────────────────────────────────────────
def _helm_template(repo_root: Path, *set_args: str):
    cmd = ["helm", "template", "t", str(repo_root / _CHART)]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _docs(stdout: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(stdout) if d]


def _deployment(docs: list[dict]) -> dict:
    return next(d for d in docs if d.get("kind") == "Deployment")


def _nginx_configmap(docs: list[dict]) -> dict:
    return next(
        d for d in docs
        if d.get("kind") == "ConfigMap"
        and d["metadata"]["name"].endswith("-nginx-config")
    )


@_needs_helm
def test_render_default_omits_relay_wiring(repo_root: Path):
    """Default (off) render: raw passthrough intact, zero njs/token surface."""
    res = _helm_template(repo_root)
    assert res.returncode == 0, f"default render must succeed: {res.stderr}"
    out = res.stdout
    assert "proxy_set_header Authorization $http_authorization;" in out, (
        "default render must keep the raw Authorization passthrough"
    )
    assert "js_import" not in out, "default render must NOT wire njs"
    assert "$relay_auth" not in out, "default render must NOT reference $relay_auth"
    assert "relay_token.js" not in out, (
        "default render must NOT ship the relay_token.js configmap key"
    )
    assert "tenant-api-token" not in out, (
        "default render must NOT mount the projected token volume"
    )


@_needs_helm
def test_render_enabled_replaces_passthrough_with_relay(repo_root: Path):
    """Enabled render: $relay_auth replaces the passthrough — the client's own
    Bearer must no longer be able to reach the header-trusting 8080."""
    res = _helm_template(repo_root, "portal.relayToken.enabled=true")
    assert res.returncode == 0, f"enabled render must succeed: {res.stderr}"
    out = res.stdout
    assert "js_import relay from conf.d/relay_token.js;" in out
    assert "js_set $relay_auth relay.relayAuth;" in out
    assert "proxy_set_header Authorization $relay_auth;" in out
    assert "$http_authorization" not in out, (
        "enabled render must fully replace the raw client passthrough"
    )


@_needs_helm
def test_render_enabled_ships_js_matching_source(repo_root: Path):
    """The rendered configmap's relay_token.js equals the chart source file
    (which the static drift test pins to the image copy)."""
    res = _helm_template(repo_root, "portal.relayToken.enabled=true")
    assert res.returncode == 0, f"enabled render must succeed: {res.stderr}"
    cm = _nginx_configmap(_docs(res.stdout))
    assert "relay_token.js" in cm["data"], (
        "enabled render must ship relay_token.js in the nginx configmap"
    )
    rendered = cm["data"]["relay_token.js"].strip()
    source = (repo_root / _JS_CHART).read_text(encoding="utf-8").strip()
    assert rendered == source, (
        "rendered relay_token.js must match helm/da-portal/files/relay_token.js"
    )


@_needs_helm
def test_render_tier1_with_relay_token_aborts(repo_root: Path):
    """relayToken on a static-only (Tier 1) portal is a misconfiguration —
    the render must fail loud instead of shipping njs wiring (that old images
    die on) plus a token with no /api/v1 proxy to ride."""
    cmd = [
        "helm", "template", "t", str(repo_root / _CHART),
        "-f", str(repo_root / _CHART / "values-tier1.yaml"),
        "--set", "portal.relayToken.enabled=true",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert res.returncode != 0, "tier1 + relayToken must abort the render"
    assert "relayToken" in res.stderr, (
        f"render failure must name relayToken: {res.stderr}"
    )


@_needs_helm
def test_render_enabled_projected_token_volume_pinned(repo_root: Path):
    """The projected volume carries the HARD audience binding + 900s TTL, and
    ONLY the nginx container mounts it (readOnly)."""
    res = _helm_template(repo_root, "portal.relayToken.enabled=true")
    assert res.returncode == 0, f"enabled render must succeed: {res.stderr}"
    dep = _deployment(_docs(res.stdout))
    pod = dep["spec"]["template"]["spec"]

    volumes = {v["name"]: v for v in pod["volumes"]}
    assert "tenant-api-token" in volumes, "projected token volume missing"
    sat = volumes["tenant-api-token"]["projected"]["sources"][0][
        "serviceAccountToken"
    ]
    assert sat["audience"] == "tenant-api", (
        "the audience MUST be tenant-api — a default-audience token would be "
        "rejected by tenant-api's TokenReview audience hard-gate"
    )
    assert sat["expirationSeconds"] == 900
    assert sat["path"] == "tenant-api-token"

    mounts_by_container = {
        c["name"]: {m["name"]: m for m in c.get("volumeMounts", [])}
        for c in pod["containers"]
    }
    nginx_mount = mounts_by_container["nginx"].get("tenant-api-token")
    assert nginx_mount is not None, "nginx must mount the token volume"
    assert nginx_mount["mountPath"] == "/var/run/secrets/tokens"
    assert nginx_mount.get("readOnly") is True, "token mount must be readOnly"
    for name, mounts in mounts_by_container.items():
        if name != "nginx":
            assert "tenant-api-token" not in mounts, (
                f"only nginx may mount the token volume; {name} must not"
            )
