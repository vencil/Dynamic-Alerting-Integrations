"""test_tenant_api_portal_container.py — the portal in tenant-api's pod (ADR-027 §5, §9 item 3)

The tenant-api chart serves the portal's static files from a container in
its own pod, behind oauth2-proxy's "/" upstream. This guards, on the render:

  * the nginx config the portal container actually loads is the chart's —
    its command names it with ``-c`` and the directory is the chart's
    ConfigMap — so the image's own config is never read;
  * that config forwards nothing: no ``*_pass``, no njs, no include but
    mime.types; it listens on the pod's loopback only;
  * the container mounts no human socket and no secret, and its image is
    pinned to a release;
  * oauth2-proxy sends the portal "/" at that loopback address.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _render_baseline import baseline_args

_CHART = "helm/tenant-api"
_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")

#: Directives that hand a request to something else, and the njs module.
_FORWARDING = re.compile(r"\b(?:proxy|fastcgi|uwsgi|scgi|grpc|memcached)_pass\b|\bjs_\w+|\bload_module\b|\bperl\b")


@pytest.fixture(scope="module")
def render() -> list[dict]:
    root = Path(__file__).resolve().parents[2]
    res = subprocess.run(
        ["helm", "template", "t", str(root / _CHART), *baseline_args(root / _CHART)],
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
    )
    assert res.returncode == 0, res.stderr
    return [d for d in yaml.safe_load_all(res.stdout) if d]


def _pod(docs: list[dict]) -> dict:
    (dep,) = [d for d in docs if d["kind"] == "Deployment"]
    return dep["spec"]["template"]["spec"]


def _container(docs: list[dict], name: str) -> dict:
    (c,) = [c for c in _pod(docs)["containers"] if c["name"] == name]
    return c


def _loaded_config(docs: list[dict]) -> str:
    """The nginx config the portal container runs, read the way it does."""
    c = _container(docs, "portal")
    argv = (c.get("command") or []) + (c.get("args") or [])
    assert argv[:1] == ["nginx"] and "-c" in argv, f"portal must run nginx -c <chart config>: {argv}"
    conf = Path(argv[argv.index("-c") + 1])
    (mount,) = [m for m in c["volumeMounts"] if Path(m["mountPath"]) == conf.parent]
    (vol,) = [v for v in _pod(docs)["volumes"] if v["name"] == mount["name"]]
    (cm,) = [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == vol["configMap"]["name"]]
    return cm["data"][conf.name]


# adr-check: ADR-027/9.3g
@_needs_helm
def test_portal_runs_the_charts_config_and_it_forwards_nothing(render):
    conf = _loaded_config(render)
    directives = "\n".join(l.split("#", 1)[0] for l in conf.splitlines())
    assert not _FORWARDING.search(directives), f"the portal's nginx config forwards: {_FORWARDING.search(directives)}"
    includes = re.findall(r"^\s*include\s+([^;]+);", directives, re.M)
    assert includes == ["/etc/nginx/mime.types"], f"includes {includes}: only mime.types (no conf.d of the image)"
    listens = re.findall(r"^\s*listen\s+([^;]+);", directives, re.M)
    assert listens == ["127.0.0.1:8081"], f"listen {listens}: the pod's loopback only"


# adr-check: ADR-027/9.3c
@_needs_helm
def test_portal_container_mounts_no_identity_or_secret(render):
    c = _container(render, "portal")
    names = {m["name"] for m in c["volumeMounts"]}
    assert "human-sock" not in names, "only tenant-api and oauth2-proxy mount the human socket (ADR-027 §2)"
    vols = {v["name"]: v for v in _pod(render)["volumes"]}
    for n in names:
        assert "secret" not in vols[n], f"portal mounts secret volume {n}"
    assert not c.get("env"), "the portal reads no environment"
    # Pinned to a release (bump_docs.py --portal rewrites it), never a
    # floating tag; a digest is optional, as for the generator image.
    assert re.search(r":v\d+\.\d+\.\d+(?:@sha256:[0-9a-f]{64})?$", c["image"]), f"portal image {c['image']} is not pinned to a release"
    assert not c.get("ports"), "the portal exposes no container port: it is reached through oauth2-proxy only"


@_needs_helm
def test_oauth2_proxy_sends_slash_to_the_portal(render):
    (cm,) = [d for d in render if d["kind"] == "ConfigMap" and "-oauth2-proxy-" in d["metadata"]["name"]]
    cfg = yaml.safe_load(next(iter(cm["data"].values())))
    ups = {u["id"]: (u["path"], u["uri"]) for u in cfg["upstreamConfig"]["upstreams"]}
    assert ups == {"api": ("/api/", "unix:///run/ta-human/human.sock"), "portal": ("/", "http://127.0.0.1:8081")}
