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
Standard library plus ``generate_rule_pack_split``'s extractor (shipped).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# generate_rule_pack_split lives in ops/ in the repo; the image ships it flat
# beside this module. Appended, so nothing there shadows a module already found.
if (_HERE / "ops").is_dir() and str(_HERE / "ops") not in sys.path:
    sys.path.append(str(_HERE / "ops"))

from generate_rule_pack_split import extract_metrics_from_expr  # noqa: E402

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


# ── PromQL extraction (patched superset of the split tool's extractor) ──────
# generate_rule_pack_split.extract_metrics_from_expr requires the metric name
# to be followed by `{`, `[` or whitespace, so it misses a metric directly
# followed by `)` or end-of-string (e.g. `(time() - foo_seconds) > 600`) and
# bare `absent(foo)`. The patch strips grouping clauses (by/on/... lists would
# otherwise leak label names as metrics) and string literals, then re-scans
# with a trailing charset that includes `)`/EOL. Keyword/function names the
# wider scan can capture are subtracted below. Verified against every rule in
# both trees by the sanity tests (tests/lint/test_check_scrape_reachability.py).
EXTRA_KEYWORDS = frozenset({
    "without", "bool", "offset", "vector", "time", "absent", "absent_over_time",
    "label_replace", "label_join", "changes", "clamp_min", "clamp_max",
    "max_over_time", "min_over_time", "avg_over_time", "count_over_time",
    "sum_over_time", "last_over_time", "quantile_over_time", "stddev_over_time",
    "predict_linear", "deriv", "round", "ceil", "floor", "abs", "scalar",
    "count_values", "quantile", "stddev", "stdvar", "idelta", "resets",
    "sort", "sort_desc", "day_of_week", "hour", "minute", "sgn", "exp", "ln",
    # the split extractor's own builtin set (its filter only applies to ITS
    # matches; the wider re-scan here captures these again)
    "rate", "sum", "max", "min", "avg", "count", "topk", "bottomk",
    "histogram_quantile", "increase", "delta", "irate", "group",
    "on", "ignoring", "group_left", "group_right", "unless", "by",
    "or", "and",
})
GROUPING_CLAUSE_RE = re.compile(
    r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
# Lookahead allowlist = what may legally follow a METRIC reference: selector
# `{`, range `[`, whitespace, `)`, EOF — plus compact-writing operators like
# `metric>60` (Gemini review of #1221 — verified miss). Two DELIBERATE
# exclusions, both verified regressions when attempted: `,` / bare
# negative-lookahead-on-`(` leak function-argument labels (label_replace/
# group_left args); `=` and `!` leak selector label-matcher names (this
# extractor does NOT strip brace interiors — the lookahead IS the brace
# filter: `{condition="Ready"}` must not yield `condition`). The compact
# comparisons `a==1` / `a!=1` are caught by a second scan, COMPARISON_RE, run on
# the text with every `{...}` emptied — so no matcher name can reach it (#1595
# D1b: a rule `mysql_up==0` otherwise read as reading no metric, and
# operator_unportable kept it).
TOKEN_RE = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\b(?=[{\[\s)+\-*/<>^%]|$)")
COMPARISON_RE = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\b(?=[=!]=)")
BRACE_RE = re.compile(r"\{[^}]*\}")
ABSENT_RE = re.compile(r"absent(?:_over_time)?\(\s*([a-zA-Z_:][a-zA-Z0-9_:]*)")
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def extract_metrics(expr: str) -> set[str]:
    """Metric names consumed by *expr* (see patch note above)."""
    if not expr or not isinstance(expr, str):
        return set()
    base = extract_metrics_from_expr(expr)
    stripped = STRING_RE.sub('""', expr)
    stripped = GROUPING_CLAUSE_RE.sub(r"\1 ", stripped)
    extra = {m for m in TOKEN_RE.findall(stripped) if not m[0].isupper()}
    unbraced = BRACE_RE.sub("{}", stripped)
    extra |= {m for m in COMPARISON_RE.findall(unbraced) if not m[0].isupper()}
    extra |= set(ABSENT_RE.findall(expr))
    return (base | extra) - EXTRA_KEYWORDS


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

#: Recording rules a platform pack reads that another pack records (#3771).
#: operator_split judges one pack at a time, so it cannot see their rule; each
#: is recorded by the always-on liveness pack (emitted in operator mode too)
#: from tenant-exporter metrics selected under ``job="tenant-exporters"``
#: (TENANT_EXPORTER_JOB — the Operator doc's ServiceMonitor sets that job), so
#: it has a source wherever the platform pack runs.
#: tests/rulepacks/test_exporter_presence.py holds each name to a record of
#: rule-pack-liveness.yaml whose every input selects that job.
CROSS_PACK_RECORDS: frozenset = frozenset({"tenant:exporter_present",
                                           "tenant:exporter_presence_loaded"})

#: Series Prometheus itself manufactures (no exporter, no /metrics line):
#: `up` per scrape target, `ALERTS` per alert. `up` carries the target's job,
#: so its source is whatever job its selector names.
MANUFACTURED = frozenset({"up", "ALERTS"})

_MATCHER_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')


def _occurrences(metric: str, expr: str) -> list[str | None]:
    """The selector body of every reference to *metric* in *expr* (None for a
    reference without braces). String literals are blanked first, so a label
    VALUE spelling the name is not a reference."""
    blank = STRING_RE.sub(lambda m: '"' + "_" * (len(m.group(0)) - 2) + '"', expr)
    found: list[str | None] = []
    for m in re.finditer(r"(?<![\w:])" + re.escape(metric) + r"(?![\w:])", blank):
        rest = blank[m.end():]
        brace = re.match(r"\s*\{", rest)
        if brace is None:
            found.append(None)
            continue
        start = m.end() + brace.end()
        close = blank.find("}", start)
        found.append(expr[start:close if close >= 0 else len(expr)])
    return found


def _job_matchers(metric: str, expr: str) -> list[tuple[str, str] | None]:
    """Per reference to *metric*: its `job` matcher as (op, value), or None."""
    out: list[tuple[str, str] | None] = []
    for body in _occurrences(metric, expr):
        jobs = [(op, value) for name, op, value in _MATCHER_RE.findall(body or "")
                if name == "job"]
        out.append(jobs[0] if jobs else None)
    return out


def operator_jobs() -> frozenset:
    """Every `job` value an Operator install's ServiceMonitors produce: each
    platform chart's, plus the tenant exporters' that the Operator doc teaches
    (its `job` relabeling to TENANT_EXPORTER_JOB, #3720)."""
    return frozenset().union(*CHART_SERVICEMONITOR_JOBS.values(), {TENANT_EXPORTER_JOB})


def operator_unportable(expr: str, produced: "dict[str, list[str]] | None" = None) -> list[str]:
    """Why *expr* cannot run under a kube-prometheus-stack install, or [] when
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
    for metric in sorted(extract_metrics(expr)):
        if metric in produced:
            if produced[metric]:
                reasons.append(f"{metric}: its recording rule is not portable")
            continue
        if metric in CROSS_PACK_RECORDS:
            continue
        jobs = _job_matchers(metric, expr)
        if metric == "ALERTS":
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
    is emitted is the source minus exactly the named rules.
    """
    produced: dict[str, list[str]] = {}
    kept_groups: list = []
    dropped: list[tuple[str, list[str]]] = []
    for group in groups or []:
        kept_rules = []
        for rule in group.get("rules") or []:
            reasons = operator_unportable(str(rule.get("expr", "")), produced)
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
    """The expression could not be parsed, so its guards cannot be judged."""


#: Functions whose result has a value whatever their argument holds, so a
#: presence check inside them proves nothing.
_ALWAYS_VALUED_CALLS = frozenset({
    "absent", "absent_over_time", "scalar", "vector", "time", "pi",
})


def _present(node) -> frozenset:
    """The optional charts whose presence is GUARANTEED whenever *node* has a
    value — not merely mentioned under it. `x and y` guarantees what either
    side does; `x or y` only what both sides do (`<presence> or vector(1)`
    guarantees nothing); `x unless y` only what `x` does; arithmetic and
    comparison need every vector operand, so they guarantee what any operand
    does; a function or aggregation guarantees what its arguments do, except
    the always-valued ones (_ALWAYS_VALUED_CALLS). Unknown node → nothing."""
    kind = type(node).__name__
    if kind == "VectorSelector":
        return frozenset(
            chart for chart, (metric, label, value) in CHART_PRESENCE.items()
            if node.name == metric and any(
                m.name == label and str(m.op).endswith("Equal") and
                not str(m.op).endswith("NotEqual") and m.value == value
                for m in node.matchers.matchers))
    if kind == "BinaryExpr":
        op = str(node.op)
        lhs, rhs = _present(node.lhs), _present(node.rhs)
        if op == "or":
            return lhs & rhs
        if op == "unless":
            return lhs
        return lhs | rhs
    if kind == "Call":
        if node.func.name in _ALWAYS_VALUED_CALLS:
            return frozenset()
        out: frozenset = frozenset()
        for arg in node.args or []:
            out |= _present(arg)
        return out
    if kind == "MatrixSelector":
        return _present(node.vector_selector)
    if kind in ("AggregateExpr", "ParenExpr", "UnaryExpr", "SubqueryExpr"):
        return _present(node.expr)
    return frozenset()


def unguarded_absences(expr: str) -> list[str]:
    """The absence checks in *expr* over an optional chart's metric that are
    not filtered by that chart's presence check (CHART_PRESENCE). A guard
    counts only when an `and` joins it to the absence: walking down from the
    root, the side of an `and` inherits the charts whose presence the OTHER
    side checks; nothing else (`or`, `unless`, arithmetic) adds a guard. So
    `absent(x) and on() count(<presence>) > 0` passes and `absent(x) or
    <presence>` does not. Read with promql_parser; an expression it cannot
    parse raises AbsenceGuardUnreadable (fail closed). [] when all guarded."""
    try:
        import promql_parser
        tree = promql_parser.parse(expr)
    except Exception as exc:  # noqa: BLE001 — any reader failure is unjudgeable
        raise AbsenceGuardUnreadable(f"{expr!r}: {exc}") from exc
    found: list[str] = []

    def visit(node, guarded: frozenset) -> None:
        kind = type(node).__name__
        if kind == "BinaryExpr":
            if str(node.op) == "and":
                visit(node.lhs, guarded | _present(node.rhs))
                visit(node.rhs, guarded | _present(node.lhs))
            else:
                visit(node.lhs, guarded)
                visit(node.rhs, guarded)
            return
        if kind == "Call" and node.func.name in ("absent", "absent_over_time"):
            arg = node.args[0] if node.args else None
            vs = getattr(arg, "vector_selector", arg)
            metric = getattr(vs, "name", None)
            fam = resolve_family(metric) if metric else None
            if fam and fam[1] == "helm-service" and fam[2] in OPTIONAL_CHARTS \
                    and fam[2] not in guarded:
                pm, label, value = CHART_PRESENCE[fam[2]]
                found.append(f"{node.func.name}({metric}): {fam[2]} is optional — AND it with "
                             f"`count({pm}{{{label}=\"{value}\"}}) > 0`")
        for attr in ("expr", "param"):
            child = getattr(node, attr, None)
            if child is not None and not isinstance(child, (str, int, float)):
                visit(child, guarded)
        for arg in getattr(node, "args", None) or []:
            visit(arg, guarded)

    visit(tree, frozenset())
    return found
