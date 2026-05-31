"""Tests for check_leftouterjoin_enrichment.py — ADR-024 onboarding-vacuum guard.

Pinned contracts
----------------
1. A left-outer-join enrichment (marker paired with a void branch) → markers ==
   branches (compliant).
2. A bare inner-join enrichment (marker without a void branch) → markers >
   branches (flagged).
3. Live dogfood: every committed rule pack pairs every enrichment with a void
   branch (gates a regression of the PR3-pre Commit 3 sweep).
"""
from __future__ import annotations

import os
import sys

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_leftouterjoin_enrichment as lint  # noqa: E402

_BARE = (
    "        expr: |\n"
    "          (X)\n"
    "          * on(tenant) group_left(runbook_url, owner, tier)\n"
    "            tenant_metadata_info\n"
)
_LEFT_OUTER = (
    "        expr: |\n"
    "          (\n"
    "            (X)\n"
    "            * on(tenant) group_left(runbook_url, owner, tier)\n"
    "              tenant_metadata_info\n"
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
    packs = sorted((repo / "rule-packs").glob("rule-pack-*.yaml"))
    assert packs, "expected rule packs under rule-packs/"
    for p in packs:
        markers, branches = lint.count_pair(p.read_text(encoding="utf-8"))
        assert markers == branches, (
            f"{p.name}: {markers} enrichment marker(s) but {branches} void "
            f"branch(es) — a bare inner-join would drop onboarding-vacuum tenants"
        )
