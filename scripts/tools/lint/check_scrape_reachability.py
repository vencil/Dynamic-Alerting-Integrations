#!/usr/bin/env python3
"""Scrape-face reachability gate — every rule-consumed leaf metric must be admitted by at least one scrape job (TRK-340 / #1203, epic #1200 WS2b S6).

WHY: an alert whose input metric is filtered out by EVERY scrape job is
structurally inert — pint/promtool stay green (the expression is valid), but
the series can never exist, so the alert can never fire. #1203 found five such
platform alerts: the tenant-api Service carried the full scrape annotation set
(and its NetworkPolicy even admitted Prometheus) while NO job's SD face could
select it, and the Alertmanager Service had no scrape annotation at all — the
ADR-025 self-liveness guardian AlertmanagerWebhookNotificationsFailing was
unfireable. Same declared-but-unwired disease as TRK-337, one contract to the
left: scrape face instead of threshold face.

WHAT THIS CHECKS: the leaf metrics DEMANDED by both rule trees
(rule-packs/rule-pack-*.yaml + k8s/03-monitoring/configmap-rules-*.yaml) are
resolved against the scrape face parsed from
k8s/03-monitoring/configmap-prometheus.yaml (every job's target-selection
keeps + metric_relabel __name__ keep/drop) and the Service manifests pinned in
k8s/**. Each leaf is classified:

  REACHABLE             admitted by >=1 job. ⚠️ TWO EVIDENCE TIERS hide under
                        this one word:
                        * `static` / `service` / `name-filtered` families — the
                          target is a REPO ARTIFACT (a k8s/** Service doc or a
                          static_configs job), so SD namespace + annotation keep
                          + __name__ filter are all evaluated against it.
                        * `helm-service` families (vector_, and the
                          threshold-exporter families below) — no repo manifest
                          pins the target, so only the JOB side is checked: that
                          some annotation-keep job's SD face covers the pinned
                          namespace, and that no __name__ filter rejects the
                          name. That the chart's Service actually carries
                          `prometheus.io/scrape` is ASSUMED, not verified —
                          this gate never opens helm/ (#1286).
  REACHABLE-IF-EXPORTED tenant-exporters face: the job WOULD admit a db-* ns
                        annotated Service, but per-tenant targets only exist at
                        runtime and whether the exporter exposes this exact
                        name is a runtime fact. Not decidable statically —
                        never an error.
  UNKNOWN-SOURCE        helm-only component whose install namespace is
                        Release.Namespace (not pinned by repo manifests). NOT
                        judged dead; instead ledger-managed in
                        KNOWN_UNKNOWN_SOURCE (exit-locked, see below).
  DEAD                  the expected target IS statically resolvable and no
                        job admits the metric (annotation missing / namespace
                        in no SD list / __name__ allowlist rejects). Hard
                        error: the alert can never fire.

Leaf metric = consumed metric minus recording-rule names (':' or produced by
either tree) and the two series Prometheus itself manufactures (`up`,
`ALERTS`) — those have no exporter and no /metrics line, so there is no scrape
face to resolve them against.

⚠️ VALUE-ORIGIN IS NOT SCRAPE-PROVENANCE (#1285). user_* / da_* /
tenant_metadata_info / tenant_expected_exporter were excluded here too, as
"conf.d/exporter-synthesised, not scraped from a component target … guarded by
other gates (TRK-337, observed-map)". The first clause conflated two different
questions and the rest was false:

  - Where the VALUE comes from (conf.d) says nothing about where the SERIES
    comes from. All four families are emitted by ONE binary, on ONE registry,
    behind ONE /metrics handler — threshold-exporter (collector.go
    Describe/Collect + main.go's MetricsHandler mount). They exist only if that
    component's target is scraped, exactly like alertmanager_* or kube_*.
  - "Guarded by other gates" was outright false for da_*: TRK-337 walks the
    threshold contract and the observed-map regex only matches colon-delimited
    recording rules, so neither family's scrape face is visible to either. For
    user_* it is half-true — TRK-337 guards the EMISSION supply chain (the
    conf.d key that produces the value exists) but never asks whether the
    emitting target can be scraped at all.

They are therefore classified like any other component family, via the
threshold-exporter helm-service entry below. Be precise about what that buys —
it is narrower than it looks, and measured:

  BUYS: a `__name__` keep/drop in the scraping job that rejects these families
  is now caught (13 DEAD, 0 collateral). That is the ONLY drift class this
  change newly detects; before #1285 the whole exclusion made it invisible.
  It also makes the 13 participate in SD-face drift, though that was already
  loud via the 24 other leaves pinned into the same namespace.

  DOES NOT BUY, in decreasing order of how easily it is mistaken for covered:
  (1) chart-side NAMESPACE drift. THRESHOLD_EXPORTER_NAMESPACE below is an
      assertion about helm/threshold-exporter/templates/service.yaml, and this
      gate never opens helm/. Templatise that line and all 13 keep reading
      REACHABLE. Tripwired only from the test suite, not from here (#1286).
  (2) chart-side ANNOTATION drift — same blindness; drop
      `prometheus.io/scrape` from the chart values and the verdict is unchanged
      (#1286).
  (3) that any individual NAME is really emitted. The prefixes are unbounded:
      a typo'd `da_config_evnet` reads REACHABLE. That stays TRK-337's half of
      the contract for user_*, and remains uncovered for da_* — the test suite
      pins the family's membership set instead.

KNOWN_UNKNOWN_SOURCE is EXIT-LOCKED (same discipline as the TRK-337
KNOWN_UNWIRED list): a NEW unknown-source metric is a hard error (classify it
— pin the family in _FAMILY_TABLE or add a ledger entry with a reason), and a
ledger entry that is no longer demanded or no longer classifies as
UNKNOWN-SOURCE is a hard error too, so the list can only shrink or stay
justified, never rot.

SCOPE BOUNDARY (federation topology): this gate models the SINGLE-CLUSTER
scrape face of configmap-prometheus.yaml only. Under the federation topology
(platform rule tree on central, Vector DaemonSet on edge — owner decision on
#1203) the vector_* consumers are inert for a DIFFERENT reason: plane
assignment (the series exist on edge but are not federated to where the rules
evaluate). That is a rule-plane/federation contract, tracked by the #1203
vector-plane mini-design + generate_rule_pack_split's central-input validator
— deliberately NOT covered here, so vector_* classifies REACHABLE (the scrape
face itself does admit it).

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_OPS = PROJECT_ROOT / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))
from generate_rule_pack_split import (  # noqa: E402
    extract_metrics_from_expr,
    extract_recording_outputs,
    is_federated_exporter_metric,
)

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, str(_THIS_DIR.parent))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

PROM_CONFIGMAP = PROJECT_ROOT / "k8s" / "03-monitoring" / "configmap-prometheus.yaml"
RULE_PACKS_DIR = PROJECT_ROOT / "rule-packs"
K8S_RULES_DIR = PROJECT_ROOT / "k8s" / "03-monitoring"
K8S_MANIFESTS_DIR = PROJECT_ROOT / "k8s"

SCRAPE_ANNOTATION = "prometheus.io/scrape"

# ── series Prometheus manufactures itself (no exporter, no /metrics line) ──
# `up` is synthesised per target by the scrape loop; `ALERTS` is synthesised by
# the rule engine. Neither has a scrape face to resolve, so neither is a leaf.
# NOTE this set is deliberately tiny: "the value is computed elsewhere" is NOT
# a reason to sit here — see the VALUE-ORIGIN note in the module docstring
# (#1285 removed a user_*/da_* prefix exclusion that made exactly that mistake).
INJECTED_EXACT = {"up", "ALERTS"}

# Vector's operational install namespace (#1018 PSS carve-out). The chart's
# Services render into Release.Namespace, but the platform pins the release to
# this namespace — the prometheus configmap SD list must carry it for vector_*
# to be scrapeable (see the monitoring-components job comment).
VECTOR_NAMESPACE = "vector"

# threshold-exporter's install namespace. Unlike Vector's (pinned only
# operationally), this one is HARDCODED in the chart template —
# helm/threshold-exporter/templates/service.yaml:5 writes `namespace:
# monitoring` literally, NOT `{{ .Release.Namespace }}` — and `monitoring` is a
# repo-pinned Namespace manifest (k8s/00-namespaces/namespaces.yaml:32). So the
# target's namespace IS statically decidable on this single-cluster face, and
# the family is judged, not ledgered as unknown-source.
THRESHOLD_EXPORTER_NAMESPACE = "monitoring"

# ── metric-family → expected source (declarative, longest-prefix wins) ──────
# kind:
#   static           scraped by a static_configs self-job (value = job name)
#   service          repo-pinned Service manifest (value = (namespace, name));
#                    admission is computed from the parsed manifests + jobs
#   helm-service     helm chart Service whose install ns is operationally
#                    pinned (value = namespace); admission = ns present in an
#                    annotation-keep job's SD list
#   name-filtered    node-role job with a __name__ allowlist (value = job name)
# Families not listed here fall through to the tenant-exporter face (the
# is_federated_exporter_metric SSOT) and then UNKNOWN-SOURCE.
# _FAMILY_EXACT (below) is consulted FIRST — see _resolve_family.
_FAMILY_TABLE: list[tuple[str, str, object]] = [
    ("prometheus_", "static", "prometheus"),
    ("alertmanager_", "service", ("monitoring", "alertmanager")),
    ("tenant_api_", "service", ("tenant-api", "tenant-api")),
    # NOTE `kubelet_` before `kube_` is load-bearing — longest-prefix matching
    # protects it, but keep the order human-readable too (substring trap).
    ("kubelet_", "name-filtered", "kubelet-volume-stats"),
    ("kube_", "service", ("monitoring", "kube-state-metrics")),
    ("container_", "name-filtered", "kubelet-cadvisor"),
    ("vector_", "helm-service", VECTOR_NAMESPACE),
    # threshold-exporter's two config-driven families (#1285). Their VALUES
    # come from conf.d; their SERIES come from the threshold-exporter target
    # like everything else it registers — see the module docstring.
    ("da_", "helm-service", THRESHOLD_EXPORTER_NAMESPACE),
    ("user_", "helm-service", THRESHOLD_EXPORTER_NAMESPACE),
]

# ── EXACT-NAME families (checked before _FAMILY_TABLE) ─────────────────────
# For series whose name shares a prefix with metrics from a DIFFERENT source,
# so no prefix can route them without collateral.
#
# ⛔ DO NOT "simplify" these two rows into a `tenant_` prefix entry (#1285).
# Measured: a `tenant_` prefix also swallows tenant_federation_requests_total
# (federation-gateway chart) and tenant_log_query_requests_total
# (tenant-log-query plane) — both correctly ledgered UNKNOWN-SOURCE because
# their install ns is Release.Namespace. A prefix would reclassify them
# REACHABLE off the wrong component's Service and trip the ledger exit-lock.
# The pin lives in tests/lint/test_check_scrape_reachability.py.
_FAMILY_EXACT: dict[str, tuple[str, object]] = {
    # Same collector.go / same registry / same /metrics as the da_*+user_* rows.
    "tenant_metadata_info": ("helm-service", THRESHOLD_EXPORTER_NAMESPACE),
    "tenant_expected_exporter": ("helm-service", THRESHOLD_EXPORTER_NAMESPACE),
}

# Helm-only components: install namespace is Release.Namespace, NOT pinned by
# any repo manifest, so the single-cluster scrape face cannot statically decide
# whether their Services land in an SD-listed namespace. Ledger-managed, never
# judged DEAD here. Exit-locked: entries must stay demanded AND stay
# unknown-source, else they must be removed (see module docstring).
KNOWN_UNKNOWN_SOURCE: dict[str, str] = {
    "federation_gateway_revocation_load_errors":
        "federation-gateway helm chart — install ns = Release.Namespace, not pinned (#1203)",
    "tenant_federation_requests_total":
        "federation-gateway helm chart — install ns = Release.Namespace, not pinned (#1203)",
    "federation_revocation_last_reconcile_timestamp_seconds":
        "federation-reconciler helm chart — install ns = Release.Namespace, not pinned (#1203)",
    "federation_revocation_tamper_suspected":
        "federation-reconciler helm chart — install ns = Release.Namespace, not pinned (#1203)",
    # Same reconciler Deployment as the two rows above — a zero-label PLATFORM
    # gauge. It could not be ledgered until TRK-353 / #1250 removed the `*_up`
    # suffix shortcut that classified it REACHABLE-IF-EXPORTED (the exit-lock
    # rejected the row with STALE-EXEMPTION).
    # ⚠️ Be precise about what this row buys, because #1250 overstated it: the
    # EXIT-LOCK, not target-drop detection. No helm-only row (these three
    # included) can be judged DEAD — Release.Namespace is not statically
    # resolvable on this single-cluster face — so "the reconciler's target
    # silently vanished" is out of reach for the cohort, not a protection this
    # gauge was singled out of. What the shortcut really cost: a factually false
    # reason string, exclusion from the exit-locked cohort, and a blanket excuse
    # for every FUTURE platform gauge whose name ends in `_up`.
    "federation_revocation_channel_up":
        "federation-reconciler helm chart — install ns = Release.Namespace, not pinned (#1203)",
    "federation_revocation_live_set_rejected_lines":
        "federation-reconciler helm chart — install ns = Release.Namespace, not pinned (#1203)",
    # NOTE the federation_gateway_ prefix, but the RECONCILER emits it: it is the
    # gateway's reload-refusal warning counted off the gateway's log stream, so the
    # scrape target is the reconciler Deployment, not the gateway (#1235).
    "federation_gateway_revoked_set_reload_rejected":
        "federation-reconciler helm chart — install ns = Release.Namespace, not pinned (#1203)",
    "tenant_log_query_requests_total":
        "tenant-log-query plane, helm-only — install ns not pinned by repo manifests (#1203)",
}

# ── PromQL extraction (patched superset of the split tool's extractor) ──────
# generate_rule_pack_split.extract_metrics_from_expr requires the metric name
# to be followed by `{`, `[` or whitespace, so it misses a metric directly
# followed by `)` or end-of-string (e.g. `(time() - foo_seconds) > 600`) and
# bare `absent(foo)`. The patch strips grouping clauses (by/on/... lists would
# otherwise leak label names as metrics) and string literals, then re-scans
# with a trailing charset that includes `)`/EOL. Keyword/function names the
# wider scan can capture are subtracted below. Verified against every rule in
# both trees by the sanity tests (tests/lint/test_check_scrape_reachability.py).
_EXTRA_KEYWORDS = frozenset({
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
_GROUPING_CLAUSE_RE = re.compile(
    r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
# Lookahead allowlist = what may legally follow a METRIC reference: selector
# `{`, range `[`, whitespace, `)`, EOF — plus compact-writing operators like
# `metric>60` (Gemini review of #1221 — verified miss). Two DELIBERATE
# exclusions, both verified regressions when attempted: `,` / bare
# negative-lookahead-on-`(` leak function-argument labels (label_replace/
# group_left args); `=` and `!` leak selector label-matcher names (this
# extractor does NOT strip brace interiors — the lookahead IS the brace
# filter: `{condition="Ready"}` must not yield `condition`). Compact `a==1`
# therefore stays unextracted — documented limitation, spaced form works.
_TOKEN_RE = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\b(?=[{\[\s)+\-*/<>^%]|$)")
_ABSENT_RE = re.compile(r"absent(?:_over_time)?\(\s*([a-zA-Z_:][a-zA-Z0-9_:]*)")
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def extract_metrics(expr: str) -> set[str]:
    """Metric names consumed by *expr* (see patch note above)."""
    if not expr or not isinstance(expr, str):
        return set()
    base = extract_metrics_from_expr(expr)
    stripped = _STRING_RE.sub('""', expr)
    stripped = _GROUPING_CLAUSE_RE.sub(r"\1 ", stripped)
    extra = {m for m in _TOKEN_RE.findall(stripped) if not m[0].isupper()}
    extra |= set(_ABSENT_RE.findall(expr))
    return (base | extra) - _EXTRA_KEYWORDS


# ── scrape-face parsing ─────────────────────────────────────────────────────

def parse_scrape_jobs(prom_yaml_text: str) -> list[dict]:
    """Parse a prometheus.yml document into the job model this gate evaluates.

    Per job: static self-scrape flag, SD role + namespace list, the
    annotation/namespace target-selection keeps (relabel_configs), and the
    __name__-level keep/drop rules plus other label keep/drops
    (metric_relabel_configs).
    """
    prom = yaml.safe_load(prom_yaml_text)
    jobs: list[dict] = []
    for sc in prom.get("scrape_configs", []) or []:
        job = {
            "job": sc.get("job_name"),
            "static": bool(sc.get("static_configs")),
            "sd_role": None,
            "sd_namespaces": [],
            "annotation_keep": False,
            "ns_keep_regex": None,
            "name_rules": [],   # [(action, regex)] over __name__
            "label_rules": [],  # [(action, source_labels, regex)] other keep/drop
        }
        for sd in sc.get("kubernetes_sd_configs", []) or []:
            job["sd_role"] = sd.get("role")
            job["sd_namespaces"] = list(
                ((sd.get("namespaces") or {}).get("names")) or [])
        for rc in sc.get("relabel_configs", []) or []:
            if rc.get("action") != "keep":
                continue
            src = rc.get("source_labels", [])
            if src == ["__meta_kubernetes_service_annotation_prometheus_io_scrape"]:
                job["annotation_keep"] = rc.get("regex") == "true"
            elif src == ["__meta_kubernetes_namespace"]:
                job["ns_keep_regex"] = rc.get("regex", "(.*)")
        for mrc in sc.get("metric_relabel_configs", []) or []:
            action = mrc.get("action", "replace")
            if action not in ("keep", "drop"):
                continue
            src = mrc.get("source_labels", [])
            if src == ["__name__"]:
                job["name_rules"].append((action, mrc.get("regex", "(.*)")))
            else:
                job["label_rules"].append((action, src, mrc.get("regex", "(.*)")))
        jobs.append(job)
    return jobs


def load_repo_jobs() -> list[dict]:
    cm = yaml.safe_load(PROM_CONFIGMAP.read_text(encoding="utf-8"))
    return parse_scrape_jobs(cm["data"]["prometheus.yml"])


def name_passes(metric: str, name_rules: list[tuple[str, str]]) -> bool:
    """Prometheus relabel semantics: regex is anchored full-match; a keep that
    does not match drops the series, a drop that matches drops it."""
    for action, regex in name_rules:
        matched = re.fullmatch(regex, metric) is not None
        if action == "keep" and not matched:
            return False
        if action == "drop" and matched:
            return False
    return True


# ── Service manifest parsing (target existence side of the contract) ────────

def parse_services(paths: list[Path]) -> list[dict]:
    """Every `kind: Service` doc found in *paths* → {name, namespace, annotations}."""
    services: list[dict] = []
    for p in paths:
        try:
            docs = list(yaml.safe_load_all(p.read_text(encoding="utf-8")))
        except yaml.YAMLError:
            # Raw k8s manifests parse clean today; a parse failure here is
            # kube-linter's problem (L4 gate), not silently a missing Service.
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "Service":
                continue
            meta = doc.get("metadata") or {}
            services.append({
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "annotations": meta.get("annotations") or {},
            })
    return services


def load_repo_services() -> list[dict]:
    return parse_services(sorted(K8S_MANIFESTS_DIR.rglob("*.yaml")))


def _service_admitted(svc: dict, job: dict) -> bool:
    """True iff *job*'s target-selection face would keep *svc*."""
    if job["sd_role"] != "service":
        return False
    ns = svc.get("namespace")
    if job["sd_namespaces"] and ns not in job["sd_namespaces"]:
        return False
    if job["annotation_keep"] and svc["annotations"].get(SCRAPE_ANNOTATION) != "true":
        return False
    if job["ns_keep_regex"] is not None:
        if not ns or re.fullmatch(job["ns_keep_regex"], ns) is None:
            return False
    return True


def _tenant_exporter_face_exists(jobs: list[dict]) -> bool:
    """A job that would admit an annotated Service in a tenant (db-*) namespace.

    Probed with a synthetic namespace string — NOT a tenant id (dev-rule #2);
    any name matching the job's ns keep regex works, targets are runtime.
    """
    probe = {"name": "exporter", "namespace": "db-probe",
             "annotations": {SCRAPE_ANNOTATION: "true"}}
    return any(_service_admitted(probe, j) for j in jobs)


# ── rule-tree collection ────────────────────────────────────────────────────

def collect_rule_trees() -> tuple[dict[str, set], set[str]]:
    """(consumed metric → {consumer rule ids}, recording outputs of BOTH trees)."""
    consumed: dict[str, set] = defaultdict(set)
    outputs: set[str] = set()

    def _ingest(rule_doc: dict, origin: str) -> None:
        rules = [r for g in rule_doc.get("groups", []) or []
                 for r in g.get("rules", []) or []]
        outputs.update(extract_recording_outputs(rules))
        for r in rules:
            rid = f"{origin}::{r.get('alert') or r.get('record')}"
            for m in extract_metrics(r.get("expr", "") or ""):
                consumed[m].add(rid)

    for p in sorted(RULE_PACKS_DIR.glob("rule-pack-*.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(doc, dict) and "groups" in doc:
            _ingest(doc, p.name)
    for p in sorted(K8S_RULES_DIR.glob("configmap-rules-*.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
            continue
        for body in (doc.get("data") or {}).values():
            rd = yaml.safe_load(body)
            if isinstance(rd, dict) and "groups" in rd:
                _ingest(rd, p.name)
    return consumed, outputs


def leaf_metrics(consumed: dict[str, set], outputs: set[str]) -> dict[str, set]:
    """Consumed metrics that must come from the scrape face (see docstring).

    Only recording-rule outputs and the Prometheus-manufactured series are
    dropped. A metric is NOT exempt for being config-driven — that is a claim
    about its value, not about how the series reaches Prometheus (#1285).
    """
    leaves: dict[str, set] = {}
    for m, users in consumed.items():
        if ":" in m or m in outputs or m in INJECTED_EXACT:
            continue
        leaves[m] = users
    return leaves


# ── classification ──────────────────────────────────────────────────────────

REACHABLE = "REACHABLE"
REACHABLE_IF_EXPORTED = "REACHABLE-IF-EXPORTED"
UNKNOWN_SOURCE = "UNKNOWN-SOURCE"
DEAD = "DEAD"


def _resolve_family(metric: str):
    """→ (matched key, kind, spec) or None. Exact beats prefix, then longest
    prefix wins. Exact-first is load-bearing, not a tie-break nicety: an exact
    row exists precisely because the name's prefix belongs to someone else."""
    exact = _FAMILY_EXACT.get(metric)
    if exact is not None:
        kind, spec = exact
        return (metric, kind, spec)
    best = None
    for prefix, kind, spec in _FAMILY_TABLE:
        if metric.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, kind, spec)
    return best


def classify(metric: str, jobs: list[dict], services: list[dict]) -> tuple[str, str]:
    """→ (classification, reason)."""
    fam = _resolve_family(metric)
    if fam is not None:
        _prefix, kind, spec = fam
        if kind == "static":
            job = next((j for j in jobs if j["job"] == spec and j["static"]), None)
            if job is not None and name_passes(metric, job["name_rules"]):
                return REACHABLE, f"static self-scrape job '{spec}'"
            return DEAD, f"static job '{spec}' missing or its __name__ filter rejects"
        if kind == "service":
            ns, name = spec
            svc = next((s for s in services
                        if s["namespace"] == ns and s["name"] == name), None)
            if svc is None:
                return DEAD, (f"pinned Service {ns}/{name} not found in k8s manifests "
                              "— family table points at a missing target")
            admitting = [j for j in jobs if _service_admitted(svc, j)
                         and name_passes(metric, j["name_rules"])]
            if admitting:
                return REACHABLE, (f"Service {ns}/{name} admitted by job "
                                   f"'{admitting[0]['job']}'")
            if svc["annotations"].get(SCRAPE_ANNOTATION) != "true":
                return DEAD, (f"Service {ns}/{name} has no {SCRAPE_ANNOTATION}=true "
                              "annotation — every annotation-keep job filters it out")
            return DEAD, (f"Service {ns}/{name} is annotated but its namespace is in "
                          "no job's SD/namespace-keep face — no job can select it")
        if kind == "helm-service":
            ns = spec
            admitting = [j for j in jobs
                         if j["sd_role"] == "service" and j["annotation_keep"]
                         and (not j["sd_namespaces"] or ns in j["sd_namespaces"])
                         and (j["ns_keep_regex"] is None
                              or re.fullmatch(j["ns_keep_regex"], ns))
                         and name_passes(metric, j["name_rules"])]
            if admitting:
                # Say what was actually checked. The federation-plane caveat is
                # a vector_-specific scope note (see SCOPE BOUNDARY above) and
                # is meaningless for other helm-service families, so it is not
                # asserted here; "assumed annotated" states the real gap.
                return REACHABLE, (f"job '{admitting[0]['job']}' admits ns '{ns}' "
                                   "(chart Service assumed annotated — helm/ not "
                                   "read, #1286)")
            return DEAD, (f"pinned ns '{ns}' is in no annotation-keep job's SD face, "
                          "or a __name__ filter rejects this metric")
        if kind == "name-filtered":
            job = next((j for j in jobs if j["job"] == spec), None)
            if job is None:
                return DEAD, f"job '{spec}' not found in scrape config"
            if name_passes(metric, job["name_rules"]):
                return REACHABLE, f"job '{spec}' __name__ filter admits it"
            return DEAD, (f"job '{spec}' __name__ allowlist rejects it and this "
                          "family has no other source")
    # Provenance by FAMILY, never by name shape: a `metric.endswith("_up")`
    # shortcut used to sit here (and its twin in the split tool), classifying
    # every platform gauge whose name happens to end in `_up` as tenant-exporter
    # liveness — the one metric it was load-bearing for was the misclassified
    # one (TRK-353 / #1250). New exporter family → register its prefix in the
    # _FEDERATED_EXPORTER_PREFIXES SSOT; a name suffix is not evidence.
    if is_federated_exporter_metric(metric):
        if _tenant_exporter_face_exists(jobs):
            return REACHABLE_IF_EXPORTED, (
                "tenant-exporters face (annotated db-* ns Service); whether the "
                "exporter exposes this exact name is a runtime fact")
        return DEAD, "no job admits an annotated tenant-namespace Service"
    return UNKNOWN_SOURCE, ("no pinned family/manifest matches — helm-only or "
                            "unmodelled source")


# ── the gate ────────────────────────────────────────────────────────────────

def run_check(
    consumed: dict[str, set] | None = None,
    outputs: set[str] | None = None,
    jobs: list[dict] | None = None,
    services: list[dict] | None = None,
    known_unknown: dict[str, str] | None = None,
) -> dict:
    """Return {errors, infos, classes, counts}. errors fail --ci.

    Inputs default to the real repo artifacts; hermetic tests inject synthetic
    trees / scrape configs / manifests to pin each branch (including the
    pre-#1203 tenant-api DEAD state) without editing repo files.
    """
    if consumed is None or outputs is None:
        real_consumed, real_outputs = collect_rule_trees()
        consumed = real_consumed if consumed is None else consumed
        outputs = real_outputs if outputs is None else outputs
    if jobs is None:
        jobs = load_repo_jobs()
    if services is None:
        services = load_repo_services()
    if known_unknown is None:
        known_unknown = KNOWN_UNKNOWN_SOURCE

    leaves = leaf_metrics(consumed, outputs)
    classes: dict[str, tuple[str, str]] = {
        m: classify(m, jobs, services) for m in sorted(leaves)}
    counts: dict[str, int] = defaultdict(int)
    for cls, _reason in classes.values():
        counts[cls] += 1

    errors: list[str] = []
    infos: list[str] = []

    for m, (cls, reason) in classes.items():
        consumers = ", ".join(sorted(leaves[m])[:3])
        if cls == DEAD:
            errors.append(
                f"DEAD: {m!r} is consumed ({consumers}) but no scrape job admits "
                f"it — {reason}. The consuming alert/recording is structurally "
                "inert. (TRK-340)")
        elif cls == UNKNOWN_SOURCE:
            if m in known_unknown:
                infos.append(f"known-unknown-source {m} — {known_unknown[m]}")
            else:
                errors.append(
                    f"NEW-UNKNOWN-SOURCE: {m!r} is consumed ({consumers}) but "
                    "matches no pinned family or manifest. Classify it: pin the "
                    "family in _FAMILY_TABLE, or ledger it in "
                    "KNOWN_UNKNOWN_SOURCE with a reason. (TRK-340)")

    # Exit-lock: the ledger may only shrink or stay justified.
    for m in sorted(known_unknown):
        if m not in leaves:
            errors.append(
                f"STALE-EXEMPTION: {m!r} is in KNOWN_UNKNOWN_SOURCE but no rule "
                "demands it anymore — remove it from the ledger.")
        elif classes[m][0] != UNKNOWN_SOURCE:
            errors.append(
                f"STALE-EXEMPTION: {m!r} is in KNOWN_UNKNOWN_SOURCE but now "
                f"classifies {classes[m][0]} ({classes[m][1]}) — the source got "
                "pinned; remove it from the ledger so the gate protects it.")

    return {"errors": errors, "infos": infos, "classes": classes,
            "counts": dict(counts)}


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "scrape 面可達性 gate：規則消費的葉 metric 須至少被一個 scrape job 放行（TRK-340）",
            "Scrape-face reachability gate: every rule-consumed leaf metric must "
            "be admitted by at least one scrape job (TRK-340)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("DEAD / 新 UNKNOWN-SOURCE / ledger exit-lock 破口即 exit 1",
                       "exit 1 on DEAD, NEW unknown-source, or a ledger exit-lock breach"))
    parser.add_argument(
        "--verbose", action="store_true",
        help=i18n_text("列出每個葉 metric 的分類與理由",
                       "print every leaf metric's classification and reason"))
    args = parser.parse_args(argv)

    try:
        result = run_check()
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        print(f"ERROR: scrape-reachability check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if args.verbose:
        for m, (cls, reason) in result["classes"].items():
            print(f"  {cls:<22} {m}  ({reason})", file=sys.stderr)
    counts = result["counts"]
    summary = ", ".join(f"{k}={counts.get(k, 0)}" for k in (
        REACHABLE, REACHABLE_IF_EXPORTED, UNKNOWN_SOURCE, DEAD))
    for msg in result["infos"]:
        print(f"INFO: {msg}", file=sys.stderr)
    for msg in result["errors"]:
        print(f"❌ {msg}", file=sys.stderr)

    if result["errors"]:
        print(
            f"\n{len(result['errors'])} scrape-reachability violation(s) — TRK-340 "
            f"[{summary}].\nA rule whose input metric no scrape job admits is "
            "structurally inert and no other gate catches it. See "
            "scripts/tools/lint/check_scrape_reachability.py.",
            file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK

    print(
        f"✅ scrape reachability OK [{summary}] — "
        f"{len(result['infos'])} known-unknown-source (ledgered), 0 DEAD.",
        file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
