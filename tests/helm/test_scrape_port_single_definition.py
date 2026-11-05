"""The scrape port of each platform chart has ONE definition (#3483).

Prometheus' platform-components job discovers endpoint slices and keeps only
the endpoint whose port equals the Service's `prometheus.io/port`. A chart
whose annotation, container port and listener can disagree silently loses
every target the moment an operator moves one of them, so:

  * threshold-exporter: the Service's and the pod's prometheus.io/port and the
    container port all follow service.port; a service.annotations override
    naming another port fails the render.
  * tenant-api: the listener is tenantApi.listenAddr (TA_ADDR); every place the
    chart names the port reads tenant-api.apiPort, and the render fails unless
    listenAddr ends in it. A podAnnotations override naming another port fails.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _render_baseline import baseline_args

_REPO = Path(__file__).resolve().parents[2]
_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")


def _render(chart: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "t", str(_REPO / chart), *baseline_args(_REPO / chart), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def _docs(out: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(out) if d]


def _one(docs, kind):
    found = [d for d in docs if d["kind"] == kind]
    assert len(found) == 1, (kind, len(found))
    return found[0]


def _ports_named(docs, chart_container: str) -> dict[str, set]:
    """Every place a render names the scrape/listen port, as strings."""
    dep = _one(docs, "Deployment")
    svc = next(d for d in docs if d["kind"] == "Service"
               and (d["metadata"].get("labels") or {}).get("dynamic-alerting.io/scrape") == "platform")
    pod = dep["spec"]["template"]
    c = next(c for c in pod["spec"]["containers"] if c["name"] == chart_container)
    got = {
        "service annotation": {svc["metadata"]["annotations"]["prometheus.io/port"]},
        "pod annotation": {pod["metadata"]["annotations"]["prometheus.io/port"]},
        "container port": {str(p["containerPort"]) for p in c.get("ports") or []},
    }
    for probe in ("readinessProbe", "livenessProbe"):
        port = ((c.get(probe) or {}).get("httpGet") or {}).get("port")
        if isinstance(port, int):
            got[probe] = {str(port)}
    return got


@_needs_helm
@pytest.mark.parametrize("port", ["8080", "9090"])
def test_threshold_exporter_annotations_follow_service_port(port):
    r = _render("helm/threshold-exporter", "--set", f"service.port={port}")
    assert r.returncode == 0, r.stderr
    got = _ports_named(_docs(r.stdout), "threshold-exporter")
    assert got and all(v == {port} for v in got.values()), got


@_needs_helm
def test_threshold_exporter_annotation_override_naming_another_port_fails():
    r = _render("helm/threshold-exporter", "--set", "service.annotations.prometheus\\.io/port=7000")
    assert r.returncode != 0 and "differs from service.port" in r.stderr, r.stderr


@_needs_helm
@pytest.mark.parametrize("listen", [":8080", "0.0.0.0:8080"])
def test_tenant_api_every_port_reference_is_the_listener(listen):
    r = _render("helm/tenant-api", "--set", f"tenantApi.listenAddr={listen}")
    assert r.returncode == 0, r.stderr
    docs = _docs(r.stdout)
    got = _ports_named(docs, "tenant-api")
    got["container port"] &= {"8080"}   # the api container also exposes nothing else
    svc = next(d for d in docs if d["kind"] == "Service")
    got["service targetPort"] = {str(p["targetPort"]) for p in svc["spec"]["ports"]
                                 if p["name"] == "api-internal"}
    netpol = next(d for d in docs if d["kind"] == "NetworkPolicy")
    got["networkpolicy"] = {str(p["port"]) for rule in netpol["spec"].get("ingress") or []
                            for p in rule.get("ports") or [] if str(p["port"]) != "4180"}
    assert all(v == {"8080"} for v in got.values()), got


@_needs_helm
@pytest.mark.parametrize("listen", [":9000", "0.0.0.0:9000", "localhost"])
def test_tenant_api_listener_on_another_port_fails_the_render(listen):
    r = _render("helm/tenant-api", "--set", f"tenantApi.listenAddr={listen}")
    assert r.returncode != 0 and "must end in :8080" in r.stderr, r.stderr


@_needs_helm
def test_tenant_api_pod_annotation_override_naming_another_port_fails():
    r = _render("helm/tenant-api", "--set", "podAnnotations.prometheus\\.io/port=9999")
    assert r.returncode != 0 and "is not the container port" in r.stderr, r.stderr
