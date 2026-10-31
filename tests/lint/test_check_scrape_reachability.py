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


# The scrape face since #1425: platform chart Services are kept by LABEL in any
# namespace (`platform-components`), and every other job drops that label so a
# platform Service has exactly one scraping job. monitoring-components keeps only
# the raw `monitoring` manifests (alertmanager, kube-state-metrics).
_FACE_PROM_YML = """
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
      - source_labels: [__meta_kubernetes_service_label_dynamic_alerting_io_scrape]
        action: drop
        regex: "platform"
  - job_name: "platform-components"
    kubernetes_sd_configs:
      - role: service
    relabel_configs:
      - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
      - source_labels: [__meta_kubernetes_service_label_dynamic_alerting_io_scrape]
        action: keep
        regex: "platform"
  - job_name: "monitoring-components"
    kubernetes_sd_configs:
      - role: service
        namespaces:
          names: ["monitoring"]
    relabel_configs:
      - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
      - source_labels: [__meta_kubernetes_service_label_dynamic_alerting_io_scrape]
        action: drop
        regex: "platform"
"""


def _face_jobs():
    return gate.parse_scrape_jobs(_FACE_PROM_YML)


def _face_job(jobs, name):
    return next(j for j in jobs if j["job"] == name)


def _check(consumed, jobs, services, known_unknown=None):
    return gate.run_check(
        consumed=consumed, outputs=set(), jobs=jobs, services=services,
        known_unknown=known_unknown or {})


# The 17 threshold-exporter series both rule trees consume (#1285;
# da_config_scan_failures_total and
# da_config_last_scan_complete_unixtime_seconds since #2452's
# ConfigScanFailing; da_config_unreadable_files and
# da_config_defaults_unusable since #2592's ConfigFilesUnreadable /
# ConfigDefaultsUnusable). All emitted
# by one binary / one registry / one /metrics handler, so all share ONE scrape
# provenance — the threshold-exporter Service in ns `monitoring`. Two DIFFERENT
# declaration sites feed that one registry, and both must be named here so
# verify_diff's text_map schedules this test when either moves:
#   components/threshold-exporter/app/internal/scrape/collector.go — user_* +
#       the two tenant_* info metrics + da_config_event +
#       da_tenant_metrics_over_limit (per-scrape ConstMetric); wired in by
#       components/threshold-exporter/app/collector.go
#   components/threshold-exporter/app/internal/scrape/config_metrics.go — the
#       other 9 da_* (cumulative collectors, registered onto the same registry);
#       mutated through components/threshold-exporter/app/config_metrics.go
#   components/threshold-exporter/app/main.go            — mounts the handler
_THRESHOLD_EXPORTER_METRICS = (
    "da_config_blast_radius_tenants_affected_bucket",
    "da_config_defaults_shadowed_total",
    "da_config_event",
    "da_config_last_reload_complete_unixtime_seconds",
    "da_config_last_scan_complete_unixtime_seconds",
    "da_config_parse_failure_total",
    "da_config_reload_trigger_total",
    "da_config_scan_failures_total",
    "da_config_unreadable_files",
    "da_config_defaults_unusable",
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
    """Injected pre-fix scrape face: no job keeps tenant-api's Service → DEAD.
    This is the regression pin for the #1203 bug. (Since #1594 the family is
    helm-service, so the injected Service is not consulted — the verdict rests
    on the job face alone, which since #1425 means a job keeping the platform
    scrape label in every namespace; the chart's annotation and label are
    pinned by the render test.)"""
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
    registry, so all 15 must resolve to the same threshold-exporter family.
    """
    for m in _THRESHOLD_EXPORTER_METRICS:
        fam = gate._resolve_family(m)
        assert fam is not None, m
        _key, kind, spec = fam
        # Assert the LITERAL chart, not gate.THRESHOLD_EXPORTER_CHART —
        # comparing the constant against itself passes even if someone retargets
        # the family at the wrong chart (measured: that mutation left this test
        # green when the spec was a namespace).
        assert (kind, spec) == ("helm-service", "helm/threshold-exporter"), (m, fam)


def test_threshold_exporter_series_are_leaves_and_reachable_on_real_repo():
    """All 15 are now judged by this gate, and all pass on the live manifests."""
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
    series, the prefix would hand it a POSITIVELY FALSE provenance — "the
    threshold-exporter chart's Service" — and route it around the NEW-UNKNOWN-SOURCE
    exit-lock that exists to force exactly that classification decision.

    Nothing in the gate can detect that, so pin the set instead: a 14th
    `da_`/`user_` leaf must fail here and be triaged by a human.
    """
    classes = gate.run_check()["classes"]
    # `da_generator_` is the generator container's, routed to tenant-api by
    # its own (longer) prefix row (#2903) — pinned by the next test.
    seen = {m for m in classes if m.startswith(("da_", "user_"))
            and not m.startswith("da_generator_")}
    seen |= {m for m in ("tenant_metadata_info", "tenant_expected_exporter")
             if m in classes}
    assert seen == set(_THRESHOLD_EXPORTER_METRICS), (
        "threshold-exporter family membership drifted — confirm the new metric is "
        "really emitted by threshold-exporter "
        "(components/threshold-exporter/app/collector.go or config_metrics.go) "
        "before adding it here; if it comes "
        f"from another component the prefix routing is wrong. Diff: "
        f"{seen ^ set(_THRESHOLD_EXPORTER_METRICS)}")


def test_generator_family_is_tenant_apis_not_threshold_exporters():
    """#2903: the generator container has only a Unix socket; tenant-api
    appends its /metrics, so every `da_generator_*` series comes from the
    tenant-api target. Longest-prefix matching must route the family there,
    never through `da_` to threshold-exporter — and the rules that read it
    (GeneratorJudgementMemoryNearLimit) must find it reachable."""
    key, kind, spec = gate._resolve_family("da_generator_judgement_peak_rss_bytes")
    # Literal chart, not gate.TENANT_API_CHART: the row and the constant must
    # not be able to drift together.
    assert (key, kind, spec) == ("da_generator_", "helm-service", "helm/tenant-api")
    classes = gate.run_check()["classes"]
    gen = {m for m in classes if m.startswith("da_generator_")}
    assert gen >= {"da_generator_judgement_peak_rss_bytes", "da_generator_judgement_memory_limit_bytes"}, gen


def test_threshold_exporter_series_die_if_a_name_filter_rejects_them():
    """The detection this change ACTUALLY adds, and the only one.

    The capability is per-metric: a `__name__` keep/drop in the scraping job
    that rejects THESE families specifically hits nothing else in the repo
    (measured: 13 DEAD, 0 collateral), so before #1285 it was invisible.
    """
    jobs = _face_jobs()
    _face_job(jobs, "platform-components")["name_rules"].append(
        ("drop", "(da_|user_|tenant_metadata_info).*"))
    consumed = {m: {f"platform::Consumer_{m}"} for m in _THRESHOLD_EXPORTER_METRICS}
    result = _check(consumed=consumed, jobs=jobs, services=[])
    for m in _THRESHOLD_EXPORTER_METRICS:
        if m == "tenant_expected_exporter":
            continue  # deliberately outside the drop regex — see the guard below
        assert result["classes"][m][0] == gate.DEAD, (m, result["classes"][m])
    # Guard against a vacuous pass: a name the filter does NOT reject must
    # survive, otherwise this test would also pass if everything went DEAD.
    assert result["classes"]["tenant_expected_exporter"][0] == gate.REACHABLE


@pytest.mark.parametrize("drift", ["namespace-listed", "label-keep-lost"])
def test_threshold_exporter_series_are_dead_if_the_platform_job_drifts(drift):
    """#1425: the family's premise is a job keeping the platform label in EVERY
    namespace — not any particular namespace. Both ways that premise can break
    must turn all 15 DEAD: the job growing a namespace list again (the release
    is installed into Release.Namespace, which no list can name), and the job
    losing its label keep (it would then select nothing of the charts').
    """
    jobs = _face_jobs()
    job = _face_job(jobs, "platform-components")
    if drift == "namespace-listed":
        job["sd_namespaces"] = ["monitoring"]
    else:
        job["platform_label"] = None
    consumed = {m: {f"platform::Consumer_{m}"} for m in _THRESHOLD_EXPORTER_METRICS}
    result = _check(consumed=consumed, jobs=jobs, services=[])
    for m in _THRESHOLD_EXPORTER_METRICS:
        cls, reason = result["classes"][m]
        assert cls == gate.DEAD, (m, cls, reason)
        assert gate.PLATFORM_SCRAPE_LABEL in reason
    assert len([e for e in result["errors"] if "DEAD" in e]) == len(
        _THRESHOLD_EXPORTER_METRICS), result["errors"]


def test_threshold_exporter_series_are_reachable_on_the_label_face():
    """Control for the drift test: the unmodified face keeps them REACHABLE."""
    consumed = {m: {f"platform::Consumer_{m}"} for m in _THRESHOLD_EXPORTER_METRICS}
    result = _check(consumed=consumed, jobs=_face_jobs(), services=[])
    for m in _THRESHOLD_EXPORTER_METRICS:
        assert result["classes"][m][0] == gate.REACHABLE, (m, result["classes"][m])
    assert result["errors"] == [], result["errors"]


# ── one scraping job per platform Service (#1425) ─────────────────────────

def test_real_configmap_parses_the_label_face():
    """The repo Prometheus config: platform-components keeps the label in every
    namespace; monitoring-components and tenant-exporters drop it."""
    jobs = gate.load_repo_jobs()
    plat = _face_job(jobs, "platform-components")
    assert gate._keeps_platform_everywhere(plat), plat
    for name in ("monitoring-components", "tenant-exporters"):
        assert _face_job(jobs, name)["platform_label"] == "drop", name


@pytest.mark.parametrize("job_name,ns", [("monitoring-components", "monitoring"),
                                         ("tenant-exporters", "db-probe")])
def test_a_job_that_keeps_a_platform_service_too_is_an_error(job_name, ns):
    """Without its drop, a platform Service is kept by two jobs. In a db-*
    namespace the tenant job also rewrites `job` and `tenant`, so
    up{job="threshold-exporter"} loses its target."""
    jobs = _face_jobs()
    _face_job(jobs, job_name)["platform_label"] = None
    errors = gate._platform_double_scrape(jobs)
    assert any("PLATFORM-DOUBLE-SCRAPE" in e and f"ns '{ns}'" in e
               and job_name in e for e in errors), errors


def test_the_label_face_scrapes_each_platform_service_once():
    """Control: the face with both drops reports nothing."""
    assert gate._platform_double_scrape(_face_jobs()) == []


# ── helm-service chart face (#1286) ───────────────────────────────────────
# The `helm-service` kind asserts only the JOB side: that some annotation-keep
# job keeps the platform scrape label in every namespace. That the chart
# actually RENDERS a Service carrying the annotation AND the label is ASSUMED —
# the gate never opens helm/.
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

# The helm-service families' specs: the charts their targets come from.
_HELM_SERVICE_CHARTS = {"helm/threshold-exporter", "helm/vector", "helm/tenant-api"}

# Rendered into a namespace no Prometheus job names, on purpose: since #1425 the
# install namespace is no premise, so a chart face that only held in one
# namespace would be the drift this catches.
_RENDER_NAMESPACE = "da-render-probe"

# Charts that refuse to render under bare defaults by design: each needs one
# operator choice that has nothing to do with its scrape Service. Rendered with
# the minimum that choice requires — tenant-api refuses until writeBack.mode is
# chosen (#2855) and needs a git repository in conf.d (#2903); the shared test
# baseline picks acknowledged direct mode + a hostPath conf.d, adding no
# Service or annotation of its own.
_RENDER_MINIMUM = {
    "helm/tenant-api": ["-f", str(REPO_ROOT / "tests" / "helm" / "tenant_api_render_baseline.yaml")],
}

# Charts whose scrape Service does NOT render under DEFAULT values, so the
# gate's REACHABLE verdict for that family currently rests on an operator
# opting in. EXIT-LOCKED: an entry that starts rendering must be deleted (the
# test below fails on it), so this map can only shrink.
_KNOWN_UNRENDERED_UNDER_DEFAULTS = {
    "helm/vector": (
        "both metrics Services are conditional — `-metrics` on metrics.enabled "
        "(default false) and `-projection-gate-metrics` on tenantProjections "
        "(default []). Tracked by #1291, which found the harder blocker too: "
        "the Prometheus egress NetworkPolicy admits neither 9598 nor 9599, so "
        "flipping the chart default alone would still yield zero series."
    ),
}


def _helm_template(chart_rel: str, *extra: str) -> list[dict]:
    """Render a chart with its DEFAULT values (plus _RENDER_MINIMUM and *extra*)."""
    out = subprocess.run(
        ["helm", "template", "t", str(REPO_ROOT / chart_rel), "-n", _RENDER_NAMESPACE,
         *_RENDER_MINIMUM.get(chart_rel, []), *extra],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=120)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _scrape_services(docs: list[dict]) -> list[dict]:
    """Rendered Services carrying prometheus.io/scrape=true AND the platform
    scrape label — what `platform-components` keeps (#1425)."""
    hits = []
    for d in docs:
        if d.get("kind") != "Service":
            continue
        meta = d.get("metadata") or {}
        if ((meta.get("annotations") or {}).get(gate.SCRAPE_ANNOTATION) == "true"
                and (meta.get("labels") or {}).get(gate.PLATFORM_SCRAPE_LABEL)
                == gate.PLATFORM_SCRAPE_VALUE):
            hits.append(d)
    return hits


def test_every_helm_service_family_has_a_pinned_chart():
    """Exhaustiveness: adding a helm-service family forces a chart-face decision.

    Without this, a new `("foo_", "helm-service", "helm/foo")` row would inherit
    the REACHABLE-by-assumption verdict with nothing anywhere verifying that
    helm/foo ever renders a labelled, annotated Service. Runs without helm.
    """
    specs = {spec for _p, kind, spec in gate._FAMILY_TABLE if kind == "helm-service"}
    specs |= {spec for kind, spec in gate._FAMILY_EXACT.values()
              if kind == "helm-service"}
    assert specs == _HELM_SERVICE_CHARTS, (
        "helm-service family charts and the rendered chart faces disagree — "
        "add the new chart to _HELM_SERVICE_CHARTS (and, if it does not render a "
        f"Service under default values, to the ledger). Diff: "
        f"{specs ^ _HELM_SERVICE_CHARTS}")


@_needs_helm
def test_helm_service_charts_render_an_annotated_service_under_defaults():
    """#1286: the assumption the REACHABLE verdict rests on, actually checked.

    Renders each chart with DEFAULT values, into a namespace no job names, and
    requires a Service carrying prometheus.io/scrape=true AND the platform
    scrape label. Deleting either from the chart — the drift the gate cannot
    see — turns this red.
    """
    for chart in sorted(_HELM_SERVICE_CHARTS):
        if chart in _KNOWN_UNRENDERED_UNDER_DEFAULTS:
            continue
        hits = _scrape_services(_helm_template(chart))
        assert hits, (
            f"{chart} renders no Service carrying {gate.SCRAPE_ANNOTATION}=true and "
            f"{gate.PLATFORM_SCRAPE_LABEL}={gate.PLATFORM_SCRAPE_VALUE} under "
            "default values, but the gate classifies that family REACHABLE on "
            "the assumption that it does. Either restore the chart's scrape "
            "annotation and label, or — when default-off rendering is "
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
    for chart, reason in sorted(_KNOWN_UNRENDERED_UNDER_DEFAULTS.items()):
        hits = _scrape_services(_helm_template(chart))
        assert not hits, (
            f"{chart} NOW renders a labelled, scrape-annotated Service under "
            f"default values — delete the '{chart}' row from "
            f"_KNOWN_UNRENDERED_UNDER_DEFAULTS so the chart face is enforced "
            f"instead of exempted. Recorded reason was: {reason}")


@_needs_helm
def test_vectors_opt_in_services_carry_the_label_when_rendered():
    """The exemption above covers only "not rendered under DEFAULTS". Once an
    operator opts in, both Vector Services must be ones `platform-components`
    keeps — otherwise opting in would still yield zero series, by label this
    time rather than by namespace (#1425)."""
    docs = _helm_template(
        "helm/vector", "--set", "metrics.enabled=true",
        "--set", "tenantProjections[0].tenantId=tenant-alpha",
        "--set", "tenantProjections[0].accountId=1000",
        "--set", "projectionGate.registry.configMapName=test-registry")
    names = sorted(d["metadata"]["name"] for d in _scrape_services(docs))
    assert names == ["vector-metrics", "vector-projection-gate-metrics"], names


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
        "vector_", "helm-service", gate.VECTOR_CHART)
    monkeypatch.setitem(gate._FAMILY_EXACT, "vector_probe_series",
                        ("static", "prometheus"))
    assert gate._resolve_family("vector_probe_series") == (
        "vector_probe_series", "static", "prometheus")


def test_vector_family_reachable_via_the_platform_label():
    result = _check(
        consumed={"vector_buffer_discarded_events_total": {"p::VectorBufferEventsDropped"}},
        jobs=_face_jobs(), services=[])
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
    assert gate.VECTOR_CHART in reason and "every namespace" in reason, reason


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


def test_a_refused_manifest_is_named_not_skipped(tmp_path):
    """#3426: a manifest kubectl's reading refuses used to be skipped (its
    Services silently missing); every one is named now, with its reason."""
    good = tmp_path / "svc.yaml"
    good.write_text("apiVersion: v1\nkind: Service\nmetadata:\n  name: s\n", encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: v1\nkind: Service\n---\n- not an object\n", encoding="utf-8")
    nokind = tmp_path / "nokind.yaml"
    nokind.write_text("metadata: {name: x}\n", encoding="utf-8")
    with pytest.raises(gate._lib_consumer_read.InputRefused) as exc:
        gate.parse_services([good, bad, nokind])
    assert "2 manifest(s)" in str(exc.value)
    lines = exc.value.stderr_lines
    assert any(str(bad) in ln and "the document is array" in ln for ln in lines), lines
    assert any(str(nokind) in ln and "it has no `kind`" in ln for ln in lines), lines
    assert gate.parse_services([good]) == [{"name": "s", "namespace": None, "annotations": {}}]


def test_a_crashed_da_crdecode_is_raised_not_a_skipped_file(tmp_path, monkeypatch):
    """#3407 review: a da-crdecode that crashes (a Go panic exits 2 with stderr, as a
    refusal does) is no answer about the file, so it is raised (a caller
    error in `main`) — never a manifest silently missing its Services."""
    svc = tmp_path / "svc.yaml"
    svc.write_text("apiVersion: v1\nkind: Service\nmetadata:\n  name: s\n", encoding="utf-8")
    fake = tmp_path / "fake-da-crdecode"
    fake.write_text("#!/bin/sh\necho 'panic: boom' >&2\nexit 2\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("DA_CRDECODE_BINARY", str(fake))
    with pytest.raises(gate._lib_consumer_read.ConsumerReadError) as exc:
        gate.parse_services([svc])
    assert not isinstance(exc.value, gate._lib_consumer_read.InputRefused)
