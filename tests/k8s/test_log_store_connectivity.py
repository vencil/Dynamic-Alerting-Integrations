"""Every reader and writer of the log store can actually reach it (#1295 follow-up).

WHY: a connection needs BOTH ends to allow it — the source's egress and the
destination's ingress — and in the documented install those two halves live in
different places: `k8s/03-monitoring/network-policies.yaml` default-denies the
monitoring namespace and allows per workload; each chart (victorialogs,
chargeback-aggregator, federation-reconciler) renders its own policy.
tests/k8s/test_networkpolicy_symmetry.py checks raw manifests only and says so;
nothing compared a raw manifest with a rendered chart. Measured before this
test existed, with every component in `monitoring` as the docs install them:

  * Grafana's egress allowed Prometheus and DNS only — its VictoriaLogs
    datasource and the plugin install from grafana.com (443) were dropped;
  * Prometheus' egress had no 9428 and the store's ingress did not admit it —
    the store was never scraped (vmauth's own metrics are on its internal
    port and are out of scope here);
  * chargeback-aggregator shipped no policy, so under the namespace's
    default-deny it could resolve no name and reach no store.

WHAT IS CHECKED: the policies are EVALUATED, not string-matched — every
NetworkPolicy from the raw manifests and the rendered charts goes into one
evaluator (matchLabels selectors, namespace selectors on
`kubernetes.io/metadata.name`, ipBlock, ports). Pod labels come from the real
pod templates (raw Deployments, rendered CronJobs/Jobs/Deployments), never typed
here. Each edge must be allowed at both ends; the anti-vacuity cell shows the
evaluator does deny (an unlisted pod in the same namespace cannot reach the
store).

⛔ The evaluator refuses what it does not model (matchExpressions, named
ports) instead of guessing. It is a model of the NetworkPolicy API, not a CNI:
what a given CNI enforces is out of scope.
"""
from __future__ import annotations

import ipaddress
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "tests"))

from _helm_gate import HAS_HELM, helm_required  # noqa: E402

_RAW = _REPO_ROOT / "k8s" / "03-monitoring"
_MON = "monitoring"
_STORE_PORT = 9428
_EXTERNAL_IP = "203.0.113.10"  # TEST-NET-3: some address outside the cluster

# chart -> (namespace, extra args). Namespaces as the READMEs install them.
_CHARTS = {
    "helm/victorialogs": (_MON, ["--set", "auth.mode=observe", "--set", "persistence.enabled=false"]),
    # manualJob on: the one-shot Job must be covered by the same policy.
    "helm/chargeback-aggregator": (_MON, ["--set", "manualJob.enabled=true"]),
    "helm/federation-reconciler": (_MON, []),
    # Vector runs in its own namespace (#1018).
    "helm/vector": ("vector", []),
}


class Unmodelled(Exception):
    pass


def test_helm_present_when_required() -> None:
    reason = helm_required()
    if reason:
        assert HAS_HELM, f"{reason}: but `helm` is not on PATH"


_needs_helm = pytest.mark.skipif(not HAS_HELM, reason="helm CLI not on PATH")


def _docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def _render(chart: str, ns: str, extra: list[str]) -> list[dict]:
    out = subprocess.run(
        ["helm", "template", Path(chart).name, str(_REPO_ROOT / chart), "-n", ns, *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, timeout=120)
    docs = _docs(out.stdout)
    for d in docs:
        d.setdefault("metadata", {}).setdefault("namespace", ns)
    return docs


def _pod_labels(doc: dict) -> dict | None:
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    if doc.get("kind") in {"Deployment", "DaemonSet", "StatefulSet", "Job", "CronJob"}:
        return dict(((spec.get("template") or {}).get("metadata") or {}).get("labels") or {})
    return None


@pytest.fixture(scope="module")
def world():
    docs = []
    for path in sorted(_RAW.glob("*.yaml")):
        for d in _docs(path.read_text(encoding="utf-8")):
            d.setdefault("metadata", {}).setdefault("namespace", _MON)
            docs.append(d)
    for chart, (ns, extra) in _CHARTS.items():
        docs += _render(chart, ns, extra)
    policies = [d for d in docs if d.get("kind") == "NetworkPolicy"]
    pods = {}
    for d in docs:
        labels = _pod_labels(d)
        if labels:
            pods[(d["metadata"]["namespace"], d["metadata"]["name"], d["kind"])] = labels
    return policies, pods


def _match_labels(selector: dict | None, labels: dict) -> bool:
    if selector is None:
        return False
    if selector.get("matchExpressions"):
        raise Unmodelled(f"matchExpressions in {selector}")
    return all(labels.get(k) == v for k, v in (selector.get("matchLabels") or {}).items())


def _ns_labels(ns: str) -> dict:
    return {"kubernetes.io/metadata.name": ns}


def _peer_matches(peer: dict, policy_ns: str, other: dict) -> bool:
    if "ipBlock" in peer:
        if other["ns"] is not None:
            return False
        block = peer["ipBlock"]
        ip = ipaddress.ip_address(other["ip"])
        return (ip in ipaddress.ip_network(block["cidr"])
                and not any(ip in ipaddress.ip_network(e) for e in block.get("except") or []))
    if other["ns"] is None:
        return False
    if "namespaceSelector" in peer:
        if not _match_labels(peer["namespaceSelector"], _ns_labels(other["ns"])):
            return False
    elif other["ns"] != policy_ns:
        return False
    if "podSelector" in peer:
        return _match_labels(peer["podSelector"], other["labels"])
    return True


def _port_matches(rule: dict, port: int, proto: str) -> bool:
    ports = rule.get("ports")
    if not ports:
        return True
    for p in ports:
        if "endPort" in p:
            raise Unmodelled(f"endPort range {p}")
        if not isinstance(p.get("port", port), int):
            raise Unmodelled(f"named port {p}")
        if p.get("protocol", "TCP") == proto and p.get("port", port) == port:
            return True
    return False


def _policy_types(policy: dict) -> list[str]:
    """`spec.policyTypes`, or the API's default when absent: Ingress always,
    plus Egress when the policy has egress rules."""
    spec = policy["spec"]
    if spec.get("policyTypes"):
        return spec["policyTypes"]
    return ["Ingress", "Egress"] if spec.get("egress") else ["Ingress"]


def _allowed(policies, pod: dict, direction: str, other: dict, port: int, proto: str) -> bool:
    """Is `pod`'s traffic in `direction` to/from `other` allowed by the policies
    that select it? No policy selecting it for this direction means allowed."""
    key, peers_key = ("Egress", "to") if direction == "egress" else ("Ingress", "from")
    selecting = [p for p in policies
                 if p["metadata"]["namespace"] == pod["ns"]
                 and key in _policy_types(p)
                 and _match_labels(p["spec"].get("podSelector") or {"matchLabels": {}}, pod["labels"])]
    if not selecting:
        return True
    for p in selecting:
        for rule in p["spec"].get(direction) or []:
            if not _port_matches(rule, port, proto):
                continue
            peers = rule.get(peers_key)
            if not peers or any(_peer_matches(peer, p["metadata"]["namespace"], other) for peer in peers):
                return True
    return False


def _pod(pods, ns: str, name: str) -> dict:
    hits = [labels for (n, w, _), labels in pods.items() if n == ns and w == name]
    assert len(hits) == 1, f"workload {ns}/{name}: found {len(hits)} (pods: {sorted(pods)})"
    return {"ns": ns, "labels": hits[0]}


def _edge(policies, src, dst, port, proto="TCP") -> tuple[bool, bool]:
    return (_allowed(policies, src, "egress", dst, port, proto),
            _allowed(policies, dst, "ingress", src, port, proto))


_STORE = ("monitoring", "victorialogs")
_EDGES = [
    (("monitoring", "grafana"), _STORE, _STORE_PORT),          # VictoriaLogs datasource
    (("monitoring", "prometheus"), _STORE, _STORE_PORT),       # /metrics scrape
    (("monitoring", "chargeback-aggregator"), _STORE, _STORE_PORT),
    (("monitoring", "chargeback-aggregator-manual"), _STORE, _STORE_PORT),
    (("monitoring", "federation-reconciler"), _STORE, _STORE_PORT),
    (("vector", "vector"), _STORE, _STORE_PORT),               # ingest
]


def _edge_id(edge) -> str:
    src, dst, port = edge
    return f"{'/'.join(src)}->{'/'.join(dst)}:{port}"


@_needs_helm
@pytest.mark.parametrize("edge", _EDGES, ids=_edge_id)
def test_each_consumer_reaches_the_store(world, edge) -> None:
    policies, pods = world
    src, dst, port = edge
    s, d = _pod(pods, *src), _pod(pods, *dst)
    egress, ingress = _edge(policies, s, d, port)
    assert egress and ingress, (
        f"{_edge_id(edge)}: egress allowed={egress}, ingress allowed={ingress}. "
        f"Both ends must allow it; the dropped half is the file to fix "
        f"(k8s/03-monitoring/network-policies.yaml or the chart).")


@_needs_helm
@pytest.mark.parametrize("src", [("monitoring", "grafana"), ("monitoring", "chargeback-aggregator")],
                         ids=lambda s: "/".join(s))
def test_dns_is_reachable(world, src) -> None:
    policies, pods = world
    dns = {"ns": "kube-system", "labels": {"k8s-app": "kube-dns"}}
    assert _allowed(policies, _pod(pods, *src), "egress", dns, 53, "UDP"), f"{src}: no DNS egress"


@_needs_helm
def test_grafana_reaches_grafana_com_for_the_plugin(world) -> None:
    policies, pods = world
    g = _pod(pods, "monitoring", "grafana")
    ext = {"ns": None, "ip": _EXTERNAL_IP, "labels": {}}
    assert _allowed(policies, g, "egress", ext, 443, "TCP"), (
        "grafana cannot reach 443 outside the cluster: GF_PLUGINS_PREINSTALL_SYNC "
        "cannot install the VictoriaLogs datasource plugin")
    meta = {"ns": None, "ip": "169.254.169.254", "labels": {}}
    assert not _allowed(policies, g, "egress", meta, 443, "TCP"), "metadata endpoint reachable"


@_needs_helm
def test_an_unlisted_pod_cannot_reach_the_store(world) -> None:
    """Anti-vacuity: the evaluator denies, and the store is not open to its namespace."""
    policies, pods = world
    stranger = {"ns": "monitoring", "labels": {"app": "something-else"}}
    egress, ingress = _edge(policies, stranger, _pod(pods, *_STORE), _STORE_PORT)
    assert not egress and not ingress, (egress, ingress)


def test_missing_policy_types_default_as_the_api_does() -> None:
    assert _policy_types({"spec": {"egress": [{}]}}) == ["Ingress", "Egress"]
    assert _policy_types({"spec": {"ingress": [{}]}}) == ["Ingress"]
    assert _policy_types({"spec": {"policyTypes": ["Egress"]}}) == ["Egress"]


def test_evaluator_refuses_what_it_does_not_model() -> None:
    with pytest.raises(Unmodelled):
        _match_labels({"matchExpressions": [{"key": "a", "operator": "Exists"}]}, {})
    with pytest.raises(Unmodelled):
        _port_matches({"ports": [{"port": "http"}]}, 80, "TCP")
    with pytest.raises(Unmodelled):
        _port_matches({"ports": [{"port": 80, "endPort": 443}]}, 443, "TCP")


@_needs_helm
def test_reuse_values_from_a_release_without_the_map_keeps_the_policy() -> None:
    """`helm upgrade --reuse-values` from chargeback-aggregator 0.3.x carries
    values with no `networkPolicy` map; the render must not fail and must keep
    the chart default (policy on, store in monitoring)."""
    docs = _render("helm/chargeback-aggregator", _MON, ["--set", "networkPolicy=null"])
    nps = [d for d in docs if d.get("kind") == "NetworkPolicy"]
    assert len(nps) == 1, nps
    peers = [peer for rule in nps[0]["spec"]["egress"] for peer in rule.get("to", [])]
    assert {"kubernetes.io/metadata.name": "monitoring"} in [
        (p.get("namespaceSelector") or {}).get("matchLabels") for p in peers], peers
