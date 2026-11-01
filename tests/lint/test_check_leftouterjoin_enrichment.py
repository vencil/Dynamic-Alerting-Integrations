"""Tests for check_leftouterjoin_enrichment.py — ADR-024 onboarding-vacuum guard.

Pinned contracts
----------------
1. A left-outer-join enrichment (marker paired with a void branch) → markers ==
   branches (compliant).
2. A bare inner-join enrichment (marker without a void branch) → markers >
   branches (flagged).
3. Live dogfood: every committed rule pack pairs every enrichment with a void
   branch (gates a regression of the PR3-pre Commit 3 sweep).
4. The join's right-hand side is exactly ENRICH_RHS (#3483): the raw
   `tenant_metadata_info`, in any spelling, is flagged — it fails with
   many-to-many once the exporter's replicas are scraped per pod.
5. ENRICH_RHS's label set is the exporter's own (collector.go), and the Go
   custom-alert compiler emits the same string — so neither can drift alone.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_leftouterjoin_enrichment as lint  # noqa: E402
import _lib_consumer_read as cr  # noqa: E402  (#3471: the rule-source declaration)

_BARE = (
    "        expr: |\n"
    "          (X)\n"
    "          * on(tenant) group_left(runbook_url, owner, tier)\n"
    "            max by(tenant, runbook_url, owner, tier) (tenant_metadata_info)\n"
)
_LEFT_OUTER = (
    "        expr: |\n"
    "          (\n"
    "            (X)\n"
    "            * on(tenant) group_left(runbook_url, owner, tier)\n"
    "              max by(tenant, runbook_url, owner, tier) (tenant_metadata_info)\n"
    "          )\n"
    "          or\n"
    "          (\n"
    "            (X)\n"
    "            unless on(tenant) tenant_metadata_info\n"
    "          )\n"
)


def test_bare_inner_join_flagged():
    markers, branches = lint.count_pair(_BARE)
    assert markers == 1 and branches == 0
    assert markers != branches  # the lint condition


def test_left_outer_join_compliant():
    markers, branches = lint.count_pair(_LEFT_OUTER)
    assert markers == 1 and branches == 1
    assert markers == branches


def test_no_enrichment_compliant():
    markers, branches = lint.count_pair("        expr: |\n          up == 0\n")
    assert markers == 0 and branches == 0


def test_live_repo_packs_all_paired():
    repo = lint._repo_root()
    packs = cr.RULE_PACKS.files_under(repo)   # #3471: the declaration
    assert packs, "expected rule packs under rule-packs/"
    for p in packs:
        markers, branches = lint.count_pair(p.read_text(encoding="utf-8"))
        assert markers == branches, (
            f"{p.name}: {markers} enrichment marker(s) but {branches} void "
            f"branch(es) — a bare inner-join would drop onboarding-vacuum tenants"
        )


# --- #3483: the join's right-hand side -------------------------------------

_REPO = Path(__file__).resolve().parents[2]


def test_canonical_rhs_compliant():
    assert lint.noncanonical_joins(_LEFT_OUTER) == []


def test_raw_rhs_flagged():
    raw = _LEFT_OUTER.replace(lint.ENRICH_RHS, "tenant_metadata_info")
    assert lint.noncanonical_joins(raw) == [(4, "tenant_metadata_info")]


def test_raw_rhs_same_line_flagged():
    text = ("expr: X * on(tenant) group_left(runbook_url, owner, tier) "
            "tenant_metadata_info\n")
    assert [ln for ln, _ in lint.noncanonical_joins(text)] == [1]


def test_raw_rhs_variant_marker_flagged():
    # A different label list / spacing escapes the exact marker, but a group_left
    # onto the raw metric is still caught.
    text = "X\n  * on(tenant) group_left(owner)\n    tenant_metadata_info\n"
    assert [ln for ln, _ in lint.noncanonical_joins(text)] == [2]


def test_other_rhs_flagged():
    # `without` (or any other collapse) is not the agreed spelling.
    text = ("X\n  * on(tenant) group_left(runbook_url, owner, tier)\n"
            "    max without(instance, pod) (tenant_metadata_info)\n")
    assert [ln for ln, _ in lint.noncanonical_joins(text)] == [2]


def test_void_branch_is_not_a_join():
    assert lint.noncanonical_joins("  unless on(tenant) tenant_metadata_info\n") == []


def test_live_repo_joins_all_canonical():
    for p in cr.RULE_PACKS.files_under(_REPO):
        assert lint.noncanonical_joins(p.read_text(encoding="utf-8")) == [], p.name


def test_metadata_labels_match_exporter():
    """The `by` list must be exactly what the exporter serves: a label added to
    tenant_metadata_info in collector.go would otherwise be collapsed away."""
    src = (_REPO / "components/threshold-exporter/app/internal/scrape/collector.go"
           ).read_text(encoding="utf-8")
    m = re.search(r'NewDesc\(\s*"tenant_metadata_info",\s*"(?:[^"\\]|\\.)*",\s*'
                  r'\[\]string\{([^}]*)\}', src)
    assert m, "tenant_metadata_info NewDesc not found in collector.go"
    labels = tuple(re.findall(r'"([^"]+)"', m.group(1)))
    assert labels == ("tenant",) + lint.METADATA_LABELS
    assert lint.ENRICH_RHS == (f"max by({', '.join(labels)}) (tenant_metadata_info)")


def test_go_compiler_emits_the_same_rhs():
    src = (_REPO / "components/threshold-exporter/app/pkg/config/custom_alert_compile.go"
           ).read_text(encoding="utf-8")
    m = re.search(r'const metadataJoinRHS = "([^"]*)"', src)
    assert m, "metadataJoinRHS const not found in custom_alert_compile.go"
    assert m.group(1) == lint.ENRICH_RHS
    # ...and the compiler joins through it, not through a raw string literal.
    assert "group_left(runbook_url, owner, tier)" in src
    assert not re.search(r'"\s*tenant_metadata_info\\n"', src)


# Spellings that miss ENRICH_MARKER but still join the raw metric (or drop
# metadata labels): case, group_right, one-to-one, no space after commas.
_ESCAPES = {
    "upper_group_left": "x * on(tenant) GROUP_LEFT(runbook_url, owner, tier) tenant_metadata_info",
    "group_right": "x * on(tenant) group_right(runbook_url, owner, tier) tenant_metadata_info",
    "one_to_one": "x * on(tenant) tenant_metadata_info",
    "no_space_partial_max": ("x * on(tenant) group_left(runbook_url,owner,tier) "
                             "max by(tenant) (tenant_metadata_info)"),
}


def test_marker_escaping_spellings_flagged():
    for name, text in _ESCAPES.items():
        assert lint.noncanonical_joins(text), name


def test_canonical_rhs_spacing_and_set_operators_pass():
    ok = ("x * on(tenant) group_left(runbook_url,owner,tier)\n"
          "  max by (tenant, runbook_url, owner, tier) (tenant_metadata_info)")
    assert lint.noncanonical_joins(ok) == []
    assert lint.noncanonical_joins("x and on (tenant) tenant_metadata_info") == []
    assert lint.noncanonical_joins("x or on(tenant) tenant_metadata_info") == []
