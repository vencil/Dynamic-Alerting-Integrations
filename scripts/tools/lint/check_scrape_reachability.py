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
                        * `helm-service` families (vector_, tenant_api_ /
                          da_generator_, the threshold-exporter families
                          below, and since #1595 D1b the federation gateway's
                          and reconciler's) — no repo manifest
                          pins the target, so only the JOB side is checked: that
                          some job keeps annotated Services carrying the
                          platform scrape label (PLATFORM_SCRAPE_LABEL) in EVERY
                          namespace, and that no __name__ filter rejects the
                          name. The install namespace is no premise (#1425):
                          the charts render into Release.Namespace, and the job
                          selects by label, not by a namespace list. That the
                          chart's Service actually carries the annotation and
                          the label is not verified HERE — this gate never
                          opens helm/. The chart face is pinned instead by
                          tests/lint/test_check_scrape_reachability.py, which
                          renders each chart and requires such a Service
                          (#1286). Two job-side faults are errors of their
                          own: a platform Service kept by more than one job
                          (PLATFORM-DOUBLE-SCRAPE — endpoints / pod role jobs
                          count as keeping it unless they drop the label, and
                          each job's namespace keep regex picks the namespaces
                          probed), a label-keeping job whose `job` is not
                          the Service name (PLATFORM-JOB-LABEL), and a
                          label-keeping job that does not scrape every pod
                          exactly once (PLATFORM-PER-POD, #3483: `role:
                          service` reaches one replica per scrape; an
                          endpointslice role without the annotated-port keep
                          scrapes a pod once per Service port).
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
  (1) chart-side ANNOTATION and LABEL drift — this gate still never opens
      helm/, so neither moves a verdict here. Both are tripwired from the TEST
      SUITE instead (#1286): it renders each chart with `helm template` and
      requires a Service carrying the scrape annotation and the platform
      scrape label. Those tests need the helm binary, so they are real gates
      in CI and skip locally. (The install namespace stopped being a premise
      in #1425: the job no longer lists namespaces for these charts.)
  (2) NETWORK REACHABILITY. This gate models SD + annotations + __name__
      filters — it does NOT model NetworkPolicy. A scrape the egress policy
      denies still reads REACHABLE. That is not hypothetical: #1291 found all
      four vector_* consumers inert because `allow-prometheus-egress` admitted
      neither 9598 nor 9599. The egress half is now pinned from the test suite
      (tests/k8s/test_prometheus_scrape_egress.py), not by this gate.
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

SCOPE BOUNDARY (Prometheus Operator): this gate judges the raw scrape config
only. Whether a kube-prometheus-stack install has each source is the same
table asked a different question — `_lib_metric_sources.operator_split`, which
`operator-generate` runs on the platform pack (#1595 D1b).

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_OPS = PROJECT_ROOT / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))
from generate_rule_pack_split import (  # noqa: E402
    extract_recording_outputs,
    is_federated_exporter_metric,
)

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, str(_THIS_DIR.parent))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402
import _lib_metric_sources  # noqa: E402  (the family table, #1595 D1b)
import _lib_consumer_read  # noqa: E402  (ADR-036 §12: prometheus.yml / manifests as their consumers read them)
# The repo's one content-based rule-tree scanner. Same directory, zero
# import cost — see collect_rule_trees for why this file no longer globs.
from _rule_tree import _rule_containers  # noqa: E402

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

# The platform scrape label (#1425). The threshold-exporter, Vector and
# tenant-api charts put it on their scrape Services (helm/*/templates/
# service*.yaml), and Prometheus' `platform-components` job keeps Services by
# it in every namespace. The namespace a release is installed into is therefore
# no premise of this gate. It used to be: three constants (monitoring / vector /
# tenant-api) that an SD namespace list had to match by hand while the charts
# render into Release.Namespace — and `da-tools init --namespace` invites any
# other namespace, which then was silently never scraped.
PLATFORM_SCRAPE_LABEL = "dynamic-alerting.io/scrape"
PLATFORM_SCRAPE_VALUE = "platform"
#: The label as Prometheus' kubernetes SD exposes it: every character outside
#: [a-zA-Z0-9_] becomes `_`, under `__meta_kubernetes_service_label_`.
PLATFORM_SCRAPE_META = ("__meta_kubernetes_service_label_"
                        + re.sub(r"[^a-zA-Z0-9_]", "_", PLATFORM_SCRAPE_LABEL))
#: The meta label kubernetes_sd sets to the Service's name.
SERVICE_NAME_META = "__meta_kubernetes_service_name"
#: The meta label of the Service's `prometheus.io/port` annotation.
SCRAPE_PORT_META = "__meta_kubernetes_service_annotation_prometheus_io_port"
#: The endpointslice role's port of the referenced endpoint — the pod port the
#: target reaches (absent on the extra container-port targets).
ENDPOINTSLICE_PORT_META = "__meta_kubernetes_endpointslice_port"

#: Kubernetes SD roles whose targets carry the Service's
#: __meta_kubernetes_service_* labels, so the gate's model of the Service
#: selection face (annotation keep, platform label, namespace) applies to them.
#: `endpointslice` / `endpoints` yield one target per pod address × port; the
#: `service` role yields one per Service port — its scrape lands on whichever
#: replica the Service address routes to (#3483).
_SERVICE_FACE_ROLES = ("service", "endpoints", "endpointslice")
#: Of those, the roles that make every pod a target (#3483).
_PER_POD_ROLES = ("endpoints", "endpointslice")

# The family table (metric family → expected source) and the charts behind the
# helm-service families live in `scripts/tools/_lib_metric_sources.py` since
# #1595 D1b: `da-tools operator-generate` asks the same table whether a
# Prometheus Operator install has each source, and it runs inside the da-tools
# image, which does not ship this lint. Re-exported under the names this gate
# and its tests have always used — the SAME objects, so there is one copy.
# tests/lint/test_check_scrape_reachability.py renders each helm-service chart
# and requires a Service carrying the scrape annotation and the label.
THRESHOLD_EXPORTER_CHART = _lib_metric_sources.THRESHOLD_EXPORTER_CHART
VECTOR_CHART = _lib_metric_sources.VECTOR_CHART
TENANT_API_CHART = _lib_metric_sources.TENANT_API_CHART
FEDERATION_GATEWAY_CHART = _lib_metric_sources.FEDERATION_GATEWAY_CHART
FEDERATION_RECONCILER_CHART = _lib_metric_sources.FEDERATION_RECONCILER_CHART
# kind: static / service / helm-service / name-filtered — see the table's
# comment. Families not listed fall through to the tenant-exporter face (the
# is_federated_exporter_metric SSOT) and then UNKNOWN-SOURCE.
# _FAMILY_EXACT is consulted FIRST — see _resolve_family.
_FAMILY_TABLE = _lib_metric_sources.FAMILY_TABLE
_FAMILY_EXACT = _lib_metric_sources.FAMILY_EXACT

# Helm-only components whose Service no job is known to select wherever they
# are installed: NOT judged dead here, ledger-managed instead. Exit-locked:
# entries must stay demanded AND stay unknown-source, else they must be removed
# (see module docstring).
# EMPTY since #1595 D1b: the nine rows that sat here were the federation
# gateway's and reconciler's series. Both charts now label their scrape
# Service `dynamic-alerting.io/scrape: platform`, so `platform-components`
# keeps them in any namespace and the families are pinned in _FAMILY_TABLE /
# _FAMILY_EXACT (helm-service) like threshold-exporter's. Before that their
# Services were scraped only when installed into `monitoring`
# (`monitoring-components`), which is why they could not be judged.
KNOWN_UNKNOWN_SOURCE: dict[str, str] = {}

# ── federation PLANE provenance (TRK-340 / #1203 part 2) ───────────────────
# Which CLUSTER each unpinned-namespace source is ASSUMED to run on, under the
# federation topology (platform rule tree on central). Consumed by
# TestPlatformAlertPlaneContract; this file only owns the fact, not the
# assertion.
#
# ⚠️ NOT a comprehension over KNOWN_UNKNOWN_SOURCE, deliberately: deriving it
# would hand every future ledger entry a silent "central" — the exact default
# this table exists to prevent. The contract pins the two key sets equal, so a
# new ledger row without a plane decision is a red test, not a default.
#
# Empty with the ledger (#1595 D1b). The federation charts' series it used to
# place on central are helm-service families now, which the plane contract
# places by family like threshold-exporter's and tenant-api's. ⚠️ The
# asymmetry recorded here before still holds and is invisible from the alert
# side: `tenant_log_query_requests_total` is `by account_id, project_id,
# status` (helm/federation-gateway/files/audit-metrics.vector.yaml, `m_lq`) —
# NO `tenant` label — so a gateway installed on an EDGE cluster blinds the
# alert reading it on central and cannot be federated back (the
# `match[]={tenant!=""}` selector skips it), while its sibling
# `tenant_federation_requests_total` (`by tenant, status`) would federate.
PLANE_OF_UNPINNED_SOURCE: dict[str, str] = {}

# ── PromQL extraction ───────────────────────────────────────────────────────
# The patched superset of the split tool's extractor lives with the family
# table (scripts/tools/_lib_metric_sources.py, #1595 D1b) — operator-generate
# reads rules with it too. Re-exported here: the plane contract and this
# gate's tests import it from this module.
extract_metrics = _lib_metric_sources.extract_metrics
_EXTRA_KEYWORDS = _lib_metric_sources.EXTRA_KEYWORDS


# ── scrape-face parsing ─────────────────────────────────────────────────────

def parse_scrape_jobs(prom_yaml_text: str) -> list[dict]:
    """Parse a prometheus.yml document into the job model this gate evaluates.

    Per job: static self-scrape flag, SD role + namespace list, the
    annotation/namespace target-selection keeps (relabel_configs), and the
    __name__-level keep/drop rules plus other label keep/drops
    (metric_relabel_configs).

    ADR-036 §12 (#3206): the text is read the way Prometheus reads it —
    ``da-read prometheus`` (Prometheus' own ``config.Load``) — which returns
    only the keys the text writes, each value Prometheus' reading (a relabel
    ``action`` lower-cased, a regex as Prometheus prints it). A key it leaves
    out is Prometheus' default, which the ``.get`` defaults below spell. A
    config Prometheus refuses raises ``_lib_consumer_read.ConsumerReadError``.
    """
    prom = _lib_consumer_read.prometheus_config(prom_yaml_text)
    jobs: list[dict] = []
    for sc in prom.get("scrape_configs", []) or []:
        job = {
            "job": sc.get("job_name"),
            "static": bool(sc.get("static_configs")),
            "sd_role": None,
            # every kubernetes_sd_configs entry's role, in order (sd_role is the
            # last one; a platform job with more than one is an error, #3483)
            "sd_roles": [],
            "sd_namespaces": [],
            "annotation_keep": False,
            "ns_keep_regex": None,
            # "keep" / "drop" when a relabel step keeps or drops Services whose
            # platform scrape label is PLATFORM_SCRAPE_VALUE (#1425), else None
            "platform_label": None,
            # True when the LAST relabel step writing `job` copies the Service
            # name verbatim (#1425: platform alerts select up{job="<service>"})
            "job_from_service_name": False,
            # True when a `keepequal` keeps only the target whose __address__
            # port is the annotated scrape port (#3483: an endpoints-role job
            # otherwise scrapes each pod once per Service port)
            "port_select": False,
            "name_rules": [],   # [(action, regex)] over __name__
            "label_rules": [],  # [(action, source_labels, regex)] other keep/drop
        }
        for sd in sc.get("kubernetes_sd_configs", []) or []:
            job["sd_role"] = sd.get("role")
            job["sd_roles"].append(sd.get("role"))
            job["sd_namespaces"] = list(
                ((sd.get("namespaces") or {}).get("names")) or [])
        # labels holding the endpoint's port: the endpointslice role supplies
        # one; a `replace` capturing __address__'s port adds others
        port_labels: set[str] = (
            {ENDPOINTSLICE_PORT_META} if job["sd_roles"] == ["endpointslice"] else set())
        address_rewritten = False
        for rc in sc.get("relabel_configs", []) or []:
            src = rc.get("source_labels", [])
            action = rc.get("action", "replace")
            target = rc.get("target_label")
            if action == "keepequal":
                # Either operand order: keepequal compares the joined
                # source_labels with the value of target_label.
                if ((src == [SCRAPE_PORT_META] and target in port_labels)
                        or (len(src) == 1 and src[0] in port_labels
                            and target == SCRAPE_PORT_META)):
                    job["port_select"] = True
                continue
            if action in ("labeldrop", "labelkeep"):
                regex = rc.get("regex", "(.*)")
                port_labels = {lbl for lbl in port_labels
                               if (re.fullmatch(regex, lbl) is None) == (action == "labeldrop")}
            elif action == "labelmap":
                port_labels.clear()   # may write any label: assume the worst
            elif target is not None:
                if target == "__address__":
                    # From here __address__'s port is no longer the endpoint's
                    # (e.g. forced to the annotated port), so comparing it with
                    # the annotation selects nothing — and a rewrite AFTER the
                    # keep sends the scrape to another port than the one kept.
                    port_labels.clear()
                    address_rewritten = True
                    job["port_select"] = False
                elif (action == "replace" and src == ["__address__"]
                      and not address_rewritten and _captures_port(rc)):
                    port_labels.add(target)
                else:
                    port_labels.discard(target)
            if (rc.get("action", "replace") == "replace"
                    and rc.get("target_label") == "job"):
                job["job_from_service_name"] = (
                    src == [SERVICE_NAME_META]
                    and rc.get("regex", "(.*)") == "(.*)"
                    and rc.get("replacement", "$1") == "$1")
            if (src == [PLATFORM_SCRAPE_META] and rc.get("action") in ("keep", "drop")
                    and rc.get("regex") == PLATFORM_SCRAPE_VALUE):
                job["platform_label"] = rc.get("action")
                continue
            if rc.get("action") != "keep":
                continue
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


def _captures_port(rc: dict) -> bool:
    """Whether a `replace` step writes exactly __address__'s port: its regex,
    anchored as Prometheus anchors it, turns `<ip>:<port>` (IPv4 and the
    bracketed IPv6 form) into the bare port through its replacement."""
    regex = rc.get("regex", "(.*)")
    replacement = rc.get("replacement", "$1")
    py_repl = re.sub(r"\$\{?(\d+)\}?", r"\\\1", replacement)
    for address, port in (("10.0.0.1:8080", "8080"), ("[fd00::1]:9598", "9598")):
        try:
            m = re.fullmatch(regex, address)
            if m is None or m.expand(py_repl) != port:
                return False
        except (re.error, IndexError):
            return False
    return True


#: The ConfigMap key Prometheus' config is mounted from.
PROM_CONFIGMAP_KEY = "prometheus.yml"


def load_repo_jobs() -> list[dict]:
    """The scrape jobs of the repo's Prometheus ConfigMap: the outer layer
    read the way kubectl reads it (``da-crdecode``), the ``prometheus.yml``
    string inside it the way Prometheus does (``parse_scrape_jobs``)."""
    holders = [doc["data"][PROM_CONFIGMAP_KEY]
               for doc in _lib_consumer_read.kubernetes_documents(str(PROM_CONFIGMAP))
               if doc["kind"] == "ConfigMap"   # every doc an object (#3426)
               and isinstance(doc.get("data"), dict) and PROM_CONFIGMAP_KEY in doc["data"]]
    rel = PROM_CONFIGMAP.relative_to(PROJECT_ROOT).as_posix()
    if len(holders) != 1 or not isinstance(holders[0], str):
        raise _lib_consumer_read.ConsumerReadError(
            f"{rel}: expected exactly one ConfigMap with a string `data.{PROM_CONFIGMAP_KEY}` "
            f"in kubectl's reading of the file, found {len(holders)}")
    return parse_scrape_jobs(holders[0])


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
    """Every `kind: Service` doc found in *paths* → {name, namespace, annotations, labels},
    each file read the way kubectl reads it (da-crdecode, ADR-036 §12, #3206).
    No da-crdecode raises ConsumerReadError; files kubectl's reading refuses
    raise InputRefused naming every one, with its reason (#3426) — both
    caller errors in `main`. A refused file used to be skipped: a Service in
    it was then silently missing, and a metric it serves read as unscraped."""
    services: list[dict] = []
    read = _lib_consumer_read.kubernetes_documents_of(paths, keep_refusals=True)
    refused = [(p, read[str(p)]) for p in paths
               if isinstance(read[str(p)], _lib_consumer_read.ConsumerReadError)]
    if refused:
        raise _lib_consumer_read.InputRefused(
            f"{len(refused)} manifest(s) kubectl's reading refuses, so their Services "
            f"cannot be checked", [f"{p}: {e.detail()}" for p, e in refused])
    for p in paths:
        docs = read[str(p)]
        for doc in docs:   # a Kubernetes object: the manifest reading's guarantee (#3426)
            if doc["kind"] != "Service":
                continue
            meta = doc.get("metadata") or {}
            services.append({
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "annotations": meta.get("annotations") or {},
                "labels": meta.get("labels") or {},
            })
    return services


def load_repo_services() -> list[dict]:
    return parse_services(sorted(K8S_MANIFESTS_DIR.rglob("*.yaml")))


def _service_admitted(svc: dict, job: dict) -> bool:
    """True iff *job*'s target-selection face would keep *svc*."""
    if job["sd_role"] not in _SERVICE_FACE_ROLES:
        return False
    ns = svc.get("namespace")
    if job["sd_namespaces"] and ns not in job["sd_namespaces"]:
        return False
    if job["annotation_keep"] and svc["annotations"].get(SCRAPE_ANNOTATION) != "true":
        return False
    if job["ns_keep_regex"] is not None:
        if not ns or re.fullmatch(job["ns_keep_regex"], ns) is None:
            return False
    platform = (svc.get("labels") or {}).get(PLATFORM_SCRAPE_LABEL) == PLATFORM_SCRAPE_VALUE
    if job["platform_label"] == "keep" and not platform:
        return False
    if job["platform_label"] == "drop" and platform:
        return False
    return True


def _keeps_platform_everywhere(job: dict) -> bool:
    """A job that keeps annotated, platform-labelled Services in EVERY namespace."""
    return (job["sd_role"] in _SERVICE_FACE_ROLES and job["annotation_keep"]
            and job["platform_label"] == "keep"
            and not job["sd_namespaces"] and job["ns_keep_regex"] is None)


#: Kubernetes SD roles whose targets can be a platform chart's pods but carry
#: no Service selection face the gate models (the `pod` role has no
#: __meta_kubernetes_service_* labels); a job of these roles is taken to keep
#: a platform Service unless its namespace face excludes it or it drops the
#: platform label (#1425 review F4a) — guessing "no" would hide exactly the
#: double scrape this check exists for. The endpointslice / endpoints roles are modelled
#: through the Service face (_SERVICE_FACE_ROLES) since #3483.
_ROLES_REACHING_PLATFORM_PODS = ("pod",)

#: A namespace no job is expected to name — the "installed anywhere" case.
#: A synthetic string, not a tenant id (dev-rule #2).
_UNNAMED_PROBE_NAMESPACE = "da-probe"


def _may_keep_platform_service(svc: dict, job: dict) -> bool:
    """Whether *job* could keep *svc* — exact for the roles carrying the
    Service face, conservative for the roles in _ROLES_REACHING_PLATFORM_PODS."""
    if job["sd_role"] in _SERVICE_FACE_ROLES:
        return _service_admitted(svc, job)
    if job["sd_role"] not in _ROLES_REACHING_PLATFORM_PODS:
        return False
    ns = svc["namespace"]
    if job["sd_namespaces"] and ns not in job["sd_namespaces"]:
        return False
    if job["ns_keep_regex"] is not None and re.fullmatch(job["ns_keep_regex"], ns) is None:
        return False
    return job["platform_label"] != "drop"


def _regex_example(regex: str) -> str | None:
    """A string *regex* fully matches, or None when it cannot be derived.

    Walks the parsed pattern taking the first branch / first class member and
    the minimum repeat (at least once); the caller re-checks with fullmatch,
    so a construct this does not model yields None, never a wrong string.
    """
    try:
        import re._parser as sre  # Python 3.11+
        import re._constants as c
    except ImportError:  # pragma: no cover — older Pythons
        import sre_parse as sre
        import sre_constants as c

    def gen(items) -> str:
        out = []
        for op, av in items:
            if op is c.LITERAL:
                out.append(chr(av))
            elif op is c.ANY:
                out.append("probe")
            elif op in (c.MAX_REPEAT, c.MIN_REPEAT):
                lo, _hi, sub = av
                out.append(gen(sub) * max(lo, 1))
            elif op is c.SUBPATTERN:
                out.append(gen(av[-1]))
            elif op is c.BRANCH:
                out.append(gen(av[1][0]))
            elif op is c.IN:
                kind, val = av[0]
                if kind is c.LITERAL:
                    out.append(chr(val))
                elif kind is c.RANGE:
                    out.append(chr(val[0]))
                elif kind is c.CATEGORY and val is c.CATEGORY_DIGIT:
                    out.append("0")
                elif kind is c.CATEGORY and val is c.CATEGORY_WORD:
                    out.append("a")
                else:
                    raise ValueError(f"unmodelled class member {kind}")
            elif op is c.AT:
                continue
            else:
                raise ValueError(f"unmodelled regex op {op}")
        return "".join(out)

    try:
        example = gen(sre.parse(regex))
    except (ValueError, re.error, TypeError, IndexError):
        return None
    return example if re.fullmatch(regex, example) else None


def _platform_probe_namespaces(jobs: list[dict]) -> tuple[list[str], list[str]]:
    """(namespaces, errors): every namespace a job names or keeps by regex,
    plus one none names (#1425 review F4b: derived from the jobs, so a tenant
    job's regex changing does not leave the probe testing a stale namespace)."""
    names = {_UNNAMED_PROBE_NAMESPACE}
    errors = []
    for j in jobs:
        names.update(j["sd_namespaces"])
        if j["ns_keep_regex"] is not None:
            example = _regex_example(j["ns_keep_regex"])
            if example is None:
                errors.append(
                    f"PLATFORM-PROBE-UNDERIVABLE: job '{j['job']}' keeps namespaces "
                    f"by {j['ns_keep_regex']!r}, which the gate cannot turn into a "
                    "probe namespace — so it cannot tell whether that job also "
                    "keeps a platform Service. Simplify the regex or extend "
                    "_regex_example. (#1425)")
            else:
                names.add(example)
    return sorted(names), errors


def _platform_double_scrape(jobs: list[dict]) -> list[str]:
    """Errors for a platform Service that more than one job would keep.

    Two jobs keeping one target scrape it twice; worse, the tenant job rewrites
    `job` and `tenant` (#1425: a platform chart in a db-* namespace), so every
    `up{job="threshold-exporter"}`-style platform alert loses its target.
    """
    namespaces, errors = _platform_probe_namespaces(jobs)
    for ns in namespaces:
        probe = {"name": "platform-probe", "namespace": ns,
                 "annotations": {SCRAPE_ANNOTATION: "true"},
                 "labels": {PLATFORM_SCRAPE_LABEL: PLATFORM_SCRAPE_VALUE}}
        keeping = sorted(j["job"] for j in jobs if _may_keep_platform_service(probe, j))
        if len(keeping) > 1:
            errors.append(
                f"PLATFORM-DOUBLE-SCRAPE: a Service labelled "
                f"{PLATFORM_SCRAPE_LABEL}={PLATFORM_SCRAPE_VALUE} in ns '{ns}' is "
                f"kept by {len(keeping)} jobs ({', '.join(keeping)}) — drop that "
                f"label in every job but the platform one. (#1425)")
    return errors


def _platform_job_label(jobs: list[dict]) -> list[str]:
    """Errors for a job keeping platform Services without `job` ← Service name.

    The platform alerts select `up{job="threshold-exporter"}`,
    `up{job="tenant-api"}` and so on; without the relabel every target is
    `job="<scrape job name>"` and those selectors match nothing — no error,
    the alerts just never fire. (#1425 review F5)
    """
    return [
        f"PLATFORM-JOB-LABEL: job '{j['job']}' keeps platform Services but does "
        f"not end its `job` relabelling with source_labels [{SERVICE_NAME_META}] "
        "→ target_label job — every up{job=\"<service>\"} platform alert would "
        "select nothing. (#1425)"
        for j in jobs
        if j["platform_label"] == "keep" and not j["job_from_service_name"]]


def _platform_per_pod(jobs: list[dict]) -> list[str]:
    """Errors for a job keeping platform Services without scraping every pod
    exactly once (#3483).

    threshold-exporter runs several replicas. `role: service` scrapes the
    Service address, so each scrape reaches one replica: a dead replica never
    reads up 0 and the per-replica series do not exist. The endpointslice roles
    reach every pod, but one target per pod address × Service port (plus the
    container ports the Service does not expose) — without a keep on the
    annotated port, a pod with two Service ports (tenant-api) is scraped twice.
    """
    errors = []
    for j in jobs:
        if j["platform_label"] != "keep":
            continue
        if len(j["sd_roles"]) != 1:
            errors.append(
                f"PLATFORM-PER-POD: job '{j['job']}' keeps platform Services with "
                f"{len(j['sd_roles'])} kubernetes_sd_configs ({j['sd_roles']}) — the "
                "gate models exactly one, and a second discovery scrapes the same "
                "pods again (or one replica per scrape). Use one "
                "`role: endpointslice`. (#3483)")
        elif j["sd_role"] not in _PER_POD_ROLES:
            errors.append(
                f"PLATFORM-PER-POD: job '{j['job']}' keeps platform Services with "
                f"`role: {j['sd_role']}`, which scrapes one replica per scrape — a "
                "dead replica is invisible. Use `role: endpointslice`. (#3483)")
        elif not j["port_select"]:
            errors.append(
                f"PLATFORM-PER-POD: job '{j['job']}' (role: {j['sd_role']}) has no "
                f"`keepequal` of [{SCRAPE_PORT_META}] against a label holding "
                "__address__'s port (with __address__ not rewritten afterwards) — "
                "every pod is scraped once per Service port. (#3483)")
    return errors


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
    """(consumed metric → {consumer rule ids}, recording outputs of every tree).

    ⛔ Discovery is DELEGATED to `_rule_tree`, the repo's one content-based
    scanner, rather than re-globbed here. This function used to be the FOURTH
    reader of "the rule trees" — two non-recursive globs, `.yaml` only, ConfigMap
    only, no PrometheusRule / binaryData / `kind: List` / multi-document
    handling — sitting in the same directory as the scanner, importable at zero
    cost, and outside the cross-check that exists precisely because readers
    drift. Measured: it opened 33 of the 49 files that shipped rules then,
    missing all 16 PrometheusRules of the committed operator copy (no longer
    committed since #3427).

    Missing a tree here is fail-open for THIS gate too: an unseen rule is an
    unseen consumer, and a metric whose only consumer went unseen stops being
    reported as unreachable. The 16 it missed happen to be exact generated
    copies, so switching is a no-op on today's tree (verified: identical
    consumed keys, recording outputs and leaf metrics) — the point is the day
    they are not.
    """
    consumed: dict[str, set] = defaultdict(set)
    outputs: set[str] = set()
    for where, doc, _prov in _rule_containers():
        # ⛔ isinstance at BOTH levels. `_is_rule_groups` accepts a `groups`
        # list when only SOME element is a mapping with `rules`, so a mixed list
        # reaches every consumer — and this one raised `AttributeError` on the
        # first scalar, taking the whole gate down. Measured.
        rules = [r for g in (doc.get("groups") or []) if isinstance(g, dict)
                 for r in (g.get("rules") or []) if isinstance(r, dict)]
        outputs.update(extract_recording_outputs(rules))
        for r in rules:
            rid = f"{where}::{r.get('alert') or r.get('record')}"
            for m in extract_metrics(r.get("expr", "") or ""):
                consumed[m].add(rid)
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


#: → (matched key, kind, spec) or None — exact beats prefix, then longest
#: prefix wins (the table's module, re-exported).
_resolve_family = _lib_metric_sources.resolve_family


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
            chart = spec
            admitting = [j for j in jobs if _keeps_platform_everywhere(j)
                         and name_passes(metric, j["name_rules"])]
            if admitting:
                # Say what was actually checked. The federation-plane caveat is
                # a vector_-specific scope note (see SCOPE BOUNDARY above) and
                # is meaningless for other helm-service families, so it is not
                # asserted here; "chart face not read here" states the real gap.
                return REACHABLE, (
                    f"job '{admitting[0]['job']}' keeps "
                    f"{PLATFORM_SCRAPE_LABEL}={PLATFORM_SCRAPE_VALUE} Services in "
                    f"every namespace ({chart}'s chart face not read here — "
                    "pinned by tests/lint/test_check_scrape_reachability.py, "
                    "#1286)")
            return DEAD, (f"no annotation-keep job keeps "
                          f"{PLATFORM_SCRAPE_LABEL}={PLATFORM_SCRAPE_VALUE} Services in "
                          f"every namespace (the target is {chart}'s Service, installed "
                          "into Release.Namespace), or a __name__ filter rejects "
                          "this metric")
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

    errors: list[str] = (_platform_double_scrape(jobs) + _platform_job_label(jobs)
                         + _platform_per_pod(jobs))
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
        help=i18n_text("DEAD / 新 UNKNOWN-SOURCE / ledger exit-lock 破口 / 平台 Service 被多個 job 收錄即 exit 1",
                       "exit 1 on DEAD, NEW unknown-source, a ledger exit-lock breach, "
                       "or a platform Service kept by more than one job"))
    parser.add_argument(
        "--verbose", action="store_true",
        help=i18n_text("列出每個葉 metric 的分類與理由",
                       "print every leaf metric's classification and reason"))
    args = parser.parse_args(argv)

    try:
        result = run_check()
    except _lib_consumer_read.ConsumerReadError as exc:
        # No da-read / da-crdecode, or Prometheus (or kubectl) refuses the
        # config or a manifest: no verdict, and no falling back to reading
        # it ourselves.
        print(f"ERROR: cannot read the Prometheus config or the k8s/ manifests "
              f"the way their consumers do: {exc}", file=sys.stderr)
        for line in exc.stderr_lines:
            print(f"  {line}", file=sys.stderr)
        return EXIT_CALLER_ERROR
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
