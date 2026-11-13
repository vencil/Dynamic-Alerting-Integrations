"""Render-time guards of the log store's NetworkPolicy and the gateway's egress (#1295 PR2).

helm/victorialogs renders its ingress from `auth.consumers[].from` and
`networkPolicy.scrapers`. A wrong value there does not fail loudly at the
network layer — it admits too much or nothing — so the chart refuses it at
render time. Each case below is one such refusal; each was red with the guard
removed (mutation-checked when it was added).

helm/federation-gateway restricts egress to its upstream's namespace, read from
`upstream.host`. A host it cannot place must fail the render, not be guessed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
STORE = REPO / "helm" / "victorialogs"
GATEWAY = REPO / "helm" / "federation-gateway"
BASELINE = Path(__file__).resolve().parent / "victorialogs_render_baseline.yaml"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")


def _helm(chart: Path, *args: str, ns: str = "monitoring") -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "t", str(chart), "-n", ns, *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)


def _one_consumer(frm: str) -> list[str]:
    return ["--set", "auth.mode=observe", "--set-json",
            f'auth.consumers=[{{"name":"a","paths":["/x"],"from":{frm}}}]']


@pytest.mark.parametrize("args, needle", [
    # --reuse-values from <0.6.0 carries the removed keys
    (["-f", str(BASELINE), "--set-json", 'networkPolicy.allowedNamespaces=["vector"]'],
     "networkPolicy.allowedNamespaces was removed"),
    (["-f", str(BASELINE), "--set-json", "networkPolicy.allowedPodSelectors=[]"],
     "networkPolicy.allowedPodSelectors was removed"),
    # a restated registry without `from`
    (["--set", "auth.mode=observe", "--set-json", 'auth.consumers=[{"name":"a","paths":["/x"]}]'],
     "auth.consumers[a] has no `from`"),
    # a typo must not drop the restriction
    (_one_consumer('{"podSelector":{"app":"x"}}'), 'unknown key "podSelector"'),
    (_one_consumer('{"namespace":""}'), "needs `namespace`, `podLabels`, or both"),
    # the whole store namespace
    (_one_consumer('{"namespace":"monitoring"}'), "admits every pod in the store's own namespace"),
    (_one_consumer('"grafana"'), "`from` must be a map"),
    # YAML 1.1: a bare `no` is false; `default ""` would silently drop the namespace
    (_one_consumer('{"namespace":false,"podLabels":{"app":"x"}}'), "`from.namespace` must be a string"),
    (_one_consumer('{"podLabels":"app=x"}'), "`from.podLabels` must be a map"),
    # an empty `from` list admits every source
    (["--set", "auth.mode=observe", "--set-json", "auth.consumers=[]",
      "--set-json", "networkPolicy.scrapers=[]"], "both empty"),
    (["-f", str(BASELINE), "--set-json", 'networkPolicy.scrapers=[{"namespace":"monitoring"}]'],
     "networkPolicy.scrapers[0]: `from.namespace: monitoring` without `podLabels`"),
])
def test_store_render_refuses(args: list[str], needle: str) -> None:
    out = _helm(STORE, *args)
    assert out.returncode != 0 and needle in out.stderr, (out.returncode, out.stderr[-500:])


def test_store_admits_a_consumer_in_another_namespace_by_namespace_alone() -> None:
    out = _helm(STORE, *_one_consumer('{"namespace":"obs"}'), "--show-only", "templates/networkpolicy.yaml")
    assert out.returncode == 0, out.stderr
    peers = yaml.safe_load(out.stdout)["spec"]["ingress"][0]["from"]
    assert {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "obs"}}} in peers, peers


def test_store_without_the_scrapers_key_keeps_prometheus() -> None:
    """`networkPolicy` from a values file that never had `scrapers` (but also no
    removed key) still admits the default scraper."""
    out = _helm(STORE, "-f", str(BASELINE), "--set", "networkPolicy.scrapers=null",
                "--show-only", "templates/networkpolicy.yaml")
    assert out.returncode == 0, out.stderr
    peers = yaml.safe_load(out.stdout)["spec"]["ingress"][0]["from"]
    assert {"podSelector": {"matchLabels": {"app": "prometheus"}}} in peers, peers


def _gateway_egress(*args: str, ns: str = "obs") -> list[dict] | None:
    out = _helm(GATEWAY, *args, "--show-only", "templates/networkpolicy.yaml", ns=ns)
    assert out.returncode == 0, out.stderr
    return yaml.safe_load(out.stdout)["spec"].get("egress")


def _upstream_ns(egress: list[dict]) -> str:
    peers = [p for rule in egress for p in rule["to"] if p.get("namespaceSelector")]
    return peers[0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]


@pytest.mark.parametrize("host, ns", [
    ("federation-proxy", "obs"),                                  # bare Service name: this release's ns
    ("federation-proxy.monitoring.svc", "monitoring"),
    ("federation-proxy.monitoring.svc.cluster.local", "monitoring"),
])
def test_gateway_reads_the_upstream_namespace_from_the_host(host: str, ns: str) -> None:
    assert _upstream_ns(_gateway_egress("--set", f"upstream.host={host}")) == ns


def test_gateway_egress_is_dns_and_the_upstream_port_only() -> None:
    egress = _gateway_egress("--set", "upstream.port=9428")
    assert egress == [
        {"to": [{"namespaceSelector": {}}],
         "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
        {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}}],
         "ports": [{"protocol": "TCP", "port": 9428}]},
    ], egress


def test_gateway_explicit_upstream_namespace_wins() -> None:
    egress = _gateway_egress("--set", "upstream.host=vmselect.corp.example",
                             "--set", "networkPolicy.upstreamNamespace=vm")
    assert _upstream_ns(egress) == "vm"


@pytest.mark.parametrize("host", ["vmselect.corp", "vmselect.corp.example", "10.0.0.1",
                                  "fd00::1",  # an IPv6 literal must not read as a bare Service name
                                  # an external FQDN whose third label happens to be `svc`
                                  "api.prod.svc.example.com",
                                  # a custom cluster domain cannot be told from the above
                                  "federation-proxy.monitoring.svc.corp.local", "a.b"])
def test_gateway_refuses_a_host_it_cannot_place(host: str) -> None:
    out = _helm(GATEWAY, "--set", f"upstream.host={host}")
    assert out.returncode != 0 and ("cannot tell which namespace" in out.stderr
                                    or "is an IP address" in out.stderr), out.stderr[-400:]


def test_gateway_lower_cases_the_host() -> None:
    """DNS is case-insensitive, namespace names are not: `MONITORING` would select nothing."""
    assert _upstream_ns(_gateway_egress("--set", "upstream.host=Proxy.MONITORING.svc")) == "monitoring"


@pytest.mark.parametrize("args, needle", [
    (["--set", "upstream.host=..svc"], "is not a namespace name"),
    (["--set", "networkPolicy.upstreamNamespace=Obs"], "is not a namespace name"),
    (["--set", "networkPolicy.upstreamPort=0"], "is not 1-65535"),
    (["--set", "upstream.port=70000"], "is not 1-65535"),
])
def test_gateway_refuses_bad_targets(args, needle) -> None:
    out = _helm(GATEWAY, *args)
    assert out.returncode != 0 and needle in out.stderr, out.stderr[-400:]


def test_gateway_explicit_pod_port_wins() -> None:
    """Service port -> a different pod port: the rule must name the pod port."""
    egress = _gateway_egress("--set", "upstream.port=9090", "--set", "networkPolicy.upstreamPort=10902")
    assert egress[1]["ports"] == [{"protocol": "TCP", "port": 10902}], egress


@pytest.mark.parametrize("value, restricted", [("false", False), ("true", True), ("null", True)])
def test_gateway_restrict_egress_switch(value: str, restricted: bool) -> None:
    """Only an explicit false turns it off; unset (`--reuse-values` from 0.7.x) is on."""
    out = _helm(GATEWAY, "--set", f"networkPolicy.restrictEgress={value}",
                "--show-only", "templates/networkpolicy.yaml")
    assert out.returncode == 0, out.stderr
    spec = yaml.safe_load(out.stdout)["spec"]
    assert ("Egress" in spec["policyTypes"]) is restricted and bool(spec.get("egress")) is restricted, spec
