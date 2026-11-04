"""Vector's scrape ports are reachable only from the scraping Prometheus (#1595 D2④).

WHY: with `metrics.enabled` on by default, Vector's self-telemetry on
`metrics.port` names every tenant sink (`vl_tenant_<accountId>`) with its event
counts; the projection-gate exposer serves the per-node verdict. Before this
chart shipped no NetworkPolicy, so any pod that could reach a Vector pod IP read
them. Vector takes no other inbound traffic (api disabled, kubernetes_logs reads
node files), so the policy admits ONLY the scrape ports, ONLY from the scrape
namespace, and denies all ingress when no scrape port is rendered.

WHAT IS CHECKED (rendered with `helm template`):
* default: one Ingress-only policy selecting the DaemonSet's pods, one rule, from
  the `monitoring` namespace, on exactly `metrics.port`;
* the admitted ports are exactly the scrape ports the DaemonSet declares
  (derived from the rendered containers' `metrics` / `gate-metrics` ports, so a
  new listener without a policy port, or a policy port nothing listens on, is red);
* `scrapeNamespaceLabel: null` -> any namespace (`namespaceSelector: {}`), never
  a rule without `from` (which would admit every source);
* no scrape port -> `ingress: []` (deny all);
* `networkPolicy.enabled=false` renders nothing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "tests"))

from _helm_gate import HAS_HELM  # noqa: E402

_CHART = str(_REPO / "helm" / "vector")
_PROJ = ["--set", "tenantProjections[0].tenantId=tenant-alpha",
         "--set", "tenantProjections[0].accountId=1000",
         "--set", "projectionGate.registry.configMapName=test-registry"]

pytestmark = pytest.mark.skipif(not HAS_HELM, reason="helm CLI not on PATH")


def _render(*args: str) -> list[dict]:
    out = subprocess.run(["helm", "template", "t", _CHART, "-n", "x", *args],
                         check=True, capture_output=True, text=True, encoding="utf-8", timeout=120).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _policies(docs):
    return [d for d in docs if d["kind"] == "NetworkPolicy"]


def _scrape_ports(docs) -> set[int]:
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    ports = set()
    for c in ds["spec"]["template"]["spec"]["containers"]:
        for p in c.get("ports") or []:
            if p["name"] in ("metrics", "gate-metrics"):
                ports.add(p["containerPort"])
    return ports


def _admitted(policy) -> set[int]:
    return {p["port"] for rule in policy["spec"].get("ingress") or [] for p in rule["ports"]}


def test_default_admits_only_the_scrape_port_from_monitoring():
    docs = _render()
    [np] = _policies(docs)
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    assert np["spec"]["podSelector"]["matchLabels"].items() <= \
        ds["spec"]["template"]["metadata"]["labels"].items()
    assert np["spec"]["policyTypes"] == ["Ingress"]
    [rule] = np["spec"]["ingress"]
    assert rule["from"] == [{"namespaceSelector": {"matchLabels": {
        "kubernetes.io/metadata.name": "monitoring"}}}]
    assert _admitted(np) == _scrape_ports(docs) == {9598}


@pytest.mark.parametrize("args", [[], _PROJ, ["--set", "metrics.enabled=false", *_PROJ],
                                  ["--set", "metrics.port=9700", *_PROJ]])
def test_admitted_ports_are_exactly_the_scrape_listeners(args):
    docs = _render(*args)
    [np] = _policies(docs)
    assert _admitted(np) == _scrape_ports(docs)
    assert _admitted(np), "a scrape port is rendered in every case here"


def test_null_namespace_label_means_any_namespace_not_any_source():
    [np] = _policies(_render("--set", "networkPolicy.scrapeNamespaceLabel=null"))
    [rule] = np["spec"]["ingress"]
    assert rule["from"] == [{"namespaceSelector": {}}]


def test_no_scrape_port_denies_all_ingress():
    docs = _render("--set", "metrics.enabled=false")
    [np] = _policies(docs)
    assert _scrape_ports(docs) == set()
    assert np["spec"]["policyTypes"] == ["Ingress"]
    assert np["spec"]["ingress"] == []


def test_disabled_renders_no_policy():
    assert _policies(_render("--set", "networkPolicy.enabled=false")) == []
