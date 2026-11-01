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
   many-to-many once the exporter's replicas are scraped per pod — and so is
   the `max by(...)`-only collapse: during a config reload the replicas briefly
   serve different metadata for one tenant, `max by` keeps both, and the rule
   fails for every tenant. Only the `topk by(tenant) (1, …)` wrap is accepted.
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
    "            topk by(tenant) (1, max by(tenant, runbook_url, owner, tier) "
    "(tenant_metadata_info))\n"
)
_LEFT_OUTER = (
    "        expr: |\n"
    "          (\n"
    "            (X)\n"
    "            * on(tenant) group_left(runbook_url, owner, tier)\n"
    "              topk by(tenant) (1, max by(tenant, runbook_url, owner, tier) "
    "(tenant_metadata_info))\n"
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
    assert lint.ENRICH_RHS == (f"topk by(tenant) (1, max by({', '.join(labels)}) "
                               f"(tenant_metadata_info))")


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
          "  topk by (tenant) (1, max by (tenant, runbook_url, owner, tier) "
          "(tenant_metadata_info))")
    assert lint.noncanonical_joins(ok) == []
    assert lint.noncanonical_joins("x and on (tenant) tenant_metadata_info") == []
    assert lint.noncanonical_joins("x or on(tenant) tenant_metadata_info") == []


# --- reload skew: the `max by`-only collapse is no longer canonical ---------
# During a config reload one replica can serve a tenant's new runbook_url while
# the other still serves the old one; `max by(tenant, runbook_url, owner, tier)`
# keeps both and the join fails with many-to-many for every tenant of the rule
# (tests/rulepacks/rule-pack-metadata-replicas_test.yaml proves it with promtool).

_MAX_ONLY = "max by(tenant, runbook_url, owner, tier) (tenant_metadata_info)"


def test_canonical_rhs_wraps_the_max_by_in_topk():
    assert lint.ENRICH_RHS == f"topk by(tenant) (1, {_MAX_ONLY})"


def test_new_form_accepted_in_spacing_variants():
    variants = [
        f"x * on(tenant) group_left(runbook_url, owner, tier) {lint.ENRICH_RHS}",
        ("x\n  * on(tenant) group_left(runbook_url, owner, tier)\n"
         f"    {lint.ENRICH_RHS}\n"),
        ("x * on(tenant) group_left(runbook_url,owner,tier) "
         "topk by(tenant)(1,max by(tenant,runbook_url,owner,tier)(tenant_metadata_info))"),
        ("x * on(tenant) GROUP_LEFT(runbook_url, owner, tier)\n"
         "  TOPK BY (tenant) (\n    1,\n    MAX BY (tenant, runbook_url, owner, tier)\n"
         "      (tenant_metadata_info)\n  )"),
    ]
    for text in variants:
        assert lint.noncanonical_joins(text) == [], text


def test_max_only_form_rejected():
    marker = "x\n  * on(tenant) group_left(runbook_url, owner, tier)\n"
    rejected = {
        "exact_marker": f"{marker}    {_MAX_ONLY}\n",
        "no_space": ("x * on(tenant) group_left(runbook_url,owner,tier) "
                     "max by(tenant,runbook_url,owner,tier)(tenant_metadata_info)"),
        "spaced_by": (f"{marker}    max by (tenant, runbook_url, owner, tier) "
                      "(tenant_metadata_info)\n"),
        "topk_without_by": f"{marker}    topk(1, {_MAX_ONLY})\n",
        "topk_without_max": f"{marker}    topk by(tenant) (1, tenant_metadata_info)\n",
        "topk_k2": f"{marker}    topk by(tenant) (2, {_MAX_ONLY})\n",
    }
    for name, text in rejected.items():
        assert lint.noncanonical_joins(text), name


def test_max_only_form_rejected_with_line_number():
    text = _LEFT_OUTER.replace(lint.ENRICH_RHS, _MAX_ONLY)
    assert lint.ENRICH_RHS not in text
    assert lint.noncanonical_joins(text) == [(4, _MAX_ONLY)]


# --- any label list: the right-hand operand decides (#3483 review) ---------
# These missed ENRICH_MARKER (another or no label list) and were let through
# while their right-hand side was a `max by(...)`-only collapse.
_ANY_LABEL_LIST_ESCAPES = {
    "partial_list_max_only": ("x * on(tenant) group_left(runbook_url) "
                              "max by(tenant, runbook_url, owner, tier) (tenant_metadata_info)"),
    "bare_group_left_max_only": ("x * on(tenant) group_left "
                                 "max by(tenant, runbook_url, owner, tier) (tenant_metadata_info)"),
    "owner_only": "x * on(tenant) group_left(owner) max by(tenant, owner) (tenant_metadata_info)",
    "group_right_max_only": ("x * on(tenant) group_right(runbook_url, owner, tier)\n"
                             "  max by(tenant, runbook_url, owner, tier) (tenant_metadata_info)"),
    "ignoring_one_to_one": "x / ignoring(job) max by(tenant) (tenant_metadata_info)",
    "parenthesised_raw": "x * on(tenant) group_left(owner) (tenant_metadata_info)",
    "comparison_bool": "x > bool on(tenant) max by(tenant) (tenant_metadata_info)",
}


def test_any_label_list_onto_a_non_canonical_rhs_is_flagged():
    for name, text in _ANY_LABEL_LIST_ESCAPES.items():
        assert lint.noncanonical_joins(text), name


def test_any_label_list_onto_the_canonical_rhs_passes():
    """The rule is about the right-hand operand only."""
    for mod in ("group_left(runbook_url)", "group_left", "GROUP_LEFT (owner, tier)"):
        text = f"x * on(tenant) {mod} {lint.ENRICH_RHS}"
        assert lint.noncanonical_joins(text) == [], mod


def test_an_unrelated_vector_match_is_not_flagged():
    assert lint.noncanonical_joins(
        "x > on(tenant) group_left tenant:alert_threshold:x") == []
    assert lint.noncanonical_joins(
        "x * on(tenant) group_left max by(tenant) (user_state_filter{filter=\"a\"})") == []


def test_an_apostrophe_in_a_comment_does_not_swallow_the_file():
    text = ("x * on(tenant) group_left(owner) (\n"
            "  # the tenant's metadata\n"
            "  y\n"
            ")\n"
            f"z * on(tenant) group_left(runbook_url, owner, tier) {lint.ENRICH_RHS}\n")
    assert lint.noncanonical_joins(text) == []
