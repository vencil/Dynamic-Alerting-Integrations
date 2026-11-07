"""Tests for check_leftouterjoin_enrichment.py — metadata through annotations only (#3483).

Pinned contracts
----------------
1. No metadata join: `tenant_metadata_info` in an expr only as the right-hand
   operand of `and` / `unless`; `or` (it copies the right-hand series and their
   labels) and any arithmetic / comparison match onto it (either side, any label
   list, aggregated or raw) is a violation.
2. Annotations / labels: no `$labels.X`, `.Labels.X` or `index $labels "X"` read
   of a metadata label outside the canonical templates, and every template
   reading `tenant_metadata_info` is exactly METADATA_ANNOTATIONS[label].
3. METADATA_LABELS is the exporter's own label list (collector.go), and the Go
   custom-alert compiler's metadataQuery is METADATA_QUERY.
4. Live dogfood: every committed rule pack is clean (run through main()).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_leftouterjoin_enrichment as lint  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_OLD_RHS = "topk by(tenant) (1, max by(tenant, runbook_url, owner, tier) (tenant_metadata_info))"


# --- 1. expressions ---------------------------------------------------------

_JOINS = {
    "enrichment_topk": f"x * on(tenant) group_left(runbook_url, owner, tier) {_OLD_RHS}",
    "enrichment_raw": "x * on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info",
    "partial_list": "x * on(tenant) group_left(owner) max by(tenant, owner) (tenant_metadata_info)",
    "bare_group_left": "x * on(tenant) group_left tenant_metadata_info",
    "group_right_left_side": "tenant_metadata_info * on(tenant) group_right(owner) x",
    "one_to_one": "x / ignoring(job) tenant_metadata_info",
    "comparison": "x > bool on(tenant) tenant_metadata_info",
    "no_on": "x * tenant_metadata_info",
    "aggregation_alone": "count(tenant_metadata_info) > 0",
    "upper_case": "x * ON(tenant) GROUP_LEFT(owner) tenant_metadata_info",
    "multi_line": "(\n  x\n)\n* on(tenant) group_left(runbook_url, owner, tier)\n  " + _OLD_RHS,
    # `or` passes right-hand series through with their labels (#3483 review):
    "or_raw": "x or tenant_metadata_info",
    "or_on": "x or on(tenant) tenant_metadata_info",
    "or_inside_joined_operand": "x * on(tenant) group_left(owner) (y or tenant_metadata_info)",
    "or_inside_joined_operand_no_spaces": "x*on(tenant)group_left(owner)(y or tenant_metadata_info)",
    "or_inside_joined_operand_nested": ("x * on(tenant) group_left(owner) (\n  (y)\n  or\n"
                                        "  (tenant_metadata_info)\n)"),
}


@pytest.mark.parametrize("name", sorted(_JOINS))
def test_every_metadata_join_is_flagged(name):
    assert lint.expr_violations(_JOINS[name]), name


@pytest.mark.parametrize("expr", [
    "x unless on(tenant) tenant_metadata_info",
    "x and on (tenant) tenant_metadata_info",
    "x and tenant_metadata_info",
    'x unless on(tenant) tenant_metadata_info{tier="gold"}',
    "x > on(tenant) group_left tenant:alert_threshold:x",
    'x * on(tenant) group_left max by(tenant) (user_state_filter{filter="a"})',
])
def test_set_operators_and_unrelated_matches_pass(expr):
    assert lint.expr_violations(expr) == []


def test_a_set_operand_beside_a_join_does_not_excuse_the_join():
    expr = ("(x * on(tenant) group_left(owner) tenant_metadata_info)\n"
            "or (x unless on(tenant) tenant_metadata_info)")
    assert lint.expr_violations(expr)


# --- 2. annotations / labels ------------------------------------------------

def test_canonical_annotations_pass():
    rule = {"alert": "A", "expr": "x > 1", "annotations": {
        "runbook_url": lint.METADATA_ANNOTATIONS["runbook_url"],
        "owner": lint.METADATA_ANNOTATIONS["owner"],
        "platform_summary": f"[{lint.METADATA_ANNOTATIONS['tier']}] {{{{ $labels.tenant }}}}: x",
    }}
    assert lint.rule_violations(rule) == []


@pytest.mark.parametrize("value", [
    "{{ $labels.runbook_url }}",
    "[{{ $labels.tier }}] {{ $labels.tenant }}",
    "{{$labels.owner}}",
    "{{ .Labels.owner }}",
    "{{ range query `up` }}{{ .Labels.tier }}{{ end }}",
    '{{ index $labels "owner" }}',
    '{{ index .Labels "runbook_url" }}',
])
def test_metadata_label_references_are_flagged(value):
    assert lint.template_violations("annotations.x", value)


@pytest.mark.parametrize("value", [
    # a different query: no topk, so several replica series render several values
    "{{ with query (printf `tenant_metadata_info{tenant=%q}` $labels.tenant) }}"
    "{{ with index . 0 }}{{ .Labels.owner }}{{ end }}{{ end }}",
    # the canonical one, respaced
    lint.METADATA_ANNOTATIONS["owner"].replace("{{ with index", "{{with index"),
    # range instead of with: concatenates every replica's value
    "{{ range query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}"
    "{{ .Labels.owner }}{{ end }}",
])
def test_non_canonical_metadata_templates_are_flagged(value):
    assert lint.template_violations("annotations.owner", value)


def test_reads_of_other_labels_pass():
    assert lint.template_violations("annotations.x", "{{ .Labels.instance }} {{ index $labels \"pod\" }}") == []


def test_the_runtime_canary_bundle_is_linted_too():
    # Both halves (#3679): the compiler's output and the hand-authored meta rules.
    assert set(lint.EXTRA_RULE_FILES) == {
        "tests/rulepacks/runtime-canary.compiled.rules.yaml",
        "tests/rulepacks/runtime-canary.meta.rules.yaml",
    }
    for rel in lint.EXTRA_RULE_FILES:
        assert (_REPO / rel).is_file(), rel


def test_labels_are_checked_too():
    rule = {"alert": "A", "expr": "x > 1", "labels": {"owner": "{{ $labels.owner }}"}}
    assert lint.rule_violations(rule)


def test_an_absent_tenant_renders_empty_by_construction():
    """`with query` skips an empty result: no `index` on an empty slice."""
    for tpl in lint.METADATA_ANNOTATIONS.values():
        assert tpl.startswith("{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})`")
        assert "{{ with index . 0 }}" in tpl and tpl.endswith("{{ end }}{{ end }}")


# --- 3. pins ------------------------------------------------------------------

def test_metadata_labels_match_exporter():
    """A label added to tenant_metadata_info in collector.go must be decided on."""
    src = (_REPO / "components/threshold-exporter/app/internal/scrape/collector.go"
           ).read_text(encoding="utf-8")
    m = re.search(r'NewDesc\(\s*"tenant_metadata_info",\s*"(?:[^"\\]|\\.)*",\s*'
                  r'\[\]string\{([^}]*)\}', src)
    assert m, "tenant_metadata_info NewDesc not found in collector.go"
    labels = tuple(re.findall(r'"([^"]+)"', m.group(1)))
    assert labels == ("tenant",) + lint.METADATA_LABELS


def test_go_compiler_uses_the_same_query_and_no_join():
    src = (_REPO / "components/threshold-exporter/app/pkg/config/custom_alert_compile.go"
           ).read_text(encoding="utf-8")
    m = re.search(r'const metadataQuery = "((?:[^"\\]|\\.)*)"', src)
    assert m, "metadataQuery const not found in custom_alert_compile.go"
    assert m.group(1).replace('\\"', '"') == lint.METADATA_QUERY
    assert "group_left(runbook_url" not in src
    assert "metadataJoinRHS" not in src


def test_the_compiled_custom_alert_pack_carries_the_canonical_templates():
    pack = (_REPO / "rule-packs/rule-pack-custom-alerts.yaml").read_text(encoding="utf-8")
    for tpl in lint.METADATA_ANNOTATIONS.values():
        assert tpl in pack


# --- 4. live -----------------------------------------------------------------

def test_live_repo_rule_packs_are_clean(da_read_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_leftouterjoin_enrichment.py", "--ci"])
    assert lint.main() == 0, capsys.readouterr()
