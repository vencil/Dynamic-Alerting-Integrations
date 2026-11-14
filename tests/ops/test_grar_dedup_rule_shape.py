"""#3899: which inhibit rules would override `_severity_dedup`.

`_grar_render.assert_base_has_no_dedup_rule` refuses a base holding one.
The predicate (`_grar_validate.overrides_severity_dedup`) is semantic: does
the rule let a tenant's critical alert suppress the same tenant's warning
alert of the same `metric_group`? So it must say yes however the rule is
spelled (`=` or `=~`, several matchers in one element, braces, legacy
`*_match` / `*_match_re`) and for any rule that does that suppression even
when it is not the generator's shape; and no for the base's Silent Mode /
CustomRecipeSilent rules and other rules that do not reach that pair.
Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "tools" / "ops"))

from _grar_routes import build_dedup_inhibit_rule  # noqa: E402
from _grar_validate import _split_matcher_element, overrides_severity_dedup  # noqa: E402

PER_TENANT = {
    "source_matchers": ['severity="critical"', 'metric_group=~".+"', 'tenant="ta"'],
    "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant="ta"'],
    "equal": ["metric_group"],
}
LEGACY_MATCH = {
    "source_match": {"severity": "critical"}, "source_match_re": {"metric_group": ".+"},
    "target_match": {"severity": "warning"}, "target_match_re": {"metric_group": ".+"},
    "equal": ["metric_group"],
}
REGEX_SEVERITY = {
    "source_matchers": ['severity=~"critical"', 'metric_group=~".+"'],
    "target_matchers": ['severity=~"warning|info"', 'metric_group=~".+"'],
    "equal": ["tenant", "metric_group"],
}
MULTI_IN_ONE = {
    "source_matchers": ['severity="critical",metric_group=~".+"'],
    "target_matchers": ['{severity="warning", metric_group=~".+"}'],
    "equal": ["metric_group"],
}
#: A pinned tenant on a regex: probed with that tenant, not only a synthetic one.
REGEX_PINNED_TENANT = {
    "source_matchers": ['severity="critical"', 'tenant=~"ta|tb"'],
    "target_matchers": ['severity="warning"', 'tenant=~"ta|tb"'],
    "equal": ["metric_group"],
}


@pytest.mark.parametrize("rule", [
    build_dedup_inhibit_rule([]), build_dedup_inhibit_rule(["tb"]),
    PER_TENANT, LEGACY_MATCH, REGEX_SEVERITY, MULTI_IN_ONE, REGEX_PINNED_TENANT,
], ids=["generated", "generated-excluding", "per-tenant", "legacy-match",
        "regex-severity", "several-matchers-in-one-element", "regex-pinned-tenant"])
def test_a_rule_that_dedups_is_refused_however_it_is_spelled(rule):
    assert overrides_severity_dedup(rule)


def _without(side: str, matcher: str) -> dict:
    rule = build_dedup_inhibit_rule(["tb"])
    key = f"{side}_matchers"
    assert matcher in rule[key], (matcher, rule[key])
    rule[key] = [m for m in rule[key] if m != matcher]
    return rule


@pytest.mark.parametrize("side,matcher", [
    ("source", 'severity="critical"'),
    ("source", 'metric_group=~".+"'),
    ("target", 'severity="warning"'),
    ("target", 'metric_group=~".+"'),
], ids=["source-severity", "source-gate", "target-severity", "target-gate"])
def test_loosening_one_side_still_dedups(side, matcher):
    """Under the semantic reading the old "missing one condition" controls
    flip: a rule that drops the severity or the metric_group gate on one
    side matches MORE alerts, so the critical→warning pair of one tenant and
    one metric_group is still suppressed (`equal: [tenant, metric_group]`
    holds) — it still overrides `_severity_dedup`."""
    assert overrides_severity_dedup(_without(side, matcher))


@pytest.mark.parametrize("change", [
    {"target_matchers": ['severity="info"', 'metric_group=~".+"', 'tenant=~".+"']},
    {"source_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant=~".+"']},
    {"target_matchers": ['severity="warning"', 'metric_group="mysql_connections"',
                         'tenant=~".+"', 'metric_group!~"mysql_connections"']},
    {"equal": ["tenant", "metric_group", "alertname"]},
    {"target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant!~".*"']},
], ids=["target-is-info", "source-is-warning", "target-matches-nothing",
        "equal-names-differ", "every-tenant-excluded"])
def test_a_rule_that_cannot_reach_the_pair_is_not_refused(change):
    rule = {**build_dedup_inhibit_rule([]), **change}
    assert not overrides_severity_dedup(rule)


def test_cross_tenant_pins_are_not_the_same_tenant_pair():
    rule = {"source_matchers": ['severity="critical"', 'tenant="ta"'],
            "target_matchers": ['severity="warning"', 'tenant="tb"'],
            "equal": ["metric_group"]}
    assert not overrides_severity_dedup(rule)


def _shipped_base_rules() -> list[dict]:
    cm = yaml.safe_load((_REPO / "k8s" / "03-monitoring" / "configmap-alertmanager.yaml")
                        .read_text(encoding="utf-8"))
    return yaml.safe_load(cm["data"]["alertmanager.yml"])["inhibit_rules"]


def test_the_shipped_base_rules_are_not_refused():
    """Silent Mode (warning / critical) and CustomRecipeSilent: their source
    is a named sentinel alert, not a tenant's critical."""
    rules = _shipped_base_rules()
    names = {m for r in rules for m in r["source_matchers"] if m.startswith("alertname=")}
    assert {'alertname="TenantSilentWarning"', 'alertname="TenantSilentCritical"',
            'alertname="CustomRecipeSilent"'} <= names, names
    assert [i for i, r in enumerate(rules) if overrides_severity_dedup(r)] == []


def test_a_rule_about_named_alerts_is_not_refused():
    """A rule naming its source alert (here an operator's own pair) is about
    that alert, not about every tenant critical."""
    rule = {"source_matchers": ['alertname="CustDown"', 'cluster=~".+"'],
            "target_matchers": ['alertname="CustDegraded"', 'cluster=~".+"'],
            "equal": ["cluster"]}
    assert not overrides_severity_dedup(rule)


def test_an_unreadable_element_fails_closed():
    rule = {"source_matchers": ['severity="critical'],
            "target_matchers": ['severity="warning"'], "equal": ["tenant"]}
    assert overrides_severity_dedup(rule)


@pytest.mark.parametrize("element,atoms", [
    ('severity="critical"', [("severity", "=", "critical")]),
    ('{severity="critical", metric_group=~".+"}',
     [("severity", "=", "critical"), ("metric_group", "=~", ".+")]),
    ('tenant!~"a\\\\.b|t\\"q"', [("tenant", "!~", 'a\\.b|t"q')]),
    ("severity=critical,tenant=~.+", [("severity", "=", "critical"), ("tenant", "=~", ".+")]),
    ('severity="critical" x', None),
    ("", None),
])
def test_split_matcher_element(element, atoms):
    assert _split_matcher_element(element) == atoms


def test_a_rule_that_is_not_a_mapping_is_not_refused():
    assert not overrides_severity_dedup(["severity=critical"])
