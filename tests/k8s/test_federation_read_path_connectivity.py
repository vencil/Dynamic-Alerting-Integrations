"""The federation proxy can reach its upstream, and only it (#3807).

The read path is client → federation-gateway → federation-proxy
(prom-label-proxy) → Prometheus. In the documented install every hop runs in
`monitoring`, whose k8s/03-monitoring/network-policies.yaml denies ingress AND
egress by default. federation-proxy's chart rendered ingress only, so the last
hop had no egress at all: every federated query failed at the proxy.

Evaluated with the same NetworkPolicy evaluator as
tests/k8s/test_log_store_connectivity.py (imported, not copied): the raw
manifests plus the rendered chart, both ends of each edge.

⚠️ What this does not cover: the gateway → proxy hop's EGRESS half. The
gateway chart's egress is #1295 PR2's change; this file checks the proxy's
ingress side of that hop only.

The render-time refusals (host forms the chart will not guess a namespace
for) are tested here too, against the chart directly.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_log_store_connectivity as ev  # noqa: E402  (the shared evaluator)

CHART = ev._REPO_ROOT / "helm" / "federation-proxy"
_PROM_PORT = 9090
_needs_helm = ev._needs_helm


@pytest.fixture(scope="module")
def world():
    docs = []
    for path in sorted(ev._RAW.glob("*.yaml")):
        for d in ev._docs(path.read_text(encoding="utf-8")):
            d.setdefault("metadata", {}).setdefault("namespace", ev._MON)
            docs.append(d)
    docs += ev._render("helm/federation-proxy", ev._MON, [])
    policies = [d for d in docs if d.get("kind") == "NetworkPolicy"]
    pods = {}
    for d in docs:
        labels = ev._pod_labels(d)
        if labels:
            pods[(d["metadata"]["namespace"], d["metadata"]["name"], d["kind"])] = labels
    return policies, pods


@_needs_helm
def test_the_proxy_reaches_prometheus(world) -> None:
    policies, pods = world
    egress, ingress = ev._edge(policies, ev._pod(pods, ev._MON, "federation-proxy"),
                               ev._pod(pods, ev._MON, "prometheus"), _PROM_PORT)
    assert egress and ingress, (
        f"federation-proxy -> prometheus:{_PROM_PORT}: egress={egress}, ingress={ingress}")


@_needs_helm
def test_the_proxy_resolves_names(world) -> None:
    policies, pods = world
    dns = {"ns": "kube-system", "labels": {"k8s-app": "kube-dns"}}
    assert ev._allowed(policies, ev._pod(pods, ev._MON, "federation-proxy"), "egress", dns, 53, "UDP")


@_needs_helm
def test_the_gateway_is_admitted_by_the_proxy(world) -> None:
    policies, pods = world
    gateway = {"ns": ev._MON, "labels": {"app.kubernetes.io/name": "federation-gateway"}}
    assert ev._allowed(policies, ev._pod(pods, ev._MON, "federation-proxy"), "ingress",
                       gateway, 8080, "TCP")


@_needs_helm
def test_the_proxy_reaches_nothing_else(world) -> None:
    """The tenant header is trusted by the proxy; its egress is the upstream only."""
    policies, pods = world
    proxy = ev._pod(pods, ev._MON, "federation-proxy")
    ext = {"ns": None, "ip": ev._EXTERNAL_IP, "labels": {}}
    assert not ev._allowed(policies, proxy, "egress", ext, 443, "TCP"), "proxy reaches the internet"
    other = {"ns": "kube-system", "labels": {"app": "x"}}
    assert not ev._allowed(policies, proxy, "egress", other, _PROM_PORT, "TCP"), \
        "the upstream port is open to other namespaces"


# ── render-time: the egress rule's target ───────────────────────────────────


def _render(*args: str, ns: str = "obs") -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "fp", str(CHART), "-n", ns,
                           "--show-only", "templates/networkpolicy.yaml", *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)


def _target(*args: str) -> tuple[str, int]:
    out = _render(*args)
    assert out.returncode == 0, out.stderr
    egress = yaml.safe_load(out.stdout)["spec"]["egress"]
    rule = egress[1]
    return (rule["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            rule["ports"][0]["port"])


@_needs_helm
@pytest.mark.parametrize("url, expected", [
    ("http://prometheus:9090", ("obs", 9090)),                       # bare Service: this release's ns
    ("http://prometheus.monitoring.svc:9090", ("monitoring", 9090)),
    ("https://query.thanos.svc.cluster.local", ("thanos", 443)),     # scheme default port
    ("HTTP://Prometheus.MONITORING.svc:9090", ("monitoring", 9090)),  # DNS is case-insensitive
    ("http://prometheus.monitoring.svc:010", ("monitoring", 10)),     # decimal, not octal
    ("http://vmselect.vm.svc:8481/select/0/prometheus", ("vm", 8481)),  # path ignored
    ("http://vmselect.vm.svc", ("vm", 80)),
])
def test_the_target_is_read_from_the_url(url, expected) -> None:
    assert _target("--set", f"upstream.url={url}") == expected


@_needs_helm
def test_an_explicit_pod_port_wins() -> None:
    """Service 9090 -> pod 10902: the rule must name the pod port."""
    assert _target("--set", "upstream.url=http://query.thanos.svc:9090",
                   "--set", "networkPolicy.upstreamPort=10902") == ("thanos", 10902)


@_needs_helm
def test_an_explicit_namespace_wins() -> None:
    assert _target("--set", "upstream.url=http://prom.example.com:9090",
                   "--set", "networkPolicy.upstreamNamespace=edge") == ("edge", 9090)


@_needs_helm
@pytest.mark.parametrize("url, needle", [
    ("http://10.0.0.1:9090", "cannot tell which namespace"),
    ("http://[fd00::1]:9090", "names an IP address"),
    ("http://[::1]", "names an IP address"),
    ("http://prometheus.monitoring.svc:99999", "is not 1-65535"),
    ("http://..svc:9090", "is not a namespace name"),
    ("http://prom.example.com", "cannot tell which namespace"),
    ("http://prometheus.monitoring:9090", "cannot tell which namespace"),        # could be a domain
    ("http://prometheus.monitoring.svc.corp.local:9090", "cannot tell which namespace"),
    ("prometheus.monitoring.svc:9090", "must be http:// or https://"),
    # a non-numeric port is refused by helm's own urlParse
    ("http://prometheus.monitoring.svc:http", "unable to parse url"),
])
def test_the_render_refuses_to_guess(url, needle) -> None:
    out = _render("--set", f"upstream.url={url}")
    assert out.returncode != 0 and needle in out.stderr, out.stderr[-300:]


@_needs_helm
@pytest.mark.parametrize("args, needle", [
    (["--set", "networkPolicy.upstreamNamespace=Foo"], "is not a namespace name"),
    (["--set", "networkPolicy.upstreamPort=0"], "is not 1-65535"),
])
def test_bad_overrides_are_refused(args, needle) -> None:
    out = _render(*args)
    assert out.returncode != 0 and needle in out.stderr, out.stderr[-300:]


@_needs_helm
@pytest.mark.parametrize("value, restricted", [("false", False), ("true", True), ("null", True)])
def test_restrict_egress_switch(value, restricted) -> None:
    """Only an explicit false turns it off; unset (`--reuse-values` from 0.1.x) is on."""
    out = _render("--set", f"networkPolicy.restrictEgress={value}")
    assert out.returncode == 0, out.stderr
    spec = yaml.safe_load(out.stdout)["spec"]
    assert ("Egress" in spec["policyTypes"]) is restricted and bool(spec.get("egress")) is restricted
