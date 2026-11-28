"""Where each metric a rule reads comes from — and whether a Prometheus Operator install has that source.

Two readers, one table (#1595 D1b):

* ``scripts/tools/lint/check_scrape_reachability.py`` resolves each metric
  family to its source and judges that source against this repo's raw scrape
  config (``k8s/03-monitoring/configmap-prometheus.yaml``). It re-exports the
  table under its historical private names (``_FAMILY_TABLE``,
  ``_FAMILY_EXACT``, ``_resolve_family``) — the same objects, so there is one
  copy.
* ``da-tools operator-generate`` asks the OTHER question of the same sources:
  does a kube-prometheus-stack install scrape them? It emits the platform pack
  (``_lib_consumer_read.PLATFORM_PACKS``) minus the rules
  :func:`operator_split` finds a source missing for, and names those rules.

The table lives here, not in the lint, because the operator question is
answered at run time inside the da-tools image, which ships ``scripts/tools``
flat and does not ship the lints (``components/da-tools/app/build.sh``).
Which metrics a rule reads, and which `job` each read pins, come from
Prometheus' parse tree (``_lib_consumer_read.promql_shape``), never from the
text of the expression (#4219).
"""
from __future__ import annotations

import _lib_consumer_read  # (#4137, #4219) it does not import this module

# ── the charts behind the helm-service families ─────────────────────────────
# A chart is what a helm-service family's target comes from: its Service carries
# `dynamic-alerting.io/scrape: platform` and is rendered into Release.Namespace.
THRESHOLD_EXPORTER_CHART = "helm/threshold-exporter"
VECTOR_CHART = "helm/vector"
TENANT_API_CHART = "helm/tenant-api"
FEDERATION_GATEWAY_CHART = "helm/federation-gateway"
FEDERATION_RECONCILER_CHART = "helm/federation-reconciler"

# ── metric-family → expected source (declarative, longest-prefix wins) ──────
# kind:
#   static           scraped by a static_configs self-job (value = job name)
#   service          repo-pinned Service manifest (value = (namespace, name))
#   helm-service     helm chart Service in Release.Namespace (value = chart path)
#   name-filtered    node-role job with a __name__ allowlist (value = job name)
# Families not listed here fall through to the tenant-exporter face (the
# is_federated_exporter_metric SSOT) and then UNKNOWN-SOURCE (scrape gate) /
# "no modelled source" (operator_split).
# FAMILY_EXACT (below) is consulted FIRST — see resolve_family.
FAMILY_TABLE: list[tuple[str, str, object]] = [
    ("prometheus_", "static", "prometheus"),
    ("alertmanager_", "service", ("monitoring", "alertmanager")),
    ("tenant_api_", "helm-service", TENANT_API_CHART),
    # NOTE `kubelet_` before `kube_` is load-bearing — longest-prefix matching
    # protects it, but keep the order human-readable too (substring trap).
    ("kubelet_", "name-filtered", "kubelet-volume-stats"),
    ("kube_", "service", ("monitoring", "kube-state-metrics")),
    ("container_", "name-filtered", "kubelet-cadvisor"),
    ("vector_", "helm-service", VECTOR_CHART),
    # threshold-exporter's two config-driven families (#1285). Their VALUES
    # come from conf.d; their SERIES come from the threshold-exporter target
    # like everything else it registers — see the scrape gate's docstring.
    ("da_", "helm-service", THRESHOLD_EXPORTER_CHART),
    ("user_", "helm-service", THRESHOLD_EXPORTER_CHART),
    # NOTE `da_generator_` is NOT threshold-exporter's: it is the generator
    # container's (`da-tools generator-service`, ADR-036 §3), which has only a
    # Unix socket — tenant-api appends its /metrics to its own (#2903), so the
    # series come from the tenant-api target. Longest-prefix matching routes
    # it here before `da_` above; da_generator_build_info is the generator /
    # da-guard version metric (ADR-036 §6).
    ("da_generator_", "helm-service", TENANT_API_CHART),
    # The revocation reconciler's own gauges
    # (scripts/tools/ops/_federation_revocation_reconciler.py). Its Service carries the platform
    # scrape label since #1595 D1b; before that it was scraped only when
    # installed into `monitoring` and these were ledgered UNKNOWN-SOURCE.
    ("federation_revocation_", "helm-service", FEDERATION_RECONCILER_CHART),
]

# ── EXACT-NAME families (checked before FAMILY_TABLE) ───────────────────────
# For series whose name shares a prefix with metrics from a DIFFERENT source,
# so no prefix can route them without collateral.
#
# ⛔ DO NOT "simplify" the tenant_* rows into a `tenant_` prefix entry (#1285):
# tenant_metadata_info / tenant_expected_exporter are threshold-exporter's,
# tenant_federation_requests_total / tenant_log_query_requests_total are the
# federation gateway's audit sidecar's. A prefix would hand one component's
# series to the other's Service. Likewise the federation_gateway_* rows are
# emitted by the RECONCILER (it counts the gateway's reload refusals off the
# gateway's log stream, #1235/#1236), so a `federation_gateway_` prefix would
# route a future gateway-emitted metric to the wrong chart.
FAMILY_EXACT: dict[str, tuple[str, object]] = {
    # Same collector.go / same registry / same /metrics as the da_*+user_* rows.
    "tenant_metadata_info": ("helm-service", THRESHOLD_EXPORTER_CHART),
    "tenant_expected_exporter": ("helm-service", THRESHOLD_EXPORTER_CHART),
    # The gateway's audit-metrics sidecar
    # (helm/federation-gateway/files/audit-metrics.vector.yaml).
    "tenant_federation_requests_total": ("helm-service", FEDERATION_GATEWAY_CHART),
    "tenant_log_query_requests_total": ("helm-service", FEDERATION_GATEWAY_CHART),
    # Reconciler-emitted, despite the prefix (see above).
    "federation_gateway_revocation_load_errors": ("helm-service", FEDERATION_RECONCILER_CHART),
    "federation_gateway_revoked_set_reload_rejected": ("helm-service", FEDERATION_RECONCILER_CHART),
    "federation_gateway_revoked_set_missing": ("helm-service", FEDERATION_RECONCILER_CHART),
}


def resolve_family(metric: str):
    """→ (matched key, kind, spec) or None. Exact beats prefix, then longest
    prefix wins. Exact-first is load-bearing, not a tie-break nicety: an exact
    row exists precisely because the name's prefix belongs to someone else."""
    exact = FAMILY_EXACT.get(metric)
    if exact is not None:
        kind, spec = exact
        return (metric, kind, spec)
    best = None
    for prefix, kind, spec in FAMILY_TABLE:
        if metric.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, kind, spec)
    return best


# ── what a rule reads (#4219) ───────────────────────────────────────────────
def job_matchers(tree: dict) -> "dict[str, list[tuple[str, str] | None]]":
    """Per metric a parse tree reads: for each selector reading it, in tree
    order, its `job` matcher as (op, value), or None when it has none."""
    out: "dict[str, list[tuple[str, str] | None]]" = {}
    for node in _lib_consumer_read.shape_nodes(tree):
        matchers = node.get("matchers") or []
        jobs = [(m["type"], m["value"]) for m in matchers if m["name"] == "job"]
        for m in matchers:
            # every name the selector pins, as shape_metric_names reads them
            if m["name"] == "__name__" and m["type"] == "=":
                out.setdefault(m["value"], []).append(jobs[0] if jobs else None)
    return out


# ── the Prometheus Operator face (#1595 D1b) ────────────────────────────────
# In operator mode (kube-prometheus-stack) Prometheus scrapes only what a
# ServiceMonitor / PodMonitor selects. The sources below are the ones such an
# install has, keyed by the FAMILY_TABLE source each family resolves to.

#: Sources kube-prometheus-stack scrapes itself: its own Prometheus and
#: Alertmanager, kube-state-metrics, and the kubelet (cAdvisor + volume
#: stats). The series exist; the JOB NAMES do not match this repo's raw
#: config (kube-prometheus-stack names them after its own Services or a
#: jobLabel), so a selector that pins `job` on one of them is not portable.
OPERATOR_STACK_SOURCES: frozenset = frozenset({
    ("static", "prometheus"),
    ("service", ("monitoring", "alertmanager")),
    ("service", ("monitoring", "kube-state-metrics")),
    ("name-filtered", "kubelet-cadvisor"),
    ("name-filtered", "kubelet-volume-stats"),
})

#: The platform charts that render a ServiceMonitor, and the `job` value each
#: of its scraped Services gets. The ServiceMonitors set no `jobLabel`, so the
#: job is the Service name — the same value the raw `platform-components` job
#: gives it, which is why a `job="tenant-api"` selector is portable. Pinned
#: against the rendered charts (templates/servicemonitor.yaml and the
#: platform-labelled Services) by tests/helm/test_platform_servicemonitors.py:
#: a chart that drops its ServiceMonitor, or renames a Service, turns it red.
CHART_SERVICEMONITOR_JOBS: dict[str, frozenset] = {
    THRESHOLD_EXPORTER_CHART: frozenset({"threshold-exporter"}),
    TENANT_API_CHART: frozenset({"tenant-api"}),
    VECTOR_CHART: frozenset({"vector-metrics", "vector-projection-gate-metrics"}),
    FEDERATION_GATEWAY_CHART: frozenset({"federation-gateway"}),
    FEDERATION_RECONCILER_CHART: frozenset({"federation-reconciler"}),
}

#: The scrape job every tenant rule pack selects (`up{job="tenant-exporters"}`),
#: and the one name for it (#3720). The repo's raw config is that job
#: (k8s/03-monitoring/configmap-prometheus.yaml); the BYO, Operator and
#: Federation docs teach customers the same job — the Operator ServiceMonitor
#: sets it with a `job` relabeling, because without one prometheus-operator
#: names the job after the Service. Both are pinned to
#: check_scrape_reachability.tenant_scrape_contract by the scrape gate and
#: tests/k8s/test_tenant_scrape_contract.py.
TENANT_EXPORTER_JOB = "tenant-exporters"

#: Series every scrape target yields with its target's labels and no others
#: of its own (#4137): Prometheus' per-scrape series and the families the
#: Prometheus client libraries register in every process. The platform's
#: own jobs (prometheus, kube-state-metrics, alertmanager, the charts') yield
#: them too, without `tenant`, so a family the table does not list is no
#: proof the series carry it: which jobs a selector reads decides.
PER_TARGET_EXACT = frozenset({"up"})
PER_TARGET_PREFIXES = ("scrape_", "go_", "process_", "promhttp_", "net_conntrack_")

#: Series Prometheus writes per alert, labelled as the alert is (#4137):
#: whether they carry `tenant` is each alert's business.
ALERT_SERIES = frozenset({"ALERTS", "ALERTS_FOR_STATE"})

#: ⛔ THE series Prometheus itself manufactures (no exporter, no /metrics
#: line): `up` per scrape target, the alert series per alert. `up` carries
#: the target's job, so its source is whatever job its selector names. The
#: scrape gate takes none of them as a leaf, and the split validator takes
#: none of them as a dangling central reference (#4219: they were unseen
#: until metric names came from the parse tree — a capitalised name was
#: skipped).
MANUFACTURED = PER_TARGET_EXACT | ALERT_SERIES

#: The label the scrape writes per tenant: the tenant exporters' job writes it
#: from the namespace (k8s/03-monitoring/configmap-prometheus.yaml; the
#: scrape gate's TENANT-* checks pin it).
TENANT_LABEL = "tenant"


def tenant_presence(metric: str) -> "str | None":
    """⛔ THE one answer to "do the series of this raw metric carry
    ``tenant``" (#4137), as ``da-read promql-label-flow`` takes it:
    ``"without"`` — a family of a static, service or name-filtered source
    (prometheus, alertmanager, kube-state-metrics, cAdvisor, the kubelet's
    volume stats: jobs that write no tenant, pinned by
    tests/lint/test_check_scrape_reachability.py). The infrastructure
    families name the tenant only as ``namespace``, on every install (#4178):
    a rule that needs ``tenant`` derives it from ``namespace``
    (``_lib_consumer_read.LABEL_ALIASES``); ``"by-job"`` — a per-target
    series (``PER_TARGET_*``); ``"unknown"`` — an alert's series; ``None`` —
    taken to carry it: the tenant exporters' job writes it on every series
    (the scrape contract), and a chart's own families are the exporter's
    business, per metric (the residual this answer does not model). A
    recording rule's name is not a raw metric's."""
    if metric in ALERT_SERIES:
        return "unknown"
    if metric in PER_TARGET_EXACT or metric.startswith(PER_TARGET_PREFIXES):
        return "by-job"
    fam = resolve_family(metric)
    if fam is not None and fam[1] in ("static", "service", "name-filtered"):
        return "without"
    return None


#: kube-state-metrics families of objects with no namespace (#4226): their
#: series carry neither ``tenant`` nor ``namespace``, so no pin selects one
#: tenant's. Upstream's cluster-scoped resources (kube-state-metrics
#: docs/metrics: Node, PersistentVolume, StorageClass, ClusterRole(Binding),
#: CertificateSigningRequest, the webhook configurations, IngressClass,
#: VolumeAttachment). `kube_persistentvolume_` is not a prefix of
#: `kube_persistentvolumeclaim_` (the claim is namespaced).
KSM_CLUSTER_SCOPED_PREFIXES = (
    "kube_node_", "kube_persistentvolume_", "kube_storageclass_",
    "kube_clusterrole_", "kube_clusterrolebinding_",
    "kube_certificatesigningrequest_", "kube_mutatingwebhookconfiguration_",
    "kube_validatingwebhookconfiguration_", "kube_ingressclass_",
    "kube_volumeattachment_",
)

#: The source kube-state-metrics' families resolve to (FAMILY_TABLE).
_KSM_SOURCE = ("service", ("monitoring", "kube-state-metrics"))


def pin_form(metric: str) -> "tuple[str | None, str | None]":
    """⛔ THE one answer to "how is a selector of the raw *metric* pinned to
    one tenant" (#4226, migrate-rule): → ``("tenant", None)`` — its series
    carry ``tenant`` (``tenant_presence`` None); ``("namespace", None)`` —
    kube-state-metrics' series of a namespaced object, or an infrastructure
    family's (cAdvisor, the kubelet's volume stats, #4178), which name the
    tenant by ``namespace`` and carry no ``tenant``; ``(None, why)`` — no pin
    selects one tenant's series (a cluster-scoped object, Prometheus' or
    Alertmanager's own, a per-target series whose job decides, an alert's)."""
    presence = tenant_presence(metric)
    if presence is None:
        return "tenant", None
    if presence == "by-job":
        return None, ("a series every scrape target yields: whether it carries tenant is "
                      "its job's business, not this metric's")
    if presence == "unknown":
        return None, "an alert's own series: whether it carries tenant is each alert's business"
    fam = resolve_family(metric)
    if fam is not None and fam[1] == "name-filtered":
        # cAdvisor's and the kubelet's volume stats: the tenant is the
        # namespace, on every install (#4178).
        return "namespace", None
    if fam is not None and (fam[1], fam[2]) == _KSM_SOURCE:
        if metric.startswith(KSM_CLUSTER_SCOPED_PREFIXES):
            return None, ("kube-state-metrics' series of a cluster-scoped object: they carry "
                          "no namespace and no tenant")
        return "namespace", None
    return None, ("the platform's own series (its job writes no tenant and names no "
                  "tenant's namespace)")


def federated_infra_families() -> "tuple[str, ...]":
    """The name-filtered families (cAdvisor's `container_`, the kubelet's
    `kubelet_`): infrastructure series a tenant's recipe may read, which carry
    the tenant only as ``namespace`` (#4178). kube-state-metrics' `kube_` is
    not among them: its series are many, and no central rule reads them raw."""
    return tuple(sorted(p for p, kind, _src in FAMILY_TABLE if kind == "name-filtered"))


def federated_infra_selector() -> str:
    """The `match[]` selector a central Prometheus federates the
    ``federated_infra_families`` with, beside ``{tenant!=""}``: the tenant
    namespaces' series, whose tenant the recipe compiler derives centrally
    (docs/integration/federation-integration.md §4, pinned by
    tests/rulepacks/test_exporter_metric_prefixes.py)."""
    alt = "|".join(p.rstrip("_") for p in federated_infra_families())
    return '{__name__=~"(' + alt + ')_.+", namespace=~"db-.+"}'


def federated_infra_keep_regex() -> str:
    """The remote-write keep over ``[tenant, __name__, namespace]`` that sends
    what ``{tenant!=""}`` and ``federated_infra_selector`` select together."""
    alt = "|".join(p.rstrip("_") for p in federated_infra_families())
    return ".+;.*;.*|;(" + alt + ")_.+;db-.+"


def label_flow(label: str, exprs: "list[str]", records: "dict[str, list[dict]] | None" = None):
    """``_lib_consumer_read.promql_label_flow`` with what the scrape says
    about *label* (#4137): for ``tenant``, every raw metric the expressions
    and *records* read (``_lib_consumer_read.promql_metric_names``) judged
    by ``tenant_presence``, and the tenant
    exporters' job as the one whose every target carries it. Any other label
    is told nothing: every raw metric is taken to carry it."""
    metrics: dict[str, str] = {}
    jobs: tuple[str, ...] = ()
    if label == TENANT_LABEL:
        jobs = (TENANT_EXPORTER_JOB,)
        read = list(exprs) + [d.get("expr", "") for defs in (records or {}).values() for d in defs]
        for names in _lib_consumer_read.promql_metric_names(read, raise_refused=True):
            for name in names:
                if name not in (records or {}):
                    presence = tenant_presence(name)
                    if presence is not None:
                        metrics[name] = presence
    return _lib_consumer_read.promql_label_flow(label, exprs, records, metrics=metrics, jobs=jobs)


def operator_jobs() -> frozenset:
    """Every `job` value an Operator install's ServiceMonitors produce: each
    platform chart's, plus the tenant exporters' that the Operator doc teaches
    (its `job` relabeling to TENANT_EXPORTER_JOB, #3720)."""
    return frozenset().union(*CHART_SERVICEMONITOR_JOBS.values(), {TENANT_EXPORTER_JOB})


def operator_unportable(tree: dict, produced: "dict[str, list[str]] | None" = None) -> list[str]:
    """Why the expression whose parse tree is *tree*
    (``_lib_consumer_read.promql_shape``) cannot run under a kube-prometheus-stack install, or [] when
    every metric it reads has a source there.

    *produced* maps a recording-rule output to the reasons its own rule is not
    portable ([] = portable), so a rule built on a recording rule inherits it.

    ⚠️ Of the labels a selector matches, only `job` is modelled. Other labels
    that exist in one mode only are not: the tenant exporters' `tenant`
    relabel (the docs teach it, nothing here checks it), or a `namespace="monitoring"` pin on an object this
    repo's raw manifests put there (CronJobLastRunFailed,
    ConfigReloader*) — such a rule counts as portable
    because its series exist, though it may watch nothing under the Operator.
    """
    produced = produced or {}
    reasons: list[str] = []
    reads = job_matchers(tree)
    for metric in sorted(reads):
        if metric in produced:
            if produced[metric]:
                reasons.append(f"{metric}: its recording rule is not portable")
            continue
        jobs = reads[metric]
        if metric in ALERT_SERIES:
            continue
        if metric == "up":
            have = operator_jobs()
            for job in jobs:
                if job is None or job[0] != "=" or job[1] not in have:
                    shown = "no job matcher" if job is None else f'job{job[0]}"{job[1]}"'
                    reasons.append(
                        f"up ({shown}): no platform chart's ServiceMonitor produces "
                        f"that job (ServiceMonitor jobs: {sorted(have)})")
            continue
        fam = resolve_family(metric)
        if fam is None:
            # A tenant exporter's own series, read under the tenant-exporters
            # job (#3771: the liveness alerts read each kind's presence metric
            # inline): the Operator doc's ServiceMonitor scrapes those targets
            # as that job (#3720), so they exist in an Operator install too.
            if jobs and all(job == ("=", TENANT_EXPORTER_JOB) for job in jobs):
                continue
            reasons.append(f"{metric}: no modelled source (FAMILY_TABLE / FAMILY_EXACT)")
            continue
        _key, kind, spec = fam
        if kind == "helm-service":
            chart_jobs = CHART_SERVICEMONITOR_JOBS.get(spec)
            if chart_jobs is None:
                reasons.append(f"{metric}: {spec} renders no ServiceMonitor")
                continue
            for job in jobs:
                if job is not None and (job[0] != "=" or job[1] not in chart_jobs):
                    reasons.append(
                        f'{metric}{{job{job[0]}"{job[1]}"}}: {spec}\'s ServiceMonitor '
                        f"produces only job {sorted(chart_jobs)}")
            continue
        if (kind, spec) not in OPERATOR_STACK_SOURCES:
            reasons.append(f"{metric}: source {kind} {spec!r} is not scraped by "
                           "kube-prometheus-stack")
            continue
        for job in jobs:
            if job is not None:
                reasons.append(
                    f'{metric}{{job{job[0]}"{job[1]}"}}: kube-prometheus-stack names '
                    f"the job scraping {kind} {spec!r} after its own Service, not "
                    "this repo's raw scrape job")
    return list(dict.fromkeys(reasons))


def operator_split(groups: list) -> tuple[list, list[tuple[str, list[str]]]]:
    """→ (groups to emit, [(rule name, reasons)] left out).

    A rule is left out when :func:`operator_unportable` names a reason; a group
    left with no rule is dropped. Rule order and content are untouched, so what
    is emitted is the source minus exactly the named rules. A rule whose
    expression Prometheus' parser refuses raises its ``InputRefused``.
    """
    produced: dict[str, list[str]] = {}
    kept_groups: list = []
    dropped: list[tuple[str, list[str]]] = []
    trees = iter(_lib_consumer_read.promql_shape_each(
        [str(rule.get("expr", "")) for group in groups or [] for rule in group.get("rules") or []],
        raise_refused=True))
    for group in groups or []:
        kept_rules = []
        for rule in group.get("rules") or []:
            reasons = operator_unportable(next(trees), produced)
            if "record" in rule:
                produced[rule["record"]] = reasons
            if reasons:
                dropped.append((rule.get("alert") or rule.get("record"), reasons))
            else:
                kept_rules.append(rule)
        if kept_rules:
            kept_groups.append({**group, "rules": kept_rules})
    return kept_groups, dropped


# ── absence guards on optional components (#1595 D1b) ───────────────────────
#: The charts a deployment may leave out, and how kube-state-metrics shows that
#: one IS deployed: (KSM metric, label, value). An `absent()` /
#: `absent_over_time()` over one of their metrics fires forever where the chart
#: is not installed, so it must be AND-ed with this presence check.
#: ⛔ Presence is read from kube-state-metrics, NOT from `up`: `up` disappears
#: exactly when the scrape target is lost (ServiceMonitor off or removed, the
#: Service's selector or label changed) — the failure the absence alert exists
#: to catch. KSM ships in this repo's raw stack and in kube-prometheus-stack.
#: The values are the charts' own resource names (Chart.Name-based, release
#: independent unless `nameOverride` is set); tests/helm/
#: test_platform_servicemonitors.py renders each chart and pins them. The
#: gateway's is its audit-metrics sidecar, not the Deployment: the audit
#: pipeline exists only with auditLog.enabled.
#: threshold-exporter is the platform itself and is not optional.
CHART_PRESENCE: dict[str, tuple[str, str, str]] = {
    TENANT_API_CHART: ("kube_deployment_spec_replicas", "deployment", "tenant-api"),
    VECTOR_CHART: ("kube_daemonset_status_desired_number_scheduled", "daemonset", "vector"),
    FEDERATION_GATEWAY_CHART: ("kube_pod_container_info", "container", "audit-metrics"),
    FEDERATION_RECONCILER_CHART: ("kube_deployment_spec_replicas", "deployment",
                                  "federation-reconciler"),
}
OPTIONAL_CHARTS: frozenset = frozenset(CHART_PRESENCE)


class AbsenceGuardUnreadable(Exception):
    """Prometheus' parser refused the expression, so its guards cannot be judged."""


#: Functions whose result has a value whatever their argument holds, so a
#: presence check inside them proves nothing.
_ALWAYS_VALUED_CALLS = frozenset({
    "absent", "absent_over_time", "scalar", "vector", "time", "pi",
})


def _present(node: dict) -> frozenset:
    """The optional charts whose presence is GUARANTEED whenever *node* (a
    ``promql_shape`` tree) has a value — not merely mentioned under it.
    `x and y` guarantees what either side does; `x or y` only what both sides
    do (`<presence> or vector(1)` guarantees nothing); `x unless y` only what
    `x` does; arithmetic and comparison need every vector operand, so they
    guarantee what any operand does; a function or aggregation guarantees
    what its arguments do, except the always-valued ones
    (_ALWAYS_VALUED_CALLS). Any other node → nothing."""
    kind = node["kind"]
    if kind == "selector":
        eq = [(m["name"], m["value"]) for m in node["matchers"] if m["type"] == "="]
        return frozenset(
            chart for chart, (metric, label, value) in CHART_PRESENCE.items()
            if ("__name__", metric) in eq and (label, value) in eq)
    if kind == "binary":
        lhs, rhs = _present(node["lhs"]), _present(node["rhs"])
        if node["op"] == "or":
            return lhs & rhs
        if node["op"] == "unless":
            return lhs
        return lhs | rhs
    if kind == "call":
        if node["name"] in _ALWAYS_VALUED_CALLS:
            return frozenset()
        out: frozenset = frozenset()
        for arg in node["args"]:
            out |= _present(arg)
        return out
    if kind in ("aggregate", "paren", "unary", "subquery"):
        return _present(node["expr"])
    return frozenset()


def unguarded_absences(expr) -> list[str]:
    """The absence checks in *expr* (its text, or its ``promql_shape`` tree)
    over an optional chart's metric that are not filtered by that chart's
    presence check (CHART_PRESENCE). A guard counts only when an `and` joins
    it to the absence: walking down from the root, the side of an `and`
    inherits the charts whose presence the OTHER side checks; nothing else
    (`or`, `unless`, arithmetic) adds a guard. So `absent(x) and on()
    count(<presence>) > 0` passes and `absent(x) or <presence>` does not.
    Read off Prometheus' parse tree (#4215); an expression its parser refuses
    raises AbsenceGuardUnreadable (fail closed). [] when all guarded."""
    if isinstance(expr, dict):
        tree = expr
    else:
        (tree,) = _lib_consumer_read.promql_shape_each([str(expr)])
        if isinstance(tree, _lib_consumer_read.InputRefused):
            raise AbsenceGuardUnreadable(f"{expr!r}: {_lib_consumer_read.error_text(tree)}")
    found: list[str] = []

    def visit(node: dict, guarded: frozenset) -> None:
        kind = node["kind"]
        if kind == "binary":
            if node["op"] == "and":
                visit(node["lhs"], guarded | _present(node["rhs"]))
                visit(node["rhs"], guarded | _present(node["lhs"]))
            else:
                visit(node["lhs"], guarded)
                visit(node["rhs"], guarded)
            return
        if kind == "call" and node["name"] in ("absent", "absent_over_time"):
            for metric in (_lib_consumer_read.shape_metric_names(node["args"][0])
                           if node["args"] else []):
                fam = resolve_family(metric)
                if fam and fam[1] == "helm-service" and fam[2] in OPTIONAL_CHARTS \
                        and fam[2] not in guarded:
                    pm, label, value = CHART_PRESENCE[fam[2]]
                    found.append(f"{node['name']}({metric}): {fam[2]} is optional — AND it "
                                 f"with `count({pm}{{{label}=\"{value}\"}}) > 0`")
        for key in ("expr", "param"):
            if isinstance(node.get(key), dict):
                visit(node[key], guarded)
        for arg in node.get("args") or []:
            visit(arg, guarded)

    visit(tree, frozenset())
    return found
