"""Prometheus egress admits every port a platform chart asks it to scrape (#1291).

WHY: `k8s/03-monitoring/network-policies.yaml` default-denies egress in the
monitoring namespace, so Prometheus reaches only the ports
`allow-prometheus-egress` lists. The `platform-components` scrape job
(`configmap-prometheus.yaml`) keeps every Service labelled
`dynamic-alerting.io/scrape: platform` in ANY namespace (#1425) and scrapes the
port its `prometheus.io/port` annotation names. The two sets were written in
different files and nothing compared them: Vector's 9598 (self-telemetry) and
9599 (projection-gate verdict) were missing, so the four Vector platform alerts
had zero series and could never fire, while every lint called them REACHABLE.
A blocked scrape looks exactly like a healthy one with nothing to report.

WHAT IS CHECKED: render each chart that labels a Service for the platform job
(with the values that turn those Services on), take each such Service's scrape
port and the pod port it lands on, and require both in a rule of
`allow-prometheus-egress` whose peer is `namespaceSelector: {}` — the chart's
namespace is not known here, so only an any-namespace rule admits it.

SCOPE BOUNDARY: the egress end only. The ingress end is each chart's own
NetworkPolicy; measured 2026-10-31, threshold-exporter and tenant-api admit
Prometheus on these ports and the vector chart ships no NetworkPolicy. A
cluster's own default-deny in the Vector namespace is outside this repo. Charts
scraped by `monitoring-components` (installed into `monitoring`, no platform
label) are not covered here.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "tests"))

from _helm_gate import HAS_HELM, helm_required  # noqa: E402

_NETPOL = _REPO_ROOT / "k8s" / "03-monitoring" / "network-policies.yaml"
_POLICY = "allow-prometheus-egress"
_PLATFORM_LABEL = "dynamic-alerting.io/scrape"

# chart -> extra `helm template` args turning on every Service it labels for
# the platform job. The Vector Services are off by default: `-metrics` needs
# metrics.enabled, `-projection-gate-metrics` needs tenantProjections.
_CHARTS = {
    "helm/threshold-exporter": [],
    "helm/tenant-api": ["-f", str(_REPO_ROOT / "tests" / "helm" / "tenant_api_render_baseline.yaml")],
    "helm/vector": [
        "--set", "metrics.enabled=true",
        "--set", "tenantProjections[0].tenantId=tenant-alpha",
        "--set", "tenantProjections[0].accountId=1000",
        "--set", "projectionGate.registry.configMapName=test-registry",
    ],
}

# Non-vacuity anchor, LITERAL on purpose: derived from the same render it would
# let an empty render pass. Every chart file under helm/*/templates that sets
# the platform label must appear here (test_every_platform_label_template_is_rendered).
_EXPECTED = {
    ("helm/threshold-exporter", "threshold-exporter", 8080),
    ("helm/tenant-api", "tenant-api", 8080),
    ("helm/vector", "vector-metrics", 9598),
    ("helm/vector", "vector-projection-gate-metrics", 9599),
}

_needs_helm = pytest.mark.skipif(not HAS_HELM, reason="helm CLI not on PATH")


def test_helm_present_when_required() -> None:
    """Fail closed: without helm every render assertion below would skip."""
    reason = helm_required()
    if reason:
        assert HAS_HELM, f"{reason}: but `helm` is not on PATH"


def _render(chart: str, extra: list[str]) -> list[dict]:
    out = subprocess.run(
        ["helm", "template", "t", str(_REPO_ROOT / chart), "-n", "some-ns", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, timeout=60)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _container_ports(docs: list[dict], selector: dict) -> dict[str, int]:
    """name -> containerPort over the workloads whose pod labels match *selector*."""
    ports: dict[str, int] = {}
    for d in docs:
        if d.get("kind") not in {"Deployment", "DaemonSet", "StatefulSet"}:
            continue
        tpl = d["spec"]["template"]
        labels = (tpl.get("metadata") or {}).get("labels") or {}
        if not all(labels.get(k) == v for k, v in selector.items()):
            continue
        for c in tpl["spec"].get("containers", []):
            for p in c.get("ports") or []:
                if p.get("name"):
                    ports[p["name"]] = int(p["containerPort"])
    return ports


def _scrape_targets() -> set[tuple[str, str, int, int]]:
    """(chart, service, annotated port, pod port) for every platform-labelled Service."""
    found = set()
    for chart, extra in _CHARTS.items():
        docs = _render(chart, extra)
        for d in docs:
            md = d.get("metadata") or {}
            if d.get("kind") != "Service" or (md.get("labels") or {}).get(_PLATFORM_LABEL) != "platform":
                continue
            ann = md.get("annotations") or {}
            if ann.get("prometheus.io/scrape") != "true":
                continue
            port = int(ann["prometheus.io/port"])
            svc_port = next((p for p in d["spec"]["ports"] if int(p["port"]) == port), None)
            assert svc_port is not None, (
                f"{chart} Service {md['name']} annotates scrape port {port} but exposes no such port")
            target = svc_port.get("targetPort", port)
            if isinstance(target, str):
                named = _container_ports(docs, d["spec"]["selector"])
                assert target in named, (
                    f"{chart} Service {md['name']}: targetPort {target!r} names no container port")
                target = named[target]
            found.add((chart, md["name"], port, int(target)))
    return found


def _any_namespace_egress_ports() -> set[int]:
    """TCP ports `allow-prometheus-egress` admits to a peer in any namespace."""
    docs = [d for d in yaml.safe_load_all(_NETPOL.read_text(encoding="utf-8")) if d]
    policy = next(d for d in docs
                  if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == _POLICY)
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "prometheus"}}, (
        f"{_POLICY} no longer selects app=prometheus — re-check what it governs")
    ports = set()
    for rule in policy["spec"].get("egress") or []:
        if not any(peer == {"namespaceSelector": {}} for peer in rule.get("to") or []):
            continue
        for p in rule.get("ports") or []:
            if p.get("protocol", "TCP") == "TCP":
                ports.add(int(p["port"]))
    return ports


@_needs_helm
def test_rendered_targets_cover_the_literal_anchor() -> None:
    got = {(c, s, p) for c, s, p, _ in _scrape_targets()}
    missing = _EXPECTED - got
    assert not missing, (
        f"expected platform scrape targets not rendered: {sorted(missing)} — a chart "
        "renamed its Service or the render args above stopped enabling it")


@_needs_helm
def test_prometheus_egress_admits_every_platform_scrape_port() -> None:
    allowed = _any_namespace_egress_ports()
    blocked = sorted((c, s, p, t) for c, s, p, t in _scrape_targets()
                     if p not in allowed or t not in allowed)
    assert not blocked, (
        f"{_POLICY} (k8s/03-monitoring/network-policies.yaml) does not admit, to any "
        f"namespace, these platform scrape targets (chart, service, port, pod port): "
        f"{blocked}. Prometheus gets zero series from them and every alert reading "
        f"them can never fire (#1291). Admitted any-namespace TCP ports: {sorted(allowed)}")


def test_every_platform_label_template_is_rendered() -> None:
    """A new chart labelling a Service for the platform job must join _CHARTS."""
    labelled = {f"helm/{p.relative_to(_REPO_ROOT / 'helm').parts[0]}"
                for p in (_REPO_ROOT / "helm").glob("*/templates/*.yaml")
                if f"{_PLATFORM_LABEL}: platform" in p.read_text(encoding="utf-8")}
    assert labelled, "no chart template carries the platform scrape label — glob broke"
    assert labelled == set(_CHARTS), (
        f"charts labelling Services for the platform job {sorted(labelled)} "
        f"!= charts this test renders {sorted(_CHARTS)}")
