"""Tests for scripts/tools/lint/check_scrape_reachability.py (TRK-340 / #1203).

The gate asserts every rule-consumed leaf metric is admitted by at least one
scrape job of k8s/03-monitoring/configmap-prometheus.yaml. Each test pins one
branch of that contract:
  - the live repo is green (0 DEAD, the 8 helm-only unknowns ledgered)
  - sanity anchors: the cadvisor job's three filter layers parse correctly,
    and the PRE-#1203 scrape face (tenant-api ns absent from the SD list /
    alertmanager unannotated) classifies the victim metrics DEAD — injected,
    so the regression pin survives the repo fix
  - each classification branch (REACHABLE / REACHABLE-IF-EXPORTED /
    UNKNOWN-SOURCE / DEAD) and the KNOWN_UNKNOWN_SOURCE exit-lock
  - the CLI exit-code contract (--ci escalates, bare run is report-only)
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_scrape_reachability.py"

_spec = importlib.util.spec_from_file_location("check_scrape_reachability", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# ── synthetic scrape faces (exercise parse_scrape_jobs on the way in) ────────

# The scrape face as it stood BEFORE the #1203 fix: monitoring-components SD
# list lacks tenant-api. Used by the regression pins below.
_PRE_FIX_PROM_YML = """
scrape_configs:
  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9090"]
  - job_name: "tenant-exporters"
    kubernetes_sd_configs:
      - role: service
    relabel_configs:
      - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
      - source_labels: [__meta_kubernetes_namespace]
        action: keep
        regex: "db-.+"
  - job_name: "monitoring-components"
    kubernetes_sd_configs:
      - role: service
        namespaces:
          names: ["monitoring", "vector"]
    relabel_configs:
      - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
  - job_name: "kubelet-cadvisor"
    kubernetes_sd_configs:
      - role: node
    metric_relabel_configs:
      - source_labels: [namespace]
        action: keep
        regex: "db-.+"
      - source_labels: [__name__]
        action: keep
        regex: "container_cpu_usage_seconds_total|container_memory_working_set_bytes"
      - source_labels: [container]
        action: drop
        regex: "|POD"
"""

_TENANT_API_SVC = {"name": "tenant-api", "namespace": "tenant-api",
                   "annotations": {"prometheus.io/scrape": "true",
                                   "prometheus.io/port": "8080"}}
_ALERTMANAGER_SVC_BARE = {"name": "alertmanager", "namespace": "monitoring",
                          "annotations": {}}
_ALERTMANAGER_SVC_ANNOTATED = {"name": "alertmanager", "namespace": "monitoring",
                               "annotations": {"prometheus.io/scrape": "true",
                                               "prometheus.io/port": "9093"}}


def _pre_fix_jobs():
    return gate.parse_scrape_jobs(_PRE_FIX_PROM_YML)


def _check(consumed, jobs, services, known_unknown=None):
    return gate.run_check(
        consumed=consumed, outputs=set(), jobs=jobs, services=services,
        known_unknown=known_unknown or {})


def _jobs_without_monitoring_ns():
    """The pre-fix face with `monitoring` dropped from the monitoring-components
    SD namespace list — the drift the threshold-exporter family exists to catch.
    Mutates the parsed job model, so no repo YAML is touched."""
    jobs = _pre_fix_jobs()
    for j in jobs:
        if j["job"] == "monitoring-components":
            j["sd_namespaces"] = [n for n in j["sd_namespaces"] if n != "monitoring"]
    return jobs


# The 13 threshold-exporter series both rule trees consume (#1285). All emitted
# by one binary / one registry / one /metrics handler, so all share ONE scrape
# provenance — the threshold-exporter Service in ns `monitoring`. Two DIFFERENT
# declaration sites feed that one registry, and both must be named here so
# verify_diff's text_map schedules this test when either moves:
#   components/threshold-exporter/app/collector.go       — user_* + the two
#       tenant_* info metrics + da_config_event (per-scrape ConstMetric)
#   components/threshold-exporter/app/config_metrics.go  — the other 6 da_*
#       (cumulative package-level collectors, registered onto the same registry)
#   components/threshold-exporter/app/main.go            — mounts the handler
_THRESHOLD_EXPORTER_METRICS = (
    "da_config_blast_radius_tenants_affected_bucket",
    "da_config_defaults_shadowed_total",
    "da_config_event",
    "da_config_last_reload_complete_unixtime_seconds",
    "da_config_parse_failure_total",
    "da_config_reload_trigger_total",
    "da_tenant_metrics_over_limit",
    "user_severity_dedup",
    "user_silent_mode",
    "user_state_filter",
    "user_threshold",
    "tenant_metadata_info",
    "tenant_expected_exporter",
)


# ── the live repo ─────────────────────────────────────────────────────────

def test_real_repo_is_green():
    """Post-#1203: 0 DEAD, 0 new unknown-source, 0 stale exemptions."""
    result = gate.run_check()
    assert result["errors"] == [], result["errors"]
    assert result["counts"].get(gate.DEAD, 0) == 0


def test_ledger_is_exactly_the_live_unknown_source_set():
    """Every KNOWN_UNKNOWN_SOURCE entry must still be demanded AND still be
    unknown-source (exit-lock keeps the ledger honest on the real artifacts)."""
    result = gate.run_check()
    assert len(result["infos"]) == len(gate.KNOWN_UNKNOWN_SOURCE) == 9
    assert not any("STALE-EXEMPTION" in e for e in result["errors"])


def test_reconciler_gauges_are_protected_as_one_cohort():
    """TRK-353 / #1250: `channel_up` gets the SAME reachability protection as
    its two siblings on the same federation-reconciler Deployment.

    It used to classify REACHABLE-IF-EXPORTED off a `*_up` suffix shortcut —
    a runtime-optimistic class that is never an error — so the one gauge whose
    whole job is detecting silent evidence-channel failure was itself the one
    gauge with no mechanical protection against silently losing its target.
    """
    classes = gate.run_check()["classes"]
    cohort = ("federation_revocation_channel_up",
              "federation_revocation_tamper_suspected",
              "federation_revocation_last_reconcile_timestamp_seconds")
    for m in cohort:
        assert classes[m][0] == gate.UNKNOWN_SOURCE, (m, classes[m])
        assert m in gate.KNOWN_UNKNOWN_SOURCE, m


def test_repo_victim_metrics_are_reachable_after_fix():
    """The five #1203 victim metrics are REACHABLE on the fixed manifests."""
    classes = gate.run_check()["classes"]
    for m in ("tenant_api_sse_clients", "tenant_api_uptime_seconds",
              "tenant_api_config_reload_failures_total",
              "tenant_api_config_last_reload_successful",
              "alertmanager_notifications_failed_total"):
        assert classes[m][0] == gate.REACHABLE, (m, classes[m])


# ── sanity anchor: cadvisor three-layer parse ─────────────────────────────

def test_cadvisor_three_filter_layers_parse_from_real_configmap():
    """The repo cadvisor job must parse into its three load-bearing layers:
    namespace keep db-.+, a __name__ keep allowlist, and the container ''|POD
    drop. If the parser stops seeing any of them, every container_* verdict
    from this gate is untrustworthy."""
    jobs = gate.load_repo_jobs()
    cad = next(j for j in jobs if j["job"] == "kubelet-cadvisor")
    assert ("keep", ["namespace"], "db-.+") in cad["label_rules"]
    assert ("drop", ["container"], "|POD") in cad["label_rules"]
    keeps = [rx for action, rx in cad["name_rules"] if action == "keep"]
    assert len(keeps) == 1
    # the tight-on-purpose allowlist (see configmap comments): 10 entries
    assert len(keeps[0].split("|")) == 10


# ── sanity anchor: the pre-#1203 face classifies the victims DEAD ─────────

def test_pre_fix_tenant_api_is_dead():
    """Injected pre-fix scrape face: tenant-api svc is annotated but its ns is
    in no SD list → DEAD. This is the regression pin for the #1203 bug."""
    result = _check(
        consumed={"tenant_api_sse_clients": {"platform::TenantApiReadHANeeded"}},
        jobs=_pre_fix_jobs(),
        services=[_TENANT_API_SVC],
    )
    assert result["classes"]["tenant_api_sse_clients"][0] == gate.DEAD
    assert any("tenant_api_sse_clients" in e and "DEAD" in e
               for e in result["errors"]), result["errors"]


def test_pre_fix_unannotated_alertmanager_is_dead():
    result = _check(
        consumed={"alertmanager_notifications_failed_total": {"platform::AmFailing"}},
        jobs=_pre_fix_jobs(),
        services=[_ALERTMANAGER_SVC_BARE],
    )
    cls, reason = result["classes"]["alertmanager_notifications_failed_total"]
    assert cls == gate.DEAD
    assert "annotation" in reason


def test_annotated_alertmanager_is_reachable_even_on_pre_fix_face():
    """The alertmanager fix is annotation-side only — the monitoring ns was
    always in the SD list, so annotating the Service alone revives it."""
    result = _check(
        consumed={"alertmanager_notifications_failed_total": {"platform::AmFailing"}},
        jobs=_pre_fix_jobs(),
        services=[_ALERTMANAGER_SVC_ANNOTATED],
    )
    assert (result["classes"]["alertmanager_notifications_failed_total"][0]
            == gate.REACHABLE)


# ── classification branches ───────────────────────────────────────────────

def test_static_family_is_reachable():
    result = _check(
        consumed={"prometheus_rule_evaluation_failures_total": {"p::x"}},
        jobs=_pre_fix_jobs(), services=[])
    assert (result["classes"]["prometheus_rule_evaluation_failures_total"][0]
            == gate.REACHABLE)


def test_name_filtered_allowlisted_metric_is_reachable():
    result = _check(
        consumed={"container_cpu_usage_seconds_total": {"k::rec"}},
        jobs=_pre_fix_jobs(), services=[])
    assert (result["classes"]["container_cpu_usage_seconds_total"][0]
            == gate.REACHABLE)


def test_name_filtered_rejected_metric_is_dead():
    """A container_* metric outside the cadvisor __name__ allowlist has no
    other source — DEAD (this is what catches an allowlist/consumer drift)."""
    result = _check(
        consumed={"container_network_receive_bytes_total": {"k::rec"}},
        jobs=_pre_fix_jobs(), services=[])
    cls, reason = result["classes"]["container_network_receive_bytes_total"]
    assert cls == gate.DEAD
    assert "allowlist" in reason


def test_tenant_exporter_family_is_reachable_if_exported():
    result = _check(consumed={"mysql_up": {"mariadb::MariaDBClusterDown"}},
                    jobs=_pre_fix_jobs(), services=[])
    assert result["classes"]["mysql_up"][0] == gate.REACHABLE_IF_EXPORTED
    assert result["errors"] == []


def test_every_real_exporter_liveness_gauge_still_routes_by_prefix():
    """No collateral from dropping the `*_up` suffix clause (TRK-353 / #1250).

    Every `<family>_up` gauge the two rule trees actually consume must still
    reach the tenant-exporter face through _FEDERATED_EXPORTER_PREFIXES. Pinned
    as a set, so a future prefix-list edit that orphans one of them is loud.
    """
    consumed = {m: {f"pack::{m}Down"} for m in
                ("mysql_up", "pg_up", "redis_up", "mongodb_up",
                 "oracledb_up", "db2_up")}
    result = _check(consumed=consumed, jobs=_pre_fix_jobs(), services=[])
    for m in consumed:
        assert result["classes"][m][0] == gate.REACHABLE_IF_EXPORTED, (
            m, result["classes"][m])
    assert result["errors"] == []


def test_platform_gauge_ending_in_up_is_not_tenant_exporter_face():
    """The #1250 defect itself, and the mutation proof that the gate now bites.

    A zero-label platform gauge whose name merely ends in `_up` used to be
    classified REACHABLE-IF-EXPORTED with the factually wrong reason
    "tenant-exporters face (annotated db-* ns Service)" — a class that is never
    an error, so the gate stayed silent AND the exit-lock refused to let the
    metric be ledgered (STALE-EXEMPTION). It must now fall through to
    UNKNOWN-SOURCE and, unledgered, be a hard error.
    """
    result = _check(
        consumed={"federation_revocation_channel_up": {"platform::EvidenceChannelDown"}},
        jobs=_pre_fix_jobs(), services=[])
    cls, reason = result["classes"]["federation_revocation_channel_up"]
    assert cls == gate.UNKNOWN_SOURCE, (cls, reason)
    assert "tenant-exporters face" not in reason
    assert any("NEW-UNKNOWN-SOURCE" in e and "federation_revocation_channel_up" in e
               for e in result["errors"]), result["errors"]


def test_platform_gauge_ending_in_up_can_be_ledgered():
    """The other half of the defect: with the shortcut gone, the ledger row the
    exit-lock used to reject is now accepted (INFO, not STALE-EXEMPTION)."""
    result = _check(
        consumed={"federation_revocation_channel_up": {"platform::EvidenceChannelDown"}},
        jobs=_pre_fix_jobs(), services=[],
        known_unknown={"federation_revocation_channel_up": "reconciler helm-only (#1203)"})
    assert result["errors"] == []
    assert any("federation_revocation_channel_up" in i for i in result["infos"])


def test_tenant_exporter_family_dead_without_exporter_face():
    """No job admits an annotated db-* Service → even the runtime-optimistic
    class collapses to DEAD."""
    jobs = [j for j in _pre_fix_jobs() if j["job"] != "tenant-exporters"]
    result = _check(consumed={"mysql_up": {"mariadb::MariaDBClusterDown"}},
                    jobs=jobs, services=[])
    assert result["classes"]["mysql_up"][0] == gate.DEAD


def test_kubelet_prefix_wins_over_kube_prefix():
    """Longest-prefix matching: kubelet_* routes to the volume-stats name
    filter, NOT the kube-state-metrics Service (substring trap pin)."""
    prefix, kind, spec = gate._resolve_family("kubelet_volume_stats_available_bytes")
    assert (kind, spec) == ("name-filtered", "kubelet-volume-stats")
    prefix, kind, spec = gate._resolve_family("kube_pod_info")
    assert kind == "service" and spec == ("monitoring", "kube-state-metrics")


# ── threshold-exporter provenance (#1285) ─────────────────────────────────

def test_threshold_exporter_series_all_route_to_one_family():
    """da_* / user_* / the two tenant_* info metrics share ONE scrape source.

    They used to be excluded from the leaf set outright, on a value-origin
    argument ("conf.d-synthesised"). Value origin is not scrape provenance:
    every one of them is registered on threshold-exporter's single /metrics
    registry, so all 13 must resolve to the same threshold-exporter family.
    """
    for m in _THRESHOLD_EXPORTER_METRICS:
        fam = gate._resolve_family(m)
        assert fam is not None, m
        _key, kind, spec = fam
        # Assert the LITERAL namespace, not gate.THRESHOLD_EXPORTER_NAMESPACE —
        # comparing the constant against itself passes even if someone retargets
        # the family at the wrong namespace (measured: that mutation left this
        # test green).
        assert (kind, spec) == ("helm-service", "monitoring"), (m, fam)


def test_threshold_exporter_series_are_leaves_and_reachable_on_real_repo():
    """All 13 are now judged by this gate, and all pass on the live manifests."""
    result = gate.run_check()
    classes = result["classes"]
    for m in _THRESHOLD_EXPORTER_METRICS:
        assert m in classes, f"{m} is not in the leaf set — the exclusion is back"
        assert classes[m][0] == gate.REACHABLE, (m, classes[m])
    assert result["errors"] == [], result["errors"]


def test_threshold_exporter_family_is_exhaustive_on_the_real_repo():
    """The prefix rows claim the WHOLE `da_`/`user_` namespace for one component.

    That claim is only safe while threshold-exporter is the only emitter. If
    another component (say the federation-gateway chart, helm-only and correctly
    ledgered UNKNOWN-SOURCE for its other metrics) grows a `da_`/`user_`-named
    series, the prefix would hand it a POSITIVELY FALSE provenance — "Service in
    pinned ns 'monitoring'" — and route it around the NEW-UNKNOWN-SOURCE
    exit-lock that exists to force exactly that classification decision.

    Nothing in the gate can detect that, so pin the set instead: a 14th
    `da_`/`user_` leaf must fail here and be triaged by a human.
    """
    classes = gate.run_check()["classes"]
    seen = {m for m in classes if m.startswith(("da_", "user_"))}
    seen |= {m for m in ("tenant_metadata_info", "tenant_expected_exporter")
             if m in classes}
    assert seen == set(_THRESHOLD_EXPORTER_METRICS), (
        "threshold-exporter family membership drifted — confirm the new metric is "
        "really emitted by threshold-exporter (components/threshold-exporter/app/"
        "collector.go or config_metrics.go) before adding it here; if it comes "
        f"from another component the prefix routing is wrong. Diff: "
        f"{seen ^ set(_THRESHOLD_EXPORTER_METRICS)}")


def test_threshold_exporter_series_die_if_a_name_filter_rejects_them():
    """The detection this change ACTUALLY adds, and the only one.

    ⚠️ Do not confuse this with the SD-list drift below: dropping `monitoring`
    from the SD list was ALREADY loud before this change (measured: 24 other
    leaves — alertmanager_* and 22 kube_* — are pinned into the same namespace
    and go DEAD on it, so the gate was never silent about that). The genuinely
    new capability is per-metric: a `__name__` keep/drop in the scraping job
    that rejects THESE families specifically hits nothing else in the repo
    (measured: 13 DEAD, 0 collateral), so before #1285 it was invisible.
    """
    jobs = _pre_fix_jobs()
    for j in jobs:
        if j["job"] == "monitoring-components":
            j["name_rules"].append(("drop", "(da_|user_|tenant_metadata_info).*"))
    consumed = {m: {f"platform::Consumer_{m}"} for m in _THRESHOLD_EXPORTER_METRICS}
    result = _check(consumed=consumed, jobs=jobs, services=[])
    for m in _THRESHOLD_EXPORTER_METRICS:
        if m == "tenant_expected_exporter":
            continue  # deliberately outside the drop regex — see the guard below
        assert result["classes"][m][0] == gate.DEAD, (m, result["classes"][m])
    # Guard against a vacuous pass: a name the filter does NOT reject must
    # survive, otherwise this test would also pass if everything went DEAD.
    assert result["classes"]["tenant_expected_exporter"][0] == gate.REACHABLE


def test_threshold_exporter_series_are_dead_if_monitoring_ns_leaves_sd_list():
    """Namespace/SD-face drift also reaches these 13 now that they are judged.

    Weaker evidence than the test above — this drift was already caught via
    other families in the same namespace — but it pins that the 13 participate
    rather than being silently skipped.
    """
    consumed = {m: {f"platform::Consumer_{m}"} for m in _THRESHOLD_EXPORTER_METRICS}
    result = _check(consumed=consumed, jobs=_jobs_without_monitoring_ns(),
                    services=[])
    for m in _THRESHOLD_EXPORTER_METRICS:
        cls, reason = result["classes"][m]
        assert cls == gate.DEAD, (m, cls, reason)
        assert "monitoring" in reason
    assert len([e for e in result["errors"] if "DEAD" in e]) == len(
        _THRESHOLD_EXPORTER_METRICS), result["errors"]


def test_threshold_exporter_chart_still_hardcodes_the_pinned_namespace():
    """⛔ Pin the EXTERNAL premise the whole family entry rests on.

    THRESHOLD_EXPORTER_NAMESPACE is an ASSERTION, not a check: the gate reads
    only k8s/**, rule-packs/ and the prometheus ConfigMap — never helm/. It is
    sound today solely because the chart template writes `namespace: monitoring`
    literally instead of `{{ .Release.Namespace }}`.

    That is a one-line edit away from false, and a tempting one: the SAME chart
    already uses the templated form in servicemonitor.yaml, so "make the
    namespace handling consistent" would silently turn 13 REACHABLE verdicts
    into fiction with no gate firing (helm/** is not in this hook's files
    filter either). Verifying the chart at gate runtime is deferred (#1286);
    until then this test is the tripwire.
    """
    svc = (REPO_ROOT / "helm" / "threshold-exporter" / "templates" / "service.yaml"
           ).read_text(encoding="utf-8")
    docs = [d for d in yaml.safe_load_all(
        re.sub(r"\{\{-?.*?-?\}\}", "PLACEHOLDER", svc, flags=re.S))
        if isinstance(d, dict)]
    assert docs, "chart Service template no longer parses as YAML"
    ns = docs[0].get("metadata", {}).get("namespace")
    assert ns == gate.THRESHOLD_EXPORTER_NAMESPACE, (
        f"chart Service namespace is now {ns!r}, but the gate assumes "
        f"{gate.THRESHOLD_EXPORTER_NAMESPACE!r}. If the chart was templatised, the "
        "13 threshold-exporter metrics are no longer statically decidable — drop "
        "the threshold-exporter rows from _FAMILY_TABLE/_FAMILY_EXACT FIRST, then "
        "ledger them in KNOWN_UNKNOWN_SOURCE. Ledgering alone does not work: "
        "classify() resolves the family before any ledger lookup, so the metrics "
        "would keep classifying REACHABLE and the rows would trip STALE-EXEMPTION."
    )


# ── helm-service chart face (#1286) ───────────────────────────────────────
# The `helm-service` kind asserts only the JOB side: that some annotation-keep
# job's SD face covers the pinned namespace. That the chart actually RENDERS an
# annotated Service into that namespace is ASSUMED — the gate never opens helm/.
#
# That blindness is deliberate, not laziness: pre-commit has no helm binary (the
# one helm-rendering hook is `stages: [manual]` because rendering the charts
# takes 1-2 min), and the repo's canonical template stripper
# (check_image_pin_capability.strip_helm_actions) DROPS whole-line template
# actions — so it would read threshold-exporter's
# `annotations:\n  {{- toYaml .Values.service.annotations | nindent 4 }}`
# as an EMPTY annotation map and produce a false DEAD on the very chart #1286
# was filed about.
#
# So the chart face is pinned HERE, by rendering. `helm` exists in CI (the
# pytest job installs it via azure/setup-helm precisely so these tests stop
# silently skipping) and in the dev container; locally it skips.
_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm binary not on PATH")

# pinned namespace (the helm-service family's spec) -> chart directory
_HELM_SERVICE_CHART_FACE = {
    "monitoring": "helm/threshold-exporter",
    "vector": "helm/vector",
}

# Charts whose scrape Service does NOT render under DEFAULT values, so the
# gate's REACHABLE verdict for that family currently rests on an operator
# opting in. EXIT-LOCKED: an entry that starts rendering must be deleted (the
# test below fails on it), so this map can only shrink.
_KNOWN_UNRENDERED_UNDER_DEFAULTS = {
    "vector": (
        "both metrics Services are conditional — `-metrics` on metrics.enabled "
        "(default false) and `-projection-gate-metrics` on tenantProjections "
        "(default []). Tracked by #1291, which found the harder blocker too: "
        "the Prometheus egress NetworkPolicy admits neither 9598 nor 9599, so "
        "flipping the chart default alone would still yield zero series."
    ),
}


def _helm_template(chart_rel: str, namespace: str) -> list[dict]:
    """Render a chart with its DEFAULT values into *namespace*."""
    out = subprocess.run(
        ["helm", "template", "t", str(REPO_ROOT / chart_rel), "-n", namespace],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=120)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _scrape_services(docs: list[dict], namespace: str) -> list[dict]:
    """Rendered Services in *namespace* carrying prometheus.io/scrape=true."""
    hits = []
    for d in docs:
        if d.get("kind") != "Service":
            continue
        meta = d.get("metadata") or {}
        # a chart may omit metadata.namespace, in which case it lands in the
        # release namespace we rendered with
        if (meta.get("namespace") or namespace) != namespace:
            continue
        if (meta.get("annotations") or {}).get(gate.SCRAPE_ANNOTATION) == "true":
            hits.append(d)
    return hits


def test_every_helm_service_family_has_a_pinned_chart():
    """Exhaustiveness: adding a helm-service family forces a chart-face decision.

    Without this, a new `("foo_", "helm-service", "some-ns")` row would inherit
    the REACHABLE-by-assumption verdict with nothing anywhere verifying that
    "some-ns" ever receives an annotated Service. Runs without helm.
    """
    specs = {spec for _p, kind, spec in gate._FAMILY_TABLE if kind == "helm-service"}
    specs |= {spec for kind, spec in gate._FAMILY_EXACT.values()
              if kind == "helm-service"}
    assert specs == set(_HELM_SERVICE_CHART_FACE), (
        "helm-service family namespaces and the pinned chart faces disagree — "
        "add the new namespace to _HELM_SERVICE_CHART_FACE (and, if its chart "
        f"does not render a Service under default values, to the ledger). Diff: "
        f"{specs ^ set(_HELM_SERVICE_CHART_FACE)}")


@_needs_helm
def test_helm_service_charts_render_an_annotated_service_under_defaults():
    """#1286: the assumption the REACHABLE verdict rests on, actually checked.

    Renders each pinned chart with DEFAULT values and requires a Service in the
    pinned namespace carrying prometheus.io/scrape=true. Deleting that
    annotation from the chart — the drift the gate cannot see — turns this red.
    """
    for ns, chart in sorted(_HELM_SERVICE_CHART_FACE.items()):
        if ns in _KNOWN_UNRENDERED_UNDER_DEFAULTS:
            continue
        hits = _scrape_services(_helm_template(chart, ns), ns)
        assert hits, (
            f"{chart} renders no {gate.SCRAPE_ANNOTATION}=true Service into ns "
            f"'{ns}' under default values, but the gate classifies that family "
            "REACHABLE on the assumption that it does. Either restore the "
            "chart's scrape annotation, or — when default-off rendering is "
            "deliberate — add the namespace to _KNOWN_UNRENDERED_UNDER_DEFAULTS "
            "with a reason. ⛔ Do NOT reach for KNOWN_UNKNOWN_SOURCE while the "
            "family is still in _FAMILY_TABLE: classify() resolves the family "
            "BEFORE any ledger lookup, so the metric keeps classifying "
            "REACHABLE and the ledger row trips the exit-lock's STALE-EXEMPTION "
            "instead. Ledgering is only correct after the helm-service family "
            "entry itself is removed or reclassified.")


@_needs_helm
def test_known_unrendered_charts_are_exit_locked():
    """The ledger may only shrink: a chart listed as not-rendering that starts
    rendering must be removed, so the exemption cannot outlive its reason."""
    for ns, reason in sorted(_KNOWN_UNRENDERED_UNDER_DEFAULTS.items()):
        chart = _HELM_SERVICE_CHART_FACE[ns]
        hits = _scrape_services(_helm_template(chart, ns), ns)
        assert not hits, (
            f"{chart} NOW renders a scrape-annotated Service into ns '{ns}' "
            f"under default values — delete the '{ns}' row from "
            f"_KNOWN_UNRENDERED_UNDER_DEFAULTS so the chart face is enforced "
            f"instead of exempted. Recorded reason was: {reason}")


def test_exact_family_does_not_swallow_other_charts_tenant_metrics():
    """⛔ Hazard pin: tenant_metadata_info / tenant_expected_exporter are routed
    by EXACT NAME, never by a `tenant_` prefix.

    A `tenant_` prefix entry would also capture tenant_federation_requests_total
    (federation-gateway chart) and tenant_log_query_requests_total
    (tenant-log-query plane). Both install into Release.Namespace, are correctly
    ledgered UNKNOWN-SOURCE, and would be silently reclassified REACHABLE off
    the wrong component's Service — tripping the ledger exit-lock. If a future
    author "simplifies" the exact rows into a prefix, this test must go red.
    """
    for m in ("tenant_federation_requests_total", "tenant_log_query_requests_total"):
        assert gate._resolve_family(m) is None, (m, gate._resolve_family(m))
        cls, _reason = gate.classify(m, _pre_fix_jobs(), [])
        assert cls == gate.UNKNOWN_SOURCE, (m, cls)
        assert m in gate.KNOWN_UNKNOWN_SOURCE, m
    # and on the real artifacts they stay ledgered, not pinned
    classes = gate.run_check()["classes"]
    assert classes["tenant_federation_requests_total"][0] == gate.UNKNOWN_SOURCE
    assert classes["tenant_log_query_requests_total"][0] == gate.UNKNOWN_SOURCE


def test_exact_family_beats_prefix_family(monkeypatch):
    """Exact-name routing must win over prefix routing — an exact row exists
    precisely because the name's prefix belongs to a different component."""
    assert gate._resolve_family("vector_probe_series") == (
        "vector_", "helm-service", gate.VECTOR_NAMESPACE)
    monkeypatch.setitem(gate._FAMILY_EXACT, "vector_probe_series",
                        ("static", "prometheus"))
    assert gate._resolve_family("vector_probe_series") == (
        "vector_probe_series", "static", "prometheus")


def test_vector_family_reachable_via_pinned_ns():
    result = _check(
        consumed={"vector_buffer_discarded_events_total": {"p::VectorBufferEventsDropped"}},
        jobs=_pre_fix_jobs(), services=[])
    cls, reason = result["classes"]["vector_buffer_discarded_events_total"]
    assert cls == gate.REACHABLE
    # The helm-service reason must disclose that only the JOB side was checked,
    # and point at what DOES pin the chart face (#1286) rather than leaving the
    # reader to assume nothing does. It deliberately no longer carries the
    # federation-plane note: that is a vector_-specific scope boundary (module
    # docstring), not a property of the helm-service kind, and printing it on
    # every helm-service family misattributed it to all of them.
    assert "chart face not read here" in reason, reason
    assert "test_check_scrape_reachability" in reason, reason
    assert gate.VECTOR_NAMESPACE in reason, reason


# ── UNKNOWN-SOURCE + exit-lock ────────────────────────────────────────────

def test_new_unknown_source_is_an_error():
    result = _check(consumed={"somepack_brand_new_metric": {"x::A"}},
                    jobs=_pre_fix_jobs(), services=[])
    assert any("NEW-UNKNOWN-SOURCE" in e and "somepack_brand_new_metric" in e
               for e in result["errors"]), result["errors"]


def test_ledgered_unknown_source_is_info_only():
    result = _check(
        consumed={"tenant_federation_requests_total": {"p::FederationAuditPipelineSilent"}},
        jobs=_pre_fix_jobs(), services=[],
        known_unknown={"tenant_federation_requests_total": "helm-only (#1203)"})
    assert result["errors"] == []
    assert any("tenant_federation_requests_total" in i for i in result["infos"])


def test_ledger_entry_no_longer_demanded_is_stale():
    result = _check(consumed={}, jobs=_pre_fix_jobs(), services=[],
                    known_unknown={"tenant_log_query_requests_total": "helm-only"})
    assert any("STALE-EXEMPTION" in e and "demands it" in e
               for e in result["errors"]), result["errors"]


def test_ledger_entry_that_got_pinned_is_stale():
    """A ledgered metric that now classifies (e.g. its family got pinned in
    _FAMILY_TABLE) must leave the ledger — the exit-lock forces shrinkage."""
    result = _check(
        consumed={"vector_tenant_projection_gate_info": {"p::VectorProjectionGateMismatch"}},
        jobs=_pre_fix_jobs(), services=[],
        known_unknown={"vector_tenant_projection_gate_info": "stale ledger row"})
    assert any("STALE-EXEMPTION" in e and "got pinned" in e
               for e in result["errors"]), result["errors"]


# ── leaf boundary ─────────────────────────────────────────────────────────

def test_only_recording_outputs_and_manufactured_series_are_not_leaves():
    """The leaf boundary drops recording-rule outputs and the two series
    Prometheus manufactures — nothing else.

    user_* / da_* used to be dropped here too as "conf.d-synthesised" (#1285).
    That is a claim about a series' VALUE, not about how it reaches Prometheus,
    and it hid 13 metrics from the only gate that asks whether their target is
    scrapeable at all — so they are leaves now.
    """
    leaves = gate.leaf_metrics(
        {"user_threshold": {"a"}, "da_config_event": {"b"}, "up": {"c"},
         "ALERTS": {"g"}, "tenant:mysql_connection_usage:ratio": {"d"},
         "produced_by_tree": {"e"}, "mysql_up": {"f"}},
        outputs={"produced_by_tree"})
    assert set(leaves) == {"mysql_up", "user_threshold", "da_config_event"}


# ── extractor patch (blind spots of the split-tool base) ──────────────────

def test_extractor_catches_metric_before_closing_paren():
    got = gate.extract_metrics(
        "(time() - da_config_last_reload_complete_unixtime_seconds) > 600")
    assert got == {"da_config_last_reload_complete_unixtime_seconds"}


def test_extractor_catches_bare_absent():
    got = gate.extract_metrics(
        "absent(federation_revocation_last_reconcile_timestamp_seconds)")
    assert got == {"federation_revocation_last_reconcile_timestamp_seconds"}


def test_extractor_does_not_leak_grouping_labels_or_keywords():
    got = gate.extract_metrics(
        'max by (mode, namespace) (vector_tenant_projection_gate_info'
        '{category="mismatch"}) > 0')
    assert got == {"vector_tenant_projection_gate_info"}
    assert gate.extract_metrics("vector(1)") == set()


# ── the CLI exit-code contract (main), independent of the real artifacts ──

def test_main_without_ci_is_report_only(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": ["some drift"], "infos": [],
                                 "classes": {}, "counts": {}})
    assert gate.main([]) == gate.EXIT_OK


def test_main_ci_fails_on_errors(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": ["a breach"], "infos": [],
                                 "classes": {}, "counts": {}})
    assert gate.main(["--ci"]) == gate.EXIT_VIOLATION


def test_main_ci_passes_when_clean(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": [], "infos": ["5 ledgered"],
                                 "classes": {}, "counts": {}})
    assert gate.main(["--ci"]) == gate.EXIT_OK


def test_extract_metrics_compact_operator_writing():
    """Metric directly followed by an operator (no whitespace) must still be
    extracted; only function names (followed by `(`) are excluded
    (Gemini review of #1221 — `tenant_api_uptime_seconds>60` was missed)."""
    from check_scrape_reachability import extract_metrics
    assert "tenant_api_uptime_seconds" in extract_metrics("tenant_api_uptime_seconds>60")
    assert "foo_total" in extract_metrics("rate(foo_total[5m])*100")
    assert "rate" not in extract_metrics("rate(foo_total[5m])*100")
