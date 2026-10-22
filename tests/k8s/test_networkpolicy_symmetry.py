"""NetworkPolicy両-end symmetry contract for the raw k8s manifests (#1203).

WHY: a NetworkPolicy connection needs BOTH ends to allow it — the source pod's
egress rules AND the destination pod's ingress rules. Nothing checked that, and
the two halves live in different documents (sometimes different files), so an
asymmetric pair reads as "wired" on either side alone. `kubectl apply` accepts
it, no controller complains, and the only symptom is a connection that silently
never happens.

That is not hypothetical: `allow-maintenance-scheduler-egress` permitted the
maintenance-scheduler CronJob to reach Alertmanager on 9093, while
`allow-alertmanager-ingress` admitted only `app: prometheus`. Every silence the
scheduler tried to create for a planned-maintenance window was dropped at the
destination — and the failure is self-concealing, because the observable effect
is "alerts fired during maintenance", which reads as an alerting-rule problem.

SCOPE BOUNDARY: raw manifests under k8s/ only. Helm charts also ship
NetworkPolicies (federation-gateway, victorialogs, da-portal, ...) but those are
Go templates, not YAML — they cannot be parsed here, and their peers are values-
dependent. This gate deliberately does NOT claim to cover them; a chart-side
asymmetry is out of scope, not proven absent.

WHAT IS CHECKED: only egress peers that name an in-cluster `podSelector`.
`ipBlock` peers are external by construction (no ingress side to check), and a
bare `namespaceSelector` peer allows a whole namespace, which cannot be resolved
to a specific destination workload.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_K8S_DIR = _REPO_ROOT / "k8s"

# Non-vacuity anchors. LITERAL on purpose — deriving them from the same walk
# they guard would make an empty walk satisfy them trivially (the hole #1283
# fixed in the nightly matrix guards).
_EXPECTED_POLICIES = frozenset({
    "default-deny-all",
    "allow-alertmanager-ingress",
    "allow-alertmanager-egress",
    "allow-maintenance-scheduler-egress",
})
_EXPECTED_WORKLOADS = frozenset({"alertmanager", "prometheus", "maintenance-scheduler"})

_WORKLOAD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "CronJob", "Job"}

def _edge_key(policy: str, dst_ns: str | None, selector: dict | None) -> tuple:
    """The identity of one egress edge: policy, destination ns AND destination pods.

    (policy, ns) alone is not an identity: one policy can name two different
    pod selectors in the same namespace, and an exception or lookup keyed
    without the selector would let one of them stand in for the other (#3270).
    matchLabels are canonicalised to a sorted tuple so key order in the YAML
    does not matter. matchExpressions are refused, not dropped: a key that
    ignored them would make two different selectors collide, which is the
    masking this key exists to prevent.
    """
    selector = selector or {}
    if _uses_match_expressions(selector):
        pytest.fail(
            f"{policy} -> ns={dst_ns}: _edge_key cannot identify a podSelector "
            f"with matchExpressions ({selector}) — teach it (and _selects) about "
            "them instead of letting two selectors collide")
    return (policy, dst_ns,
            tuple(sorted((selector.get("matchLabels") or {}).items())))


def _uses_match_expressions(selector: dict | None) -> bool:
    return bool(selector and selector.get("matchExpressions"))


# Raw egress edges whose destination is deployed ONLY by a helm chart, so no
# workload under k8s/ can resolve it. _edge_key(policy, destination ns,
# destination podSelector) -> (chart, extra helm args, reason). Each is still checked end to end, by rendering the
# chart into the destination namespace: the rendered pods must match the
# egress selector AND the rendered ingress must admit the raw source workload
# (test_helm_resident_destinations_are_symmetric). Exit-locked: an entry whose
# edge resolves to a raw workload again fails, so the map can only shrink.
_HELM_RESIDENT_DESTINATIONS = {
    _edge_key("allow-threshold-govern-egress", "tenant-api",
              {"matchLabels": {"app.kubernetes.io/name": "tenant-api"}}): (
        "helm/tenant-api",
        # the chart refuses to render until writeBack.mode is chosen (#2855);
        # the shared baseline adds no NetworkPolicy or pod label
        ["-f", str(_REPO_ROOT / "tests" / "helm" / "tenant_api_render_baseline.yaml")],
        "#1594: the raw tenant-api manifests were removed; helm/tenant-api is "
        "the only deployment source",
    ),
}


def _pod_template_labels(doc: dict) -> dict | None:
    """The labels the pods of *doc* will actually carry, or None if not a workload."""
    kind = doc.get("kind")
    if kind not in _WORKLOAD_KINDS:
        return None
    spec = doc.get("spec") or {}
    if kind == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
    template = (spec.get("template") or {}).get("metadata") or {}
    return template.get("labels") or {}


def _load():
    policies, workloads = [], []
    for path in sorted(_K8S_DIR.rglob("*.yaml")):
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError as exc:      # fail loud: an unparsable manifest
            pytest.fail(f"{path} is not parsable YAML: {exc}")
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") == "NetworkPolicy":
                policies.append((path, doc))
                continue
            labels = _pod_template_labels(doc)
            if labels is not None:
                workloads.append({
                    "name": doc["metadata"]["name"],
                    "namespace": doc["metadata"].get("namespace"),
                    "labels": labels,
                })
    return policies, workloads


def _selects(selector: dict | None, labels: dict) -> bool:
    """matchLabels-only subset test.

    `matchExpressions` is rejected by the caller rather than silently ignored:
    treating an unsupported selector as "does not match" would turn a real
    asymmetry into a green test, which is the failure shape this file exists
    to prevent.
    """
    if selector is None:
        return False
    return all(labels.get(k) == v for k, v in (selector.get("matchLabels") or {}).items())


def _ports(rule: dict) -> set[str]:
    return {str(p.get("port")) for p in (rule.get("ports") or []) if p.get("port") is not None}


# Three outcomes that must NOT collapse into one another. Returning None for
# all of them (the first version did) makes "every namespace" and "a selector
# shape this code cannot read" compare equal to each other AND to a workload
# that declares no namespace — a false resolution where an unresolvable peer
# was the honest answer (CodeRabbit, PR #1290).
_NS_ALL = "<all-namespaces>"
_NS_UNSUPPORTED = "<unsupported-namespaceSelector>"


def _peer_namespace(peer: dict, policy_namespace: str | None) -> str | None:
    """Which namespace a peer's podSelector resolves in.

    A peer with no `namespaceSelector` means "same namespace as the policy" —
    getting this wrong is how a cross-namespace pair looks symmetric when it is
    not. An EMPTY selector (`{}`) means every namespace, which is a real admit,
    not an unknown. Any other label form is something this matchLabels-only
    reader cannot evaluate, and says so instead of guessing.
    """
    ns_sel = peer.get("namespaceSelector")
    if ns_sel is None:
        return policy_namespace
    if not ns_sel:                       # `namespaceSelector: {}` — all namespaces
        return _NS_ALL
    name = (ns_sel.get("matchLabels") or {}).get("kubernetes.io/metadata.name")
    return name if name else _NS_UNSUPPORTED


def _intra_cluster_egress_edges(policies, workloads):
    """(policy, source workloads, destination workloads, ports) per resolvable peer."""
    edges = []
    for path, policy in policies:
        pol_ns = policy["metadata"].get("namespace")
        src_sel = policy["spec"].get("podSelector") or {}
        sources = [w for w in workloads
                   if w["namespace"] == pol_ns and _selects(src_sel, w["labels"])]
        for rule in policy["spec"].get("egress") or []:
            ports = _ports(rule)
            for peer in rule.get("to") or []:
                if peer.get("podSelector") is None:
                    continue                      # ipBlock / whole-namespace: out of scope
                dst_ns = _peer_namespace(peer, pol_ns)
                dsts = [w for w in workloads
                        if w["namespace"] == dst_ns
                        and _selects(peer["podSelector"], w["labels"])]
                edges.append({
                    "key": _edge_key(policy["metadata"]["name"], dst_ns,
                                     peer["podSelector"]),
                    "policy": policy["metadata"]["name"], "path": path,
                    "selector": peer["podSelector"], "dst_ns": dst_ns,
                    "sources": sources, "destinations": dsts, "ports": ports,
                })
    return edges


def _edges_by_key(policies, workloads) -> dict[tuple, list[dict]]:
    """Egress edges grouped by _edge_key. A list, not a single edge: two rules
    of one policy may name the same peer (e.g. on different ports), and a plain
    dict comprehension would silently keep only the last of them."""
    grouped: dict[tuple, list[dict]] = {}
    for e in _intra_cluster_egress_edges(policies, workloads):
        grouped.setdefault(e["key"], []).append(e)
    return grouped


def _ingress_admits(policies, dst, src_workloads, ports) -> bool:
    for _path, policy in policies:
        if "Ingress" not in (policy["spec"].get("policyTypes") or []):
            continue
        if policy["metadata"].get("namespace") != dst["namespace"]:
            continue
        if not _selects(policy["spec"].get("podSelector") or {}, dst["labels"]):
            continue
        for rule in policy["spec"].get("ingress") or []:
            in_ports = _ports(rule)
            if ports and in_ports and not (ports & in_ports):
                continue
            for peer in rule.get("from") or []:
                pod_sel = peer.get("podSelector")
                peer_ns = _peer_namespace(peer, policy["metadata"].get("namespace"))
                if pod_sel is None:
                    # An ipBlock peer admits IP ranges, not in-cluster pods —
                    # treating "no podSelector" as "admits anything" made every
                    # such edge read symmetric, which is the exact direction
                    # this gate exists to prevent (CodeRabbit, PR #1290).
                    if peer.get("namespaceSelector") is None:
                        continue
                    if peer_ns == _NS_ALL:
                        return True
                    # whole-namespace admit: the SOURCE's namespace still has to
                    # be the one admitted. Skipping this check let an ingress
                    # rule admitting namespace `foo` satisfy a source in `bar`.
                    if any(src["namespace"] == peer_ns for src in src_workloads):
                        return True
                    continue
                if peer_ns in (_NS_ALL, _NS_UNSUPPORTED):
                    # podSelector scoped by a selector we cannot evaluate: do not
                    # guess in the permissive direction.
                    continue
                for src in src_workloads:
                    if src["namespace"] == peer_ns and _selects(pod_sel, src["labels"]):
                        return True
    return False


class TestNetworkPolicySymmetry:
    def test_discovery_is_not_vacuous(self):
        policies, workloads = _load()
        names = {p["metadata"]["name"] for _, p in policies}
        missing = _EXPECTED_POLICIES - names
        assert not missing, (
            f"NetworkPolicy/-ies {sorted(missing)} no longer found under k8s/. If "
            "they were renamed or moved, update the anchor — otherwise every "
            "assertion below passes vacuously.")
        wl = {w["name"] for w in workloads}
        assert _EXPECTED_WORKLOADS <= wl, (
            f"workload(s) {sorted(_EXPECTED_WORKLOADS - wl)} not discovered; the "
            "pod-template label walk is broken and peers cannot be resolved")

    def test_no_selector_uses_unsupported_match_expressions(self):
        policies, _ = _load()
        offenders = []
        for path, policy in policies:
            selectors = [policy["spec"].get("podSelector")]
            for direction in ("ingress", "egress"):
                for rule in policy["spec"].get(direction) or []:
                    for peer in (rule.get("from") or []) + (rule.get("to") or []):
                        selectors += [peer.get("podSelector"), peer.get("namespaceSelector")]
            if any(_uses_match_expressions(s) for s in selectors):
                offenders.append(f'{path.name}:{policy["metadata"]["name"]}')
        assert offenders == [], (
            "this contract resolves selectors by matchLabels only; a "
            "matchExpressions selector would be silently read as 'matches "
            "nothing' and could turn a real asymmetry green. Teach _selects "
            f"about matchExpressions before introducing one: {offenders}")

    def test_every_egress_pod_selector_resolves_to_a_workload(self):
        policies, workloads = _load()
        dangling = [
            f'{e["policy"]} -> {e["selector"].get("matchLabels")} in ns={e["dst_ns"]}'
            for e in _intra_cluster_egress_edges(policies, workloads)
            if not e["destinations"]
            and e["key"] not in _HELM_RESIDENT_DESTINATIONS
        ]
        assert dangling == [], (
            "these egress rules name a pod selector that matches no workload "
            "manifest under k8s/ — either the destination moved/was renamed (the "
            "rule is now dead) or it lives in a helm chart (out of this gate's "
            f"scope, and the rule cannot be verified here): {dangling}")

    def test_edge_key_distinguishes_destination_pods(self):
        a = _edge_key("p", "ns", {"matchLabels": {"app": "a", "tier": "x"}})
        assert a == _edge_key("p", "ns", {"matchLabels": {"tier": "x", "app": "a"}})
        # same policy, same namespace, different pods: must NOT collide (#3270)
        assert a != _edge_key("p", "ns", {"matchLabels": {"app": "b", "tier": "x"}})
        with pytest.raises(pytest.fail.Exception, match="matchExpressions"):
            _edge_key("p", "ns", {"matchExpressions": [
                {"key": "app", "operator": "In", "values": ["a"]}]})

    def test_helm_resident_entries_are_exit_locked(self):
        policies, workloads = _load()
        edges = _edges_by_key(policies, workloads)
        for key in _HELM_RESIDENT_DESTINATIONS:
            assert key in edges, (
                f"{key} is listed as a helm-resident destination but no such raw "
                "egress edge exists any more — delete the entry")
            assert not any(e["destinations"] for e in edges[key]), (
                f"{key} now resolves to a raw workload under k8s/ — delete it from "
                "_HELM_RESIDENT_DESTINATIONS so the raw symmetry test covers it")

    @pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")
    def test_helm_resident_destinations_are_symmetric(self):
        policies, workloads = _load()
        edges = _edges_by_key(policies, workloads)
        for key, (chart, extra, _why) in sorted(_HELM_RESIDENT_DESTINATIONS.items()):
            policy, ns, _labels = key
            for edge in edges[key]:
                self._assert_helm_edge_symmetric(edge, policy, ns, chart, extra)

    @staticmethod
    def _assert_helm_edge_symmetric(edge, policy, ns, chart, extra):
        assert edge["sources"], f"{policy}: no raw source workload resolved"
        out = subprocess.run(
            ["helm", "template", "t", str(_REPO_ROOT / chart), "-n", ns, *extra],
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert out.returncode == 0, out.stderr
        docs = [d for d in yaml.safe_load_all(out.stdout) if isinstance(d, dict)]
        r_policies = [(Path(chart), d) for d in docs if d.get("kind") == "NetworkPolicy"]
        dsts = []
        for d in docs:
            labels = _pod_template_labels(d)
            if labels is not None and _selects(edge["selector"], labels):
                dsts.append({"name": d["metadata"]["name"], "namespace": ns,
                             "labels": labels})
        assert dsts, (
            f"{policy} selects {edge['selector']} in ns={ns}, but no pod "
            f"{chart} renders carries those labels — the egress rule is dead")
        for dst in dsts:
            assert _ingress_admits(r_policies, dst, edge["sources"], edge["ports"]), (
                f"{policy} -> {chart} {dst['name']}: the chart's ingress does "
                f"not admit {[w['name'] for w in edge['sources']]} on "
                f"{sorted(edge['ports']) or '<all>'}")

    @pytest.mark.parametrize("peers,src_ns,admits", [
        # ipBlock admits IP ranges, not in-cluster pods. The first version read
        # "no podSelector" as "admits anything" and returned True here, making
        # every such edge look symmetric (CodeRabbit, PR #1290).
        ([{"ipBlock": {"cidr": "10.0.0.0/8"}}], "monitoring", False),
        # whole-namespace admit must still be the SOURCE's namespace
        ([{"namespaceSelector":
           {"matchLabels": {"kubernetes.io/metadata.name": "foo"}}}],
         "monitoring", False),
        ([{"namespaceSelector":
           {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}}],
         "monitoring", True),
        # `{}` really is every namespace — an admit, not an unknown
        ([{"namespaceSelector": {}}], "somewhere-else", True),
        # a namespaceSelector this matchLabels-only reader cannot evaluate must
        # not be guessed in the permissive direction
        ([{"namespaceSelector": {"matchLabels": {"name": "monitoring"}},
           "podSelector": {"matchLabels": {"component": "x"}}}],
         "monitoring", False),
        ([{"namespaceSelector":
           {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}},
           "podSelector": {"matchLabels": {"component": "x"}}}],
         "monitoring", True),
    ])
    def test_ingress_admits_peer_shapes(self, peers, src_ns, admits):
        dst = {"name": "am", "namespace": "monitoring",
               "labels": {"app": "alertmanager"}}
        sources = [{"name": "src", "namespace": src_ns,
                    "labels": {"component": "x"}}]
        policies = [(Path("synthetic.yaml"), {
            "metadata": {"name": "p", "namespace": "monitoring"},
            "spec": {"podSelector": {"matchLabels": {"app": "alertmanager"}},
                     "policyTypes": ["Ingress"],
                     "ingress": [{"from": peers, "ports": [{"port": 9093}]}]}})]
        assert _ingress_admits(policies, dst, sources, {"9093"}) is admits

    def test_intra_cluster_egress_has_matching_ingress(self):
        policies, workloads = _load()
        broken = []
        for edge in _intra_cluster_egress_edges(policies, workloads):
            if not edge["destinations"]:
                continue                      # covered by the test above
            for dst in edge["destinations"]:
                if not _ingress_admits(policies, dst, edge["sources"], edge["ports"]):
                    broken.append(
                        f'{edge["policy"]} ('
                        f'{[w["name"] for w in edge["sources"]] or "<no source workload>"}'
                        f') -> {dst["name"]} ns={dst["namespace"]} ports='
                        f'{sorted(edge["ports"]) or "<all>"}')
        assert broken == [], (
            "egress is allowed but the destination's ingress does not admit the "
            "source — a NetworkPolicy connection needs BOTH ends, so this "
            "traffic is dropped at the destination and the symptom appears "
            f"somewhere else entirely: {broken}")
