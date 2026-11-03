"""Absence alerts on optional components are guarded by the component's presence (#1595 D1b).

WHY: `absent(x)` is true wherever x has no series — including every cluster
that never installed the chart emitting x. FederationRevocationReconcileStale
and FederationAuditPipelineSilent (both critical) fired forever wherever
federation was not deployed, in raw and Prometheus Operator mode alike.

WHAT IS CHECKED: every rule in every rule pack (the source of all rule copies)
— each `absent()` / `absent_over_time()` over a metric whose family resolves to
an optional chart (`_lib_metric_sources.OPTIONAL_CHARTS`) must sit in an
expression that also reads `up{job="<that chart's Service>"}`, the presence
guard. The predicate is `_lib_metric_sources.unguarded_absences`; its own
behaviour is pinned below, and the literal anchor keeps the scan non-vacuous.
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


def _rules():
    packs = cr.rule_pack_files(REPO / cr.RULE_PACKS.directory)
    for pack, rf in zip(packs, cr.prometheus_rule_files(packs)):
        for group in rf.groups:
            for rule in group.get("rules") or []:
                yield pack.name, rule


def test_every_optional_chart_absence_is_guarded():
    unguarded, guarded = [], set()
    for pack, rule in _rules():
        expr = str(rule.get("expr", ""))
        name = rule.get("alert") or rule.get("record")
        problems = ms.unguarded_absences(expr)
        if problems:
            unguarded.append((pack, name, problems))
        elif any(ms.resolve_family(m) and ms.resolve_family(m)[2] in ms.OPTIONAL_CHARTS
                 for m in ms.ABSENT_RE.findall(expr)):
            guarded.add(name)
    assert not unguarded, (
        "absence checks over an optional chart's metric fire forever where the chart "
        f"is not installed; guard them with that chart's `up{{job=...}}`: {unguarded}")
    assert _GUARDED_ANCHOR <= guarded, (
        f"the scan no longer sees the known guarded absences {sorted(_GUARDED_ANCHOR - guarded)} "
        "— it measured nothing")


@pytest.mark.parametrize("expr,ok", [
    ('absent(federation_revocation_channel_up)', False),
    ('absent(federation_revocation_channel_up) and on() count(up{job="federation-reconciler"}) > 0', True),
    # the WRONG chart's presence is no guard
    ('absent(federation_revocation_channel_up) and on() count(up{job="federation-gateway"}) > 0', False),
    ('absent_over_time(tenant_federation_requests_total[10m])', False),
    ('absent_over_time(tenant_federation_requests_total[10m]) and on() count(up{job="federation-gateway"}) > 0', True),
    # threshold-exporter is the platform itself: not optional
    ('absent(up{job="threshold-exporter"} == 1)', True),
    ('absent(da_config_defaults_unusable)', True),
    # an unmodelled metric is not judged here (the scrape gate owns unknown sources)
    ('absent(mysql_up)', True),
])
def test_unguarded_absences(expr, ok):
    assert (ms.unguarded_absences(expr) == []) is ok, ms.unguarded_absences(expr)
