"""Every platform scrape Service has a ServiceMonitor that scrapes it like the raw job does (#1595 D1b).

WHY: in Prometheus Operator mode (kube-prometheus-stack) Prometheus scrapes only
what a ServiceMonitor selects. Before D1b only threshold-exporter shipped one,
so every platform alert reading tenant-api, Vector or federation metrics had
zero series there. The raw config scrapes these Services with the
`platform-components` job (k8s/03-monitoring/configmap-prometheus.yaml): it
keeps Services labelled `dynamic-alerting.io/scrape: platform`, one target per
pod on the port the `prometheus.io/port` annotation names, `job` = Service name.

WHAT IS CHECKED, per chart whose template labels a Service for that job (the
chart list is DERIVED from the templates, with a literal anchor below so an
empty glob cannot pass):

* rendered with the values that turn the Service on and the ServiceMonitor on,
  each platform Service is reached by exactly ONE (ServiceMonitor, endpoint)
  pair — the monitor's selector matches the Service's labels and the endpoint
  names one of its ports (that is how the Operator builds targets);
* that port routes to the pod port the annotation names (the raw job's port);
* no monitor sets `jobLabel` (so `job` is the Service name, as in raw mode),
  and no monitor selects a Service outside the platform set;
* `_lib_metric_sources.CHART_SERVICEMONITOR_JOBS` — what operator-generate's
  portability derivation believes — equals the rendered charts and Service
  names, so dropping a ServiceMonitor or renaming a Service turns this red
  instead of leaving the derivation wrong;
* off by default (no CRD, nothing to apply), and off when its Service is off.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "tests"))
sys.path.insert(0, str(_REPO / "scripts" / "tools"))

from _helm_gate import HAS_HELM, helm_required  # noqa: E402
import _lib_metric_sources as ms  # noqa: E402

_LABEL = "dynamic-alerting.io/scrape"
_SERVICE_KIND = re.compile(r"^kind:\s*Service\s*$", re.MULTILINE)
_BASELINE = ["-f", str(_REPO / "tests" / "helm" / "tenant_api_render_baseline.yaml")]
_VECTOR_ON = ["--set", "metrics.enabled=true",
              "--set", "tenantProjections[0].tenantId=tenant-alpha",
              "--set", "tenantProjections[0].accountId=1000",
              "--set", "projectionGate.registry.configMapName=test-registry"]

# chart -> `helm template` args turning on every platform Service AND its
# ServiceMonitor. Must cover exactly the derived chart set.
_ENABLE = {
    "helm/threshold-exporter": ["--set", "rules.mode=operator"],
    "helm/tenant-api": _BASELINE + ["--set", "serviceMonitor.enabled=true"],
    "helm/vector": _VECTOR_ON + ["--set", "serviceMonitor.enabled=true"],
    "helm/federation-gateway": ["--set", "serviceMonitor.enabled=true"],
    "helm/federation-reconciler": ["--set", "serviceMonitor.enabled=true"],
}
# chart -> args that turn the Services on but leave the ServiceMonitor default.
_DEFAULTS = {
    "helm/threshold-exporter": [],
    "helm/tenant-api": _BASELINE,
    "helm/vector": _VECTOR_ON,
    "helm/federation-gateway": [],
    "helm/federation-reconciler": [],
}

# Non-vacuity anchor, LITERAL on purpose (deriving it from the glob it checks
# would let an empty glob pass).
_EXPECTED_CHARTS = {
    "helm/threshold-exporter", "helm/tenant-api", "helm/vector",
    "helm/federation-gateway", "helm/federation-reconciler",
}

_needs_helm = pytest.mark.skipif(not HAS_HELM, reason="helm CLI not on PATH")


def test_helm_present_when_required() -> None:
    """Fail closed: without helm every render assertion below would skip."""
    reason = helm_required()
    if reason:
        assert HAS_HELM, f"{reason}: but `helm` is not on PATH"


def _derived_charts() -> set[str]:
    """Charts with a template that puts the platform label on a Service."""
    found = set()
    for p in (_REPO / "helm").glob("*/templates/**/*.yaml"):
        text = p.read_text(encoding="utf-8")
        if f"{_LABEL}: platform" in text and _SERVICE_KIND.search(text):
            found.add("helm/" + p.relative_to(_REPO / "helm").parts[0])
    return found


def _render(chart: str, args: list[str]) -> list[dict]:
    out = subprocess.run(
        ["helm", "template", "t", str(_REPO / chart), "-n", "some-ns", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, timeout=60)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _platform_services(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") == "Service"
            and ((d["metadata"].get("labels") or {}).get(_LABEL) == "platform")
            and ((d["metadata"].get("annotations") or {}).get("prometheus.io/scrape") == "true")]


def _pod_port(docs: list[dict], svc: dict, port_name: str) -> int:
    """The pod port the Service port *port_name* routes to."""
    sp = next(p for p in svc["spec"]["ports"] if p.get("name") == port_name)
    target = sp.get("targetPort", sp["port"])
    if isinstance(target, int):
        return target
    sel = svc["spec"]["selector"]
    for d in docs:
        if d.get("kind") not in {"Deployment", "DaemonSet", "StatefulSet"}:
            continue
        tpl = d["spec"]["template"]
        labels = (tpl.get("metadata") or {}).get("labels") or {}
        if all(labels.get(k) == v for k, v in sel.items()):
            for c in tpl["spec"].get("containers", []):
                for p in c.get("ports") or []:
                    if p.get("name") == target:
                        return int(p["containerPort"])
    raise AssertionError(f"{svc['metadata']['name']}: targetPort {target!r} names no container port")


def _selects(sm: dict, labels: dict) -> bool:
    want = sm["spec"]["selector"].get("matchLabels") or {}
    assert not sm["spec"]["selector"].get("matchExpressions"), "matchExpressions not modelled"
    return all(labels.get(k) == v for k, v in want.items())


def test_the_chart_list_is_derived_and_covered() -> None:
    derived = _derived_charts()
    assert derived == _EXPECTED_CHARTS, (
        f"charts labelling a Service for the platform job changed: "
        f"new={sorted(derived - _EXPECTED_CHARTS)} gone={sorted(_EXPECTED_CHARTS - derived)} — "
        "a new one needs a ServiceMonitor and a row in _ENABLE")
    assert set(_ENABLE) == derived == set(_DEFAULTS)
    assert set(ms.CHART_SERVICEMONITOR_JOBS) == derived, (
        "_lib_metric_sources.CHART_SERVICEMONITOR_JOBS (operator-generate's portability "
        f"model) disagrees with the charts: {sorted(set(ms.CHART_SERVICEMONITOR_JOBS) ^ derived)}")


@_needs_helm
@pytest.mark.parametrize("chart", sorted(_EXPECTED_CHARTS))
def test_each_platform_service_is_scraped_once_on_the_raw_jobs_port(chart: str) -> None:
    docs = _render(chart, _ENABLE[chart])
    services = _platform_services(docs)
    monitors = [d for d in docs if d.get("kind") == "ServiceMonitor"]
    assert services, f"{chart}: no platform scrape Service rendered with {_ENABLE[chart]}"
    assert monitors, f"{chart}: no ServiceMonitor rendered with {_ENABLE[chart]}"
    for sm in monitors:
        assert "jobLabel" not in sm["spec"], (
            f"{chart}: ServiceMonitor {sm['metadata']['name']} sets jobLabel — `job` "
            "would no longer be the Service name the platform alerts select")
        assert "namespaceSelector" not in sm["spec"], sm["metadata"]["name"]
        assert sm["metadata"]["namespace"] == "some-ns", sm["metadata"]
    platform_names = {s["metadata"]["name"] for s in services}
    for d in docs:
        if d.get("kind") == "Service" and d["metadata"]["name"] not in platform_names:
            hit = [sm["metadata"]["name"] for sm in monitors
                   if _selects(sm, d["metadata"].get("labels") or {})]
            assert not hit, f"{chart}: {hit} also select non-platform Service {d['metadata']['name']}"
    for svc in services:
        names = {p.get("name") for p in svc["spec"]["ports"]}
        pairs = [(sm["metadata"]["name"], ep["port"]) for sm in monitors
                 if _selects(sm, svc["metadata"].get("labels") or {})
                 for ep in sm["spec"]["endpoints"] if ep.get("port") in names]
        assert len(pairs) == 1, (
            f"{chart} Service {svc['metadata']['name']}: reached by {pairs} "
            "(ServiceMonitor, endpoint) pairs — want exactly one")
        annotated = int(svc["metadata"]["annotations"]["prometheus.io/port"])
        assert _pod_port(docs, svc, pairs[0][1]) == annotated, (
            f"{chart} Service {svc['metadata']['name']}: the ServiceMonitor scrapes port "
            f"{pairs[0][1]!r}, which is not the annotated pod port {annotated} the raw "
            "platform-components job keeps")
    for sm in monitors:
        for ep in sm["spec"]["endpoints"]:
            assert any(ep.get("port") in {p.get("name") for p in s["spec"]["ports"]}
                       and _selects(sm, s["metadata"].get("labels") or {}) for s in services), (
                f"{chart}: endpoint {ep.get('port')!r} of {sm['metadata']['name']} reaches no Service")
    assert platform_names == set(ms.CHART_SERVICEMONITOR_JOBS[chart]), (
        f"{chart}: rendered platform Services {sorted(platform_names)} != the jobs "
        f"_lib_metric_sources.CHART_SERVICEMONITOR_JOBS declares "
        f"{sorted(ms.CHART_SERVICEMONITOR_JOBS[chart])} (job = Service name)")


@_needs_helm
@pytest.mark.parametrize("chart", sorted(_EXPECTED_CHARTS))
def test_servicemonitors_are_off_by_default(chart: str) -> None:
    docs = _render(chart, _DEFAULTS[chart])
    assert not [d for d in docs if d.get("kind") == "ServiceMonitor"], (
        f"{chart} renders a ServiceMonitor by default — without the "
        "monitoring.coreos.com CRDs the release cannot be applied")


@_needs_helm
@pytest.mark.parametrize("chart,args,ports", [
    ("helm/vector", ["--set", "serviceMonitor.enabled=true"], None),
    ("helm/vector", ["--set", "serviceMonitor.enabled=true", "--set", "metrics.enabled=true"],
     ["metrics"]),
    ("helm/federation-gateway",
     ["--set", "serviceMonitor.enabled=true", "--set", "auditLog.enabled=false"], None),
], ids=["vector-no-service", "vector-metrics-only", "gateway-no-audit"])
def test_an_endpoint_renders_only_with_its_service(chart, args, ports) -> None:
    monitors = [d for d in _render(chart, args) if d.get("kind") == "ServiceMonitor"]
    if ports is None:
        assert not monitors, f"{chart} {args}: a ServiceMonitor with no Service to scrape"
    else:
        assert [ep["port"] for m in monitors for ep in m["spec"]["endpoints"]] == ports


@_needs_helm
@pytest.mark.parametrize("chart", sorted(_EXPECTED_CHARTS - {"helm/threshold-exporter"}))
def test_selector_label_defaults_and_overrides(chart: str) -> None:
    labels = next(d for d in _render(chart, _ENABLE[chart])
                  if d.get("kind") == "ServiceMonitor")["metadata"]["labels"]
    assert labels.get("release") == "kube-prometheus-stack", labels
    dropped = next(d for d in _render(chart, _ENABLE[chart] + ["--set", "serviceMonitor.labels.release=null"])
                   if d.get("kind") == "ServiceMonitor")["metadata"]["labels"]
    assert "release" not in dropped, dropped
    res = subprocess.run(
        ["helm", "template", "t", str(_REPO / chart), *_ENABLE[chart],
         "--set", "serviceMonitor.labels.app\\.kubernetes\\.io/name=x"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert res.returncode != 0 and "must not set" in res.stderr, res.stderr[-400:]
