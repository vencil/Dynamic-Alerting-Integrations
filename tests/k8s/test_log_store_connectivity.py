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

WHICH EDGES: derived, not listed (#1295 PR2). Every consumer in the store's
registry (helm/victorialogs `auth.consumers`) is an edge, from every workload
that declares that username — a chart through `victorialogs.auth.username`,
a raw manifest through the Grafana datasource it mounts. A registered consumer
with no rendered workload is red, and so is a consumer chart this test does
not render. The store's ingress is itself rendered from the same registry
(`auth.consumers[].from`), so the ingress half of each edge checks that the
consumer's declared network identity matches the labels and namespace its
pods really carry. Prometheus scrapes but is not a consumer; it is the one
listed edge.

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
_VL_PLUGIN = "victoriametrics-logs-datasource"
_EXTERNAL_IP = "203.0.113.10"  # TEST-NET-3: some address outside the cluster

# chart -> (namespace, extra args). Namespaces as the READMEs install them.
_CHARTS = {
    "helm/victorialogs": (_MON, ["--set", "auth.mode=observe", "--set", "persistence.enabled=false"]),
    # manualJob on: the one-shot Job must be covered by the same policy.
    "helm/chargeback-aggregator": (_MON, ["--set", "manualJob.enabled=true"]),
    "helm/federation-reconciler": (_MON, []),
    # In the store's namespace, as the gateway runbooks operate it
    # (`kubectl -n monitoring rollout status deployment/federation-gateway`);
    # the log-query mode is the one that talks to the store.
    "helm/federation-gateway": (_MON, ["--set", "mode=victorialogs",
                                       "--set", "jwt.audience=tenant-federation-logs",
                                       "--set", "upstream.host=victorialogs.monitoring.svc",
                                       "--set", "upstream.port=9428"]),
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


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _registry() -> list[str]:
    return [c["name"] for c in _load_yaml(_REPO_ROOT / "helm/victorialogs/values.yaml")["auth"]["consumers"]]


def _consumer_charts() -> dict[str, str]:
    """chart path -> declared username, for every chart with a `victorialogs`
    values block (the discovery rule of tests/shared/test_victorialogs_consumers.py)."""
    out = {}
    for chart_yaml in sorted((_REPO_ROOT / "helm").glob("*/Chart.yaml")):
        chart = chart_yaml.parent
        if chart.name == "victorialogs":
            continue
        block = _load_yaml(chart / "values.yaml").get("victorialogs")
        if block is not None:
            out[f"helm/{chart.name}"] = ((block or {}).get("auth") or {}).get("username")
    return out


def _datasource_users(doc: dict) -> list[str]:
    """basicAuthUser of every VictoriaLogs datasource a raw ConfigMap provisions."""
    users = []
    if doc.get("kind") != "ConfigMap":
        return users
    for key, text in (doc.get("data") or {}).items():
        if key.endswith((".yaml", ".yml")) and "datasources" in text:
            for ds in (yaml.safe_load(text) or {}).get("datasources") or []:
                if ds.get("type") == _VL_PLUGIN:
                    users.append(ds.get("basicAuthUser"))
    return users


def _mounted_configmaps(doc: dict) -> set[str]:
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    pod = (spec.get("template") or {}).get("spec") or {}
    return {(v.get("configMap") or {}).get("name") for v in pod.get("volumes") or []} - {None}


@pytest.fixture(scope="module")
def world():
    """(policies, pods, owners): owners maps a consumer username to the
    workload keys that act as it."""
    raw = []
    for path in sorted(_RAW.glob("*.yaml")):
        for d in _docs(path.read_text(encoding="utf-8")):
            d.setdefault("metadata", {}).setdefault("namespace", _MON)
            raw.append(d)
    owners: dict[str, list] = {}
    rendered = []
    users = _consumer_charts()
    for chart, (ns, extra) in _CHARTS.items():
        docs = _render(chart, ns, extra)
        rendered += docs
        for d in docs:
            if _pod_labels(d) and chart in users:
                owners.setdefault(users[chart], []).append(_key(d))
    by_cm = {}
    for d in raw:
        for user in _datasource_users(d):
            by_cm.setdefault((d["metadata"]["namespace"], d["metadata"]["name"]), []).append(user)
    for d in raw:
        if _pod_labels(d):
            for cm in _mounted_configmaps(d):
                for user in by_cm.get((d["metadata"]["namespace"], cm), []):
                    owners.setdefault(user, []).append(_key(d))
    docs = raw + rendered
    policies = [d for d in docs if d.get("kind") == "NetworkPolicy"]
    pods = {}
    for d in docs:
        labels = _pod_labels(d)
        if labels:
            pods[_key(d)] = labels
    return policies, pods, owners


def _key(doc: dict) -> tuple[str, str, str]:
    return (doc["metadata"]["namespace"], doc["metadata"]["name"], doc["kind"])


def _match_labels(selector: dict | None, labels: dict) -> bool:
    if selector is None:
        return False
    if selector.get("matchExpressions"):
        raise Unmodelled(f"matchExpressions in {selector}")
    return all(labels.get(k) == v for k, v in (selector.get("matchLabels") or {}).items())


_NS_NAME = "kubernetes.io/metadata.name"


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
        keys = set((peer["namespaceSelector"] or {}).get("matchLabels") or {})
        if keys - {_NS_NAME}:
            # Only the name label is known here; any other would read as
            # "no match" and turn every deny assertion vacuous.
            raise Unmodelled(f"namespace label(s) {sorted(keys - {_NS_NAME})} in {peer}")
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


def test_every_consumer_chart_is_rendered() -> None:
    """A chart that talks to the store and is not rendered here would be an
    edge nobody evaluates."""
    missing = sorted(set(_consumer_charts()) - set(_CHARTS))
    assert not missing, (
        f"{missing} declare victorialogs.auth.username but are not in _CHARTS: add each "
        "with the namespace its README installs it in")


@_needs_helm
@pytest.mark.parametrize("consumer", _registry())
def test_each_consumer_reaches_the_store(world, consumer) -> None:
    policies, pods, owners = world
    keys = owners.get(consumer)
    assert keys, (
        f"auth.consumers[{consumer}] matches no rendered workload: no chart here declares "
        f"victorialogs.auth.username={consumer} and no raw manifest mounts a datasource with "
        f"basicAuthUser={consumer}. Owners found: {sorted(owners)}")
    store = _pod(pods, *_STORE)
    for ns, name, kind in keys:
        src = {"ns": ns, "labels": pods[(ns, name, kind)]}
        egress, ingress = _edge(policies, src, store, _STORE_PORT)
        assert egress and ingress, (
            f"{consumer} ({kind} {ns}/{name}) -> {'/'.join(_STORE)}:{_STORE_PORT}: "
            f"egress allowed={egress}, ingress allowed={ingress}. A dropped ingress means "
            f"auth.consumers[{consumer}].from does not match the pod's labels/namespace; "
            "a dropped egress is the consumer's own chart or k8s/03-monitoring/network-policies.yaml.")


@_needs_helm
def test_prometheus_scrapes_the_store(world) -> None:
    """Not a consumer (no credentials): admitted by networkPolicy.scrapers."""
    policies, pods, _ = world
    egress, ingress = _edge(policies, _pod(pods, _MON, "prometheus"), _pod(pods, *_STORE), _STORE_PORT)
    assert egress and ingress, f"prometheus -> store: egress={egress}, ingress={ingress}"


@_needs_helm
@pytest.mark.parametrize("src", [("monitoring", "grafana"), ("monitoring", "chargeback-aggregator"),
                                 ("monitoring", "federation-gateway")],
                         ids=lambda s: "/".join(s))
def test_dns_is_reachable(world, src) -> None:
    policies, pods, _ = world
    dns = {"ns": "kube-system", "labels": {"k8s-app": "kube-dns"}}
    assert _allowed(policies, _pod(pods, *src), "egress", dns, 53, "UDP"), f"{src}: no DNS egress"


@_needs_helm
def test_grafana_reaches_grafana_com_for_the_plugin(world) -> None:
    policies, pods, _ = world
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
    policies, pods, _ = world
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
    with pytest.raises(Unmodelled):
        _peer_matches({"namespaceSelector": {"matchLabels": {"team": "obs"}}}, "monitoring",
                      {"ns": "obs", "labels": {}})


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


@_needs_helm
def test_a_stranger_in_the_vector_namespace_cannot_reach_the_store(world) -> None:
    """Vector's peer is its namespace AND its pod labels: another pod in that
    namespace is not admitted."""
    policies, pods, _ = world
    stranger = {"ns": "vector", "labels": {"app": "debug"}}
    assert not _allowed(policies, _pod(pods, *_STORE), "ingress", stranger, _STORE_PORT, "TCP")


@_needs_helm
def test_the_gateway_reaches_nothing_but_dns_and_its_upstream(world) -> None:
    """The gateway's egress (#1295 PR2) is DNS plus upstream.port in the
    upstream's namespace; it is internet-facing, so nothing else."""
    policies, pods, _ = world
    gw = _pod(pods, _MON, "federation-gateway")
    ext = {"ns": None, "ip": _EXTERNAL_IP, "labels": {}}
    other_ns = {"ns": "kube-system", "labels": {"k8s-app": "kube-dns"}}
    assert not _allowed(policies, gw, "egress", ext, 443, "TCP"), "gateway reaches the internet"
    assert not _allowed(policies, gw, "egress", other_ns, _STORE_PORT, "TCP"), "upstream port open to other namespaces"
    assert not _allowed(policies, gw, "egress", _pod(pods, _MON, "prometheus"), 9090, "TCP"), "gateway reaches prometheus"
