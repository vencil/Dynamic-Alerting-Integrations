"""test_recipe_preview_container.py — tenant-api chart's recipe-preview container (#1040)

ADR-027 §5: the would-fire preview is answered by tenant-api, which authorizes
the caller on the tenant and then asks the recipe-preview service over a Unix
socket inside its own pod. The standalone recipe-preview chart is gone; this
container is the only way the service runs. Its trust boundary and its wiring
live in the tenant-api chart, so the chart is what these tests pin:

  * on by default; tenant-api is given the socket (TA_RECIPE_PREVIEW_SOCKET)
    and mounts its directory read-only. Off: no container, no env, no volume.
  * the socket directory is mounted by EXACTLY two containers — recipe-preview
    and tenant-api — out of every container the chart can render (the
    service's SO_PEERCRED check admits any same-uid peer, so the mount is the
    boundary), and neither sets its own runAsUser (one pod uid).
  * the image tag follows generator.image.tag (one `tools/v*` release builds
    both images) unless set; a digest is never inherited.
  * no network port, the ServiceAccount token shadowed, a writable /tmp on a
    read-only root.
  * the body limit is NOT configurable here: tenant-api sends at most
    recipepreview.MaxRequestBytes, which is the service's default.
  * the probe passes on the real service (which answers HTTP/1.0) and fails
    without it or on a non-200; there is no readinessProbe (a pod is Ready
    only when every container is, and preview is optional).

Helm-gated like its siblings; CI installs helm via setup-helm.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import yaml
from _helm_gate import helm_required
from _render_baseline import baseline_args

_REPO_ROOT = Path(__file__).resolve().parents[2]
# ⛔ One literal path string per chart (verify_diff.py text_map, #1313).
_CHART = _REPO_ROOT / "helm/tenant-api"
_APP = _REPO_ROOT / "components/recipe-preview/app.py"
_CLIENT_GO = _REPO_ROOT / "components/tenant-api/internal/recipepreview/client.go"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")
_REQUIRE_HELM = helm_required()  # tests/_helm_gate.py: CI or a remote session

_ON = ("gitRepoUrl=https://git.example.com/org/conf.git",)
_EVERYTHING = (*_ON, "federation.enabled=true",
               "generator.policy.existingConfigMap=da-policy")
_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount"
_SOCK_VOLUME = "recipe-preview-sock"


def test_helm_present_when_required() -> None:
    if _REQUIRE_HELM:
        assert _HAS_HELM, (
            f"{_REQUIRE_HELM}: but no `helm` on PATH — every render assertion "
            "in this file would have skipped silently")


# ── helpers ──────────────────────────────────────────────────────────────────
def _helm(*overrides: str, values: Path | None = None) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(_CHART), *baseline_args(_CHART)]
    if values is not None:
        cmd += ["-f", str(values)]
    for o in overrides:
        cmd += ["--set", o]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=90)


def _render(*overrides: str, values: Path | None = None) -> str:
    out = _helm(*overrides, values=values)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _pod(stdout: str) -> dict:
    for doc in yaml.safe_load_all(stdout):
        if doc and doc.get("kind") == "Deployment":
            return doc["spec"]["template"]["spec"]
    raise AssertionError("no Deployment in the render")


def _all_containers(pod: dict) -> list[dict]:
    return list(pod.get("initContainers") or []) + list(pod["containers"])


def _container(pod: dict, name: str) -> dict:
    found = [c for c in _all_containers(pod) if c["name"] == name]
    assert len(found) == 1, (name, [c["name"] for c in _all_containers(pod)])
    return found[0]


def _env(container: dict) -> dict:
    return {e["name"]: e.get("value") for e in container.get("env") or []}


def _mounts(container: dict, volume: str) -> list[dict]:
    return [m for m in container.get("volumeMounts") or [] if m["name"] == volume]


def _volume(pod: dict, name: str) -> dict:
    found = [v for v in pod["volumes"] if v["name"] == name]
    assert len(found) == 1, (name, [v["name"] for v in pod["volumes"]])
    return found[0]


def _values_file(tmp_path: Path, tree: dict) -> Path:
    vf = tmp_path / "v.yaml"
    vf.write_text(yaml.safe_dump(tree), encoding="utf-8")
    return vf


# ── on by default, wired to tenant-api ───────────────────────────────────────
@_needs_helm
def test_on_by_default_and_wired_to_tenant_api() -> None:
    pod = _pod(_render(*_ON))
    rp, ta = _container(pod, "recipe-preview"), _container(pod, "tenant-api")
    sock = _env(rp)["PREVIEW_SOCKET"]
    assert _env(ta)["TA_RECIPE_PREVIEW_SOCKET"] == sock
    (rm,), (tm,) = _mounts(rp, _SOCK_VOLUME), _mounts(ta, _SOCK_VOLUME)
    assert rm["mountPath"] == tm["mountPath"] == os.path.dirname(sock)
    assert tm.get("readOnly") is True and not rm.get("readOnly")
    assert _volume(pod, _SOCK_VOLUME)["emptyDir"]["medium"] == "Memory"


@_needs_helm
def test_disabled_renders_nothing_of_it() -> None:
    out = _render(*_EVERYTHING, "recipePreview.enabled=false")
    pod = _pod(out)
    assert "recipe-preview" not in [c["name"] for c in _all_containers(pod)]
    assert "TA_RECIPE_PREVIEW_SOCKET" not in _env(_container(pod, "tenant-api"))
    assert not [v for v in pod["volumes"] if v["name"].startswith("recipe-preview")]
    assert "recipe-preview" not in out.replace("recipe-preview:", "")  # no stray mount


@_needs_helm
@pytest.mark.parametrize("shape", [
    (), ("confDir.type=pvc", "gitRepoUrl="), ("confDir.type=pvc", "confDir.pvc.existingClaim=conf-repo", "gitRepoUrl="),
], ids=["clone", "pvc", "existingClaim"])
def test_only_recipe_preview_and_tenant_api_mount_the_socket_dir(shape) -> None:
    pod = _pod(_render(*_EVERYTHING, *shape))
    names = [c["name"] for c in _all_containers(pod)]
    assert len(names) >= 5, names  # the scan saw every sidecar and an init container
    mounting = sorted(c["name"] for c in _all_containers(pod) if _mounts(c, _SOCK_VOLUME))
    assert mounting == ["recipe-preview", "tenant-api"], mounting


@_needs_helm
def test_runs_as_the_pod_uid() -> None:
    """SO_PEERCRED admits only the service's own uid; tenant-api must be it."""
    pod = _pod(_render(*_ON))
    assert pod["securityContext"].get("runAsUser") is not None
    for name in ("recipe-preview", "tenant-api"):
        assert "runAsUser" not in (_container(pod, name).get("securityContext") or {}), name


@_needs_helm
def test_hardened_no_port_no_sa_token_writable_tmp() -> None:
    pod = _pod(_render(*_ON))
    rp = _container(pod, "recipe-preview")
    assert not rp.get("ports")
    sc = rp["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    shadow = [m for m in rp["volumeMounts"] if m["mountPath"] == _SA_TOKEN_PATH]
    assert len(shadow) == 1 and shadow[0]["readOnly"] is True
    assert "emptyDir" in _volume(pod, shadow[0]["name"])
    tmp = [m for m in rp["volumeMounts"] if m["mountPath"] == "/tmp"]
    assert len(tmp) == 1 and not tmp[0].get("readOnly")
    assert "emptyDir" in _volume(pod, tmp[0]["name"])


# ── image follows the tools pin ──────────────────────────────────────────────
@_needs_helm
def test_image_tag_follows_generator_tag() -> None:
    pod = _pod(_render(*_ON, "generator.image.tag=v9.8.7"))
    assert _container(pod, "recipe-preview")["image"] == "ghcr.io/vencil/recipe-preview:v9.8.7"


@_needs_helm
def test_explicit_tag_and_digest_win_and_digest_is_not_inherited() -> None:
    digest = "sha256:" + "a" * 64
    pod = _pod(_render(*_ON, f"generator.image.digest={digest}"))
    assert "@" not in _container(pod, "recipe-preview")["image"]
    pod = _pod(_render(*_ON, "recipePreview.image.tag=v1.2.3",
                       f"recipePreview.image.digest={digest}"))
    assert _container(pod, "recipe-preview")["image"] == f"ghcr.io/vencil/recipe-preview:v1.2.3@{digest}"


@_needs_helm
def test_no_tag_anywhere_fails() -> None:
    out = _helm(*_ON, "generator.image.tag=", "recipePreview.image.tag=")
    assert out.returncode != 0 and "recipePreview.image.tag" in out.stderr, out.stderr


# ── settings ─────────────────────────────────────────────────────────────────
@_needs_helm
def test_settings_reach_the_service_env(tmp_path: Path) -> None:
    vf = _values_file(tmp_path, {"recipePreview": {
        "maxConcurrency": 2.0, "rateLimitPerMin": 1000000, "queueTimeout": 2.5}})
    env = _env(_container(_pod(_render(*_ON, values=vf)), "recipe-preview"))
    # A float whole number and a large int are printed as integers (app.py
    # int()-parses them; 1e+06 would crash it at startup).
    assert env["PREVIEW_MAX_CONCURRENCY"] == "2"
    assert env["PREVIEW_RATE_LIMIT_PER_MIN"] == "1000000"
    assert env["PREVIEW_QUEUE_TIMEOUT"] == "2.5"


@_needs_helm
def test_settings_names_exist_in_the_service() -> None:
    """Every PREVIEW_* the chart sets is one app.py reads."""
    env = _env(_container(_pod(_render(*_ON)), "recipe-preview"))
    app = _APP.read_text(encoding="utf-8")
    for name in env:
        if name.startswith("PREVIEW_"):
            assert f'"{name}"' in app, name


@_needs_helm
def test_body_limit_is_not_set_so_the_service_default_matches_tenant_api() -> None:
    env = _env(_container(_pod(_render(*_ON)), "recipe-preview"))
    assert "PREVIEW_MAX_BODY_BYTES" not in env
    go = re.search(r"MaxRequestBytes\s*=\s*(\d+)\s*\*\s*(\d+)", _CLIENT_GO.read_text(encoding="utf-8"))
    py = re.search(r'"PREVIEW_MAX_BODY_BYTES",\s*str\((\d+)\s*\*\s*(\d+)\)', _APP.read_text(encoding="utf-8"))
    assert go and py
    assert int(go[1]) * int(go[2]) == int(py[1]) * int(py[2])


@_needs_helm
@pytest.mark.parametrize("override", [
    "recipePreview.maxBodyBytes=1", "recipePreview.maxConcurrency=0",
    "recipePreview.image.pullPolicy=Sometimes",
])
def test_schema_refuses(override: str) -> None:
    out = _helm(*_ON, override)
    assert out.returncode != 0, override


@_needs_helm
def test_fractional_integer_setting_fails() -> None:
    out = _helm(*_ON, "recipePreview.maxConcurrency=2.5")
    assert out.returncode != 0


# ── probes ───────────────────────────────────────────────────────────────────
_PROBES = ("startupProbe", "livenessProbe")


@_needs_helm
def test_no_readiness_probe() -> None:
    """A pod is Ready only when every container is: a readiness failure here
    would take tenant-api out of the Service over an optional feature."""
    rp = _container(_pod(_render(*_ON)), "recipe-preview")
    assert "readinessProbe" not in rp
    assert {"startupProbe", "livenessProbe"} <= set(rp)


def _probe_script(rp: dict) -> str:
    commands = [rp[name]["exec"]["command"] for name in _PROBES]
    assert commands[0] == commands[1], "the probes drifted apart"
    assert commands[0][:2] == ["python", "-c"]
    return commands[0][2]


@_needs_helm
def test_probe_dials_the_service_socket() -> None:
    rp = _container(_pod(_render(*_ON)), "recipe-preview")
    assert re.findall(r'connect\("([^"]+)"\)', _probe_script(rp)) == [_env(rp)["PREVIEW_SOCKET"]]


def _run_probe(script: str, socket_path: str) -> int:
    rendered = re.findall(r'connect\("([^"]+)"\)', script)
    assert len(rendered) == 1
    local = script.replace(f'connect("{rendered[0]}")', f'connect("{socket_path}")')
    return subprocess.run([sys.executable, "-c", local], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=30).returncode


def _short_dir() -> str:
    # AF_UNIX paths are limited to ~107 bytes; a test tmp_path can exceed it.
    return tempfile.mkdtemp(prefix="rp1040-", dir="/tmp" if os.path.isdir("/tmp") else None)


@_needs_helm
@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX")
def test_probe_passes_on_the_real_service_and_fails_without_it() -> None:
    script = _probe_script(_container(_pod(_render(*_ON)), "recipe-preview"))
    sock_dir = _short_dir()
    sock = os.path.join(sock_dir, "preview.sock")
    service = subprocess.Popen([sys.executable, str(_APP)], env={**os.environ, "PREVIEW_SOCKET": sock},
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not os.path.exists(sock):
            assert service.poll() is None, service.stderr.read()
            assert time.monotonic() < deadline, "the service never created its socket"
            time.sleep(0.1)
        assert _run_probe(script, sock) == 0
    finally:
        service.terminate()
        service.wait(timeout=30)
        service.stderr.close()
    assert _run_probe(script, sock) != 0, "no service listening must fail the probe"
    shutil.rmtree(sock_dir, ignore_errors=True)


@_needs_helm
@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX")
@pytest.mark.parametrize("status_line", [
    b"HTTP/1.0 503 Service Unavailable", b"HTTP/1.1 2000 Odd", b"garbage",
], ids=["503", "2000", "garbage"])
def test_probe_fails_on_anything_but_a_200(status_line: bytes) -> None:
    script = _probe_script(_container(_pod(_render(*_ON)), "recipe-preview"))
    sock_dir = _short_dir()
    sock = os.path.join(sock_dir, "preview.sock")
    server = socket.socket(socket.AF_UNIX)
    server.bind(sock)
    server.listen(1)

    def answer() -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(status_line + b"\r\nContent-Length: 0\r\n\r\n")

    worker = threading.Thread(target=answer, daemon=True)
    worker.start()
    try:
        assert _run_probe(script, sock) != 0, status_line
    finally:
        server.close()
        worker.join(timeout=10)
        shutil.rmtree(sock_dir, ignore_errors=True)
