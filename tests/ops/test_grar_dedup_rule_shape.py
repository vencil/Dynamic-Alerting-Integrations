"""#3899: which base inhibit rules are the route generator's own dedup rule.

`_grar_render.assert_base_has_no_dedup_rule` refuses a base holding one.
Owner decision (PR #3910, option B): only the shapes the generator itself
writes count — today's `build_dedup_inhibit_rule(excluded)` and the
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
from _grar_routes import build_dedup_inhibit_rule, pre3899_dedup_inhibit_rule  # noqa: E402
from _grar_validate import _split_matcher_element, is_generated_dedup_rule  # noqa: E402

_GAR = _REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_PRE3899_OUTPUT = Path(__file__).parent / "fixtures" / "pre3899_output_without_marker.yml"

GENERATED = build_dedup_inhibit_rule(["tb", "tc"])


# ── the pre-#3899 shape is v2.9.0's ──────────────────────────────────────

def test_the_pre3899_shape_is_v290s_output():
    """Copied from `git show tools/v2.9.0:scripts/tools/ops/_grar_routes.py`
    (`_build_inhibit_rules("ta")`); the recognition helper must not drift."""
    assert pre3899_dedup_inhibit_rule("ta") == {
        "source_matchers": ['severity="critical"', 'metric_group=~".+"', 'tenant="ta"'],
        "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant="ta"'],
        "equal": ["metric_group"],
    }


# ── refused: the generator's own shapes, however spelled ─────────────────

def _try_local_rule() -> dict:
    am = yaml.safe_load((_REPO / "try-local" / "alertmanager.yml").read_text(encoding="utf-8"))
    [rule] = [r for r in am["inhibit_rules"]
              if 'severity = "critical"' in r.get("source_matchers", [])]
    return rule


@pytest.mark.parametrize("rule", [
    build_dedup_inhibit_rule([]),
    GENERATED,
    build_dedup_inhibit_rule(["a.b", "t-c"]),
    pre3899_dedup_inhibit_rule("ta"),
    # spacing, order, both sides
    {"source_matchers": ['tenant =~ ".+"', 'severity = "critical"', 'metric_group =~ ".+"'],
     "target_matchers": ['tenant!~"tb|tc"', 'tenant=~".+"', 'metric_group=~".+"',
                         'severity="warning"'],
     "equal": ["metric_group", "tenant"]},
    # {} and several matchers in one element
    {"source_matchers": ['{severity="critical", metric_group=~".+", tenant=~".+"}'],
     "target_matchers": ['severity="warning",metric_group=~".+"', 'tenant=~".+",tenant!~"tb|tc"'],
     "equal": ["tenant", "metric_group"]},
    # unquoted values
    {"source_matchers": ["severity=critical", "metric_group=~.+", "tenant=~.+"],
     "target_matchers": ["severity=warning", "metric_group=~.+", "tenant=~.+"],
     "equal": ["tenant", "metric_group"]},
    # legacy match maps
    {"source_match": {"severity": "critical"},
     "source_match_re": {"metric_group": ".+", "tenant": ".+"},
     "target_match": {"severity": "warning"},
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
    {**build_dedup_inhibit_rule([]),
     "target_matchers": ["severity=warning", "metric_group=~.+", "tenant=~.+",
                         "tenant!~a\\.b|t-c"]},
], ids=["generated-no-exclusion", "generated-excluding", "generated-escaped-ids",
        "pre3899-per-tenant", "reordered-and-spaced", "braces-several-in-one",
        "unquoted", "legacy-match-maps", "legacy-match-maps-pre3899", "repeats",
        "escaped-ids-bare-value"])
def test_the_generators_own_rule_is_recognised(rule):
    assert is_generated_dedup_rule(rule)


def test_try_locals_static_rule_is_the_generators_shape():
    """try-local's hand-written rule is the generated rule with no exclusion
    list (try-local does not run the generator) — a copy, so recognised."""
    assert is_generated_dedup_rule(_try_local_rule())


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
    assert [is_generated_dedup_rule(r) for r in rules] == [True]
    assert 'tenant!~"tb"' in rules[0]["target_matchers"]


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
    _variant(source_matchers=['severity="critical"', 'metric_group=~".+"', 'tenant!=""'],
             target_matchers=['severity="warning"', 'metric_group=~".+"', 'tenant!=""']),
    _variant(equal=["tenant", "metric_group", "cluster"]),
    _variant(target_matchers=GENERATED["target_matchers"] + ['tenant!~"td"']),
    _variant(target_matchers=GENERATED["target_matchers"][:-1] + ['tenant!~"t.*"']),
    {**pre3899_dedup_inhibit_rule("ta"),
     "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant="tb"']},
    # an empty exclusion member is no tenant id: the generator never writes it
    _variant(target_matchers=GENERATED["target_matchers"][:-1] + ['tenant!~""']),
    _variant(target_matchers=GENERATED["target_matchers"][:-1] + ['tenant!~"tb|"']),
    # equal, a different angle: the pre-#3899 shape with today's equal
    {**pre3899_dedup_inhibit_rule("ta"), "equal": ["tenant", "metric_group"]},
    _variant(equal=["metric_group"]),
    # the exclusion list as written, a different angle: not sorted / repeated /
    # with the escaped hyphen the generator does not write
    _variant(target_matchers=GENERATED["target_matchers"][:-1] + ['tenant!~"tc|tb"']),
    _variant(target_matchers=GENERATED["target_matchers"][:-1] + ['tenant!~"tb|tb|tc"']),
    _variant(target_matchers=build_dedup_inhibit_rule(["t-b"])["target_matchers"][:-1]
             + ['tenant!~"t\\\\-b"']),
], ids=["kube-prom-crit-over-warn-info", "kube-prom-warn-over-info", "kube-prom-info-inhibitor",
        "tenant-wide-policy", "global-no-equal", "scope-cluster", "team-dba",
        "tenant-not-empty-gate", "extra-equal-label", "two-exclusion-matchers",
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
