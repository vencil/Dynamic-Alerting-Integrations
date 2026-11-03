"""Absence alerts on optional components are guarded by the component's presence (#1595 D1b).

WHY: `absent(x)` is true wherever x has no series — including every cluster
that never installed the chart emitting x. FederationRevocationReconcileStale
and FederationAuditPipelineSilent (both critical) fired forever wherever
federation was not deployed, in raw and Prometheus Operator mode alike.

WHAT IS CHECKED: every rule in every rule pack (the source of all rule copies)
— each `absent()` / `absent_over_time()` over a metric whose family resolves to
an optional chart must be AND-ed with that chart's presence check, declared
once in `_lib_metric_sources.CHART_PRESENCE` (kube-state-metrics, never `up`:
`up` disappears exactly when the scrape target is lost, the failure the
absence alert exists to catch). "AND-ed" is judged on the PromQL AST
(promql_parser): a presence check joined by `or`, or merely present elsewhere
in the expression, filters nothing and does not count. An expression the
parser cannot read fails the check (fail closed). The literal anchor keeps the
scan non-vacuous; the declaration itself is pinned against the rendered charts
by tests/helm/test_platform_servicemonitors.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import _lib_consumer_read as cr
import _lib_metric_sources as ms

REPO = Path(__file__).resolve().parents[2]

#: LITERAL: the rules known to carry a guarded optional-chart absence. If the
#: scan stops finding them it measured nothing.
_GUARDED_ANCHOR = {"FederationRevocationReconcileStale", "FederationAuditPipelineSilent"}

_RECONCILER = 'count(kube_deployment_spec_replicas{deployment="federation-reconciler"} > 0) > 0'
_GATEWAY = 'count(kube_pod_container_info{container="audit-metrics"}) > 0'


def _rules():
    packs = cr.rule_pack_files(REPO / cr.RULE_PACKS.directory)
    for pack, rf in zip(packs, cr.prometheus_rule_files(packs)):
        for group in rf.groups:
            for rule in group.get("rules") or []:
                yield pack.name, rule


def _reads_optional_absence(expr: str) -> bool:
    for metric in ms.ABSENT_RE.findall(expr):
        fam = ms.resolve_family(metric)
        if fam and fam[2] in ms.OPTIONAL_CHARTS:
            return True
    return False


def test_every_optional_chart_absence_is_guarded():
    unguarded, guarded, scanned = [], set(), 0
    for pack, rule in _rules():
        scanned += 1
        expr = str(rule.get("expr", ""))
        name = rule.get("alert") or rule.get("record")
        problems = ms.unguarded_absences(expr)
        if problems:
            unguarded.append((pack, name, problems))
        elif _reads_optional_absence(expr):
            guarded.add(name)
    assert scanned > 100, f"only {scanned} rules scanned"
    assert not unguarded, (
        "absence checks over an optional chart's metric fire forever where the chart "
        f"is not installed; AND them with its CHART_PRESENCE check: {unguarded}")
    assert _GUARDED_ANCHOR <= guarded, (
        f"the scan no longer sees the known guarded absences {sorted(_GUARDED_ANCHOR - guarded)} "
        "— it measured nothing")


def test_every_optional_chart_has_a_presence_check():
    assert ms.OPTIONAL_CHARTS == set(ms.CHART_SERVICEMONITOR_JOBS) - {ms.THRESHOLD_EXPORTER_CHART}


@pytest.mark.parametrize("expr,ok", [
    ('absent(federation_revocation_channel_up)', False),
    (f'absent(federation_revocation_channel_up) and on() {_RECONCILER}', True),
    # inside a larger expression, still joined by `and`
    ('(time() - federation_revocation_last_reconcile_timestamp_seconds > 1800) or '
     f'(absent(federation_revocation_last_reconcile_timestamp_seconds) and on() {_RECONCILER})', True),
    # `or` joins nothing: the presence check filters no absence
    (f'absent(federation_revocation_channel_up) or {_RECONCILER}', False),
    # present elsewhere, but not AND-ed to the absence
    (f'(absent(federation_revocation_channel_up) or vector(0)) + on() {_RECONCILER}', False),
    # the WRONG chart's presence is no guard
    (f'absent(federation_revocation_channel_up) and on() {_GATEWAY}', False),
    # `up` is no presence check (it vanishes with the scrape target)
    ('absent(federation_revocation_channel_up) and on() count(up{job="federation-reconciler"}) > 0', False),
    # a negative matcher is no presence check
    ('absent(federation_revocation_channel_up) and on() '
     'count(kube_deployment_spec_replicas{deployment!="federation-reconciler"} > 0) > 0', False),
    ('absent_over_time(tenant_federation_requests_total[10m])', False),
    (f'absent_over_time(tenant_federation_requests_total[10m]) and on() {_GATEWAY}', True),
    # threshold-exporter is the platform itself: not optional
    ('absent(up{job="threshold-exporter"} == 1)', True),
    ('absent(da_config_defaults_unusable)', True),
    # an unmodelled metric is not judged here (the scrape gate owns unknown sources)
    ('absent(mysql_up)', True),
])
def test_unguarded_absences(expr, ok):
    assert (ms.unguarded_absences(expr) == []) is ok, ms.unguarded_absences(expr)


def test_an_unparseable_expression_fails_closed():
    with pytest.raises(ms.AbsenceGuardUnreadable):
        ms.unguarded_absences("absent(federation_revocation_channel_up) and on( (")
