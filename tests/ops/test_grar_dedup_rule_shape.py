"""#3899: which base inhibit rules are the route generator's own dedup rule.

`_grar_render.assert_base_has_no_dedup_rule` refuses a base holding one.
Owner decision (PR #3910, option B): only the shapes the generator itself
writes count — today's `build_dedup_inhibit_rules(excluded)` (two: alerts
that carry `da_cluster`, alerts that do not; #4097) and the
per-tenant rule it wrote before #3899 (`pre3899_dedup_inhibit_rule`, v2.9.0's
`_build_inhibit_rules`). The predicate (`_grar_validate.is_generated_dedup_rule`)
rebuilds the rule from the generator and compares the two as sets, so a copy
is recognised however it is spelled (spacing, order, `{}`, several matchers
in one element, unquoted values, legacy `*_match_re`, repeats, Alertmanager's
own reading). Any other rule is operator policy — kube-prometheus' rules,
Silent Mode, a broader critical→warning policy — and merges as written.
Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "tools"))
sys.path.insert(0, str(_REPO / "scripts" / "tools" / "ops"))

import _grar_render as render  # noqa: E402
from _grar_routes import (  # noqa: E402
    build_dedup_inhibit_rules,
    pre3899_dedup_inhibit_rule,
    pre4097_dedup_inhibit_rule,
)
from _grar_validate import (  # noqa: E402
    _normalised_rule,
    _split_matcher_element,
    is_generated_dedup_rule,
)

_GAR = _REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_PRE3899_OUTPUT = Path(__file__).parent / "fixtures" / "pre3899_output_without_marker.yml"

#: The per-cluster rule and the one for alerts without `da_cluster` (#4097).
GENERATED, GENERATED_UNLABELLED = build_dedup_inhibit_rules(["tb", "tc"])


# ── the pre-#3899 shape is v2.9.0's ──────────────────────────────────────

def test_the_pre3899_shape_is_v290s_output():
    """Copied from `git show tools/v2.9.0:scripts/tools/ops/_grar_routes.py`
    (`_build_inhibit_rules("ta")`); the recognition helper must not drift."""
    assert pre3899_dedup_inhibit_rule("ta") == {
        "source_matchers": ['severity="critical"', 'metric_group=~".+"', 'tenant="ta"'],
        "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant="ta"'],
        "equal": ["metric_group"],
    }


def test_the_pre4097_shape_is_the_rule_main_wrote_before_clusters():
    """Copied from `git show 4f4105be7:scripts/tools/ops/_grar_routes.py`
    (`build_dedup_inhibit_rule(["tb", "tc"])`); the recognition helper must
    not drift."""
    assert pre4097_dedup_inhibit_rule(["tb", "tc"]) == {
        "source_matchers": ['severity="critical"', 'metric_group=~".+"', 'tenant=~".+"'],
        "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant=~".+"',
                            'tenant!~"tb|tc"'],
        "equal": ["tenant", "metric_group"],
    }


# ── refused: the generator's own shapes, however spelled ─────────────────

def _try_local_rules() -> list[dict]:
    am = yaml.safe_load((_REPO / "try-local" / "alertmanager.yml").read_text(encoding="utf-8"))
    return [r for r in am["inhibit_rules"]
            if 'severity = "critical"' in r.get("source_matchers", [])]


@pytest.mark.parametrize("rule", [
    *build_dedup_inhibit_rules([]),
    GENERATED,
    GENERATED_UNLABELLED,
    *build_dedup_inhibit_rules(["a.b", "t-c"]),
    pre3899_dedup_inhibit_rule("ta"),
    # the one rule written between #3899 and #4097 (no matcher on the cluster)
    pre4097_dedup_inhibit_rule(["tb", "tc"]),
    {"source_matchers": ['severity = "critical"', 'metric_group =~ ".+"', 'tenant =~ ".+"'],
     "target_matchers": ['severity = "warning"', 'metric_group =~ ".+"', 'tenant =~ ".+"'],
     "equal": ["tenant", "metric_group"]},
    # spacing, order, both sides
    {"source_matchers": ['tenant =~ ".+"', 'da_cluster =~ ".+"', 'severity = "critical"',
                         'metric_group =~ ".+"'],
     "target_matchers": ['tenant!~"tb|tc"', 'tenant=~".+"', 'metric_group=~".+"',
                         'severity="warning"', 'da_cluster=~".+"'],
     "equal": ["da_cluster", "metric_group", "tenant"]},
    # {} and several matchers in one element
    {"source_matchers": ['{severity="critical", metric_group=~".+", tenant=~".+", da_cluster=""}'],
     "target_matchers": ['severity="warning",metric_group=~".+"',
                         'tenant=~".+",tenant!~"tb|tc",da_cluster=""'],
     "equal": ["tenant", "metric_group"]},
    # unquoted values
    {"source_matchers": ["severity=critical", "metric_group=~.+", "tenant=~.+", "da_cluster=~.+"],
     "target_matchers": ["severity=warning", "metric_group=~.+", "tenant=~.+", "da_cluster=~.+"],
     "equal": ["tenant", "metric_group", "da_cluster"]},
    # legacy match maps
    {"source_match": {"severity": "critical", "da_cluster": ""},
     "source_match_re": {"metric_group": ".+", "tenant": ".+"},
     "target_match": {"severity": "warning", "da_cluster": ""},
     "target_match_re": {"metric_group": ".+", "tenant": ".+"},
     "equal": ["tenant", "metric_group"]},
    {"source_match": {"severity": "critical", "tenant": "ta"},
     "source_match_re": {"metric_group": ".+"},
     "target_match": {"severity": "warning", "tenant": "ta"},
     "target_match_re": {"metric_group": ".+"},
     "equal": ["metric_group"]},
    # repeated matchers and equal labels
    {"source_matchers": ['severity="critical"', 'severity="critical"', 'metric_group=~".+"',
                         'tenant="ta"'],
     "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant="ta"',
                         'tenant="ta"'],
     "equal": ["metric_group", "metric_group"]},
    # escaped ids again, the exclusion value bare (unquoted): read back
    # unescaped (`a.b`), it rebuilds to this very rule
    {**build_dedup_inhibit_rules([])[0],
     "target_matchers": ["severity=warning", "metric_group=~.+", "tenant=~.+",
                         "da_cluster=~.+", "tenant!~a\\.b|t-c"]},
], ids=["generated-no-exclusion", "generated-no-exclusion-unlabelled",
        "generated-excluding", "generated-excluding-unlabelled",
        "generated-escaped-ids", "generated-escaped-ids-unlabelled",
        "pre3899-per-tenant", "pre4097-one-rule", "pre4097-hand-copy",
        "reordered-and-spaced", "braces-several-in-one",
        "unquoted", "legacy-match-maps", "legacy-match-maps-pre3899", "repeats",
        "escaped-ids-bare-value"])
def test_the_generators_own_rule_is_recognised(rule):
    assert is_generated_dedup_rule(rule)


def test_try_locals_static_rules_are_the_generators_shape():
    """try-local's hand-written rules are the generated pair with no exclusion
    list (try-local does not run the generator) — copies, so recognised, and
    both of them: a copy holding only one would dedup only the alerts that
    carry `da_cluster`, or only those that do not."""
    rules = _try_local_rules()
    assert [is_generated_dedup_rule(r) for r in rules] == [True, True]
    assert ({_normalised_rule(r) for r in rules}
            == {_normalised_rule(r) for r in build_dedup_inhibit_rules([])})


def test_alertmanagers_reading_of_the_rule_is_recognised(da_read_env):
    """After da-read (Alertmanager's loader): one matcher per element, sorted."""
    text = yaml.safe_dump({"route": {"receiver": "r"}, "receivers": [{"name": "r"}],
                           "inhibit_rules": [GENERATED, pre3899_dedup_inhibit_rule("ta")]})
    rules = render.alertmanager_reading(text)["inhibit_rules"]
    assert rules[0]["source_matchers"] != GENERATED["source_matchers"]  # it was reordered
    assert [is_generated_dedup_rule(r) for r in rules] == [True, True]


def test_the_pre3899_output_holds_only_recognised_dedup_rules():
    """The generator's output before #3899 (fixture, see its header)."""
    rules = yaml.safe_load(_PRE3899_OUTPUT.read_text(encoding="utf-8"))["inhibit_rules"]
    assert len(rules) == 2
    assert [is_generated_dedup_rule(r) for r in rules] == [True, True]


def test_todays_output_without_its_marker_holds_a_recognised_rule(tmp_path, da_guard_env,
                                                                 da_read_env):
    """The generator's output now, rendered here, header lines dropped."""
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "ta.yaml").write_text('tenants:\n  ta:\n    mysql_connections: "70"\n', encoding="utf-8")
    (d / "tb.yaml").write_text('tenants:\n  tb:\n    mysql_connections: "70"\n'
                               '    _severity_dedup: "disable"\n', encoding="utf-8")
    out = tmp_path / "am.yml"
    proc = subprocess.run([sys.executable, "-s", str(_GAR), "--config-dir", str(d),
                           "--output-alertmanager", "-o", str(out)],
                          capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    text = out.read_text(encoding="utf-8")
    assert text.startswith(render.GENERATED_OUTPUT_HEADER)
    rules = yaml.safe_load(text[len(render.GENERATED_OUTPUT_HEADER):])["inhibit_rules"]
    assert [is_generated_dedup_rule(r) for r in rules] == [True, True]
    assert all('tenant!~"tb"' in r["target_matchers"] for r in rules)
    assert rules == build_dedup_inhibit_rules(["tb"])


# ── kept: everything else is operator policy ─────────────────────────────

def _shipped_base_rules() -> list[dict]:
    cm = yaml.safe_load((_REPO / "k8s" / "03-monitoring" / "configmap-alertmanager.yaml")
                        .read_text(encoding="utf-8"))
    return yaml.safe_load(cm["data"]["alertmanager.yml"])["inhibit_rules"]


def test_the_shipped_base_rules_are_not_the_generators():
    """Silent Mode (warning / critical) and CustomRecipeSilent."""
    rules = _shipped_base_rules()
    names = {m for r in rules for m in r["source_matchers"] if m.startswith("alertname=")}
    assert {'alertname="TenantSilentWarning"', 'alertname="TenantSilentCritical"',
            'alertname="CustomRecipeSilent"'} <= names, names
    assert [i for i, r in enumerate(rules) if is_generated_dedup_rule(r)] == []


def _variant(**changes) -> dict:
    return {**GENERATED, **changes}


@pytest.mark.parametrize("rule", [
    # kube-prometheus-stack's defaults
    {"source_matchers": ['severity="critical"'], "target_matchers": ['severity=~"warning|info"'],
     "equal": ["namespace", "alertname"]},
    {"source_matchers": ['severity="warning"'], "target_matchers": ['severity="info"'],
     "equal": ["namespace", "alertname"]},
    {"source_matchers": ['alertname="InfoInhibitor"'], "target_matchers": ['severity="info"'],
     "equal": ["namespace"]},
    # broader operator policies
    {"source_matchers": ['severity="critical"', 'tenant=~".+"'],
     "target_matchers": ['severity="warning"', 'tenant=~".+"'], "equal": ["tenant"]},
    {"source_matchers": ['severity="critical"'], "target_matchers": ['severity="warning"']},
    {"source_matchers": ['severity="critical"', 'scope="cluster"'],
     "target_matchers": ['severity=~"warning|critical"'], "equal": ["cluster"]},
    {"source_matchers": ['severity="critical"', 'team="dba"'],
     "target_matchers": ['severity="warning"', 'team="dba"'], "equal": ["tenant", "metric_group"]},
    # near the generated rule, but not what it writes
    _variant(source_matchers=['severity="critical"', 'metric_group=~".+"', 'tenant!=""',
                              'da_cluster=~".+"'],
             target_matchers=['severity="warning"', 'metric_group=~".+"', 'tenant!=""',
                              'da_cluster=~".+"']),
    _variant(equal=["tenant", "metric_group", "da_cluster", "cluster"]),
    # #4097, the cluster label: the per-cluster matchers with the cluster
    # missing from equal; the unlabelled matchers with it there; one side only
    _variant(equal=["tenant", "metric_group"]),
    {**GENERATED_UNLABELLED, "equal": ["tenant", "metric_group", "da_cluster"]},
    _variant(source_matchers=GENERATED_UNLABELLED["source_matchers"]),
    _variant(target_matchers=GENERATED["target_matchers"] + ['tenant!~"td"']),
    _variant(target_matchers=GENERATED["target_matchers"][:-1] + ['tenant!~"t.*"']),
    {**pre3899_dedup_inhibit_rule("ta"),
     "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant="tb"']},
    # an empty exclusion member is no tenant id: the generator never writes it
    _variant(target_matchers=GENERATED["target_matchers"][:-1] + ['tenant!~""']),
    _variant(target_matchers=GENERATED["target_matchers"][:-1] + ['tenant!~"tb|"']),
    # equal, a different angle: the pre-#3899 shape with today's equal
    {**pre3899_dedup_inhibit_rule("ta"), "equal": ["tenant", "metric_group"]},
    _variant(equal=["metric_group", "da_cluster"]),
    # the exclusion list as written, a different angle: not sorted / repeated /
    # with the escaped hyphen the generator does not write
    _variant(target_matchers=GENERATED["target_matchers"][:-1] + ['tenant!~"tc|tb"']),
    _variant(target_matchers=GENERATED["target_matchers"][:-1] + ['tenant!~"tb|tb|tc"']),
    _variant(target_matchers=build_dedup_inhibit_rules(["t-b"])[0]["target_matchers"][:-1]
             + ['tenant!~"t\\\\-b"']),
], ids=["kube-prom-crit-over-warn-info", "kube-prom-warn-over-info", "kube-prom-info-inhibitor",
        "tenant-wide-policy", "global-no-equal", "scope-cluster", "team-dba",
        "tenant-not-empty-gate", "extra-equal-label",
        "cluster-gated-not-in-equal", "unlabelled-with-cluster-in-equal",
        "cluster-matchers-disagree", "two-exclusion-matchers",
        "non-literal-exclusion", "pre3899-mismatched-tenants",
        "empty-exclusion", "empty-exclusion-member",
        "pre3899-with-todays-equal", "generated-missing-tenant-in-equal",
        "exclusion-unsorted", "exclusion-repeated", "exclusion-escaped-hyphen"])
def test_other_rules_are_operator_policy(rule):
    assert not is_generated_dedup_rule(rule)


def test_a_rule_that_is_not_a_mapping_or_unreadable_is_not_the_generators():
    assert not is_generated_dedup_rule(["severity=critical"])
    assert not is_generated_dedup_rule({"source_matchers": ['severity="critical'],
                                        "target_matchers": [], "equal": []})


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
