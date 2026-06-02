#!/usr/bin/env python3
"""Left-outer-join enrichment invariant lint (ADR-024 PR3-pre Commit 3).

Codifies the onboarding-vacuum fix so it can't silently regress: every metadata
enrichment in an alert must be a LEFT-outer join, not a bare inner join.

WHY: `<firing> * on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info`
is an INNER join — a tenant with metrics but no `tenant_metadata_info` row (an
onboarding vacuum) is silently DROPPED and never alerts. The fix pairs every
such enrichment with an `or (<firing> unless on(tenant) tenant_metadata_info)`
branch so the bare firing vector still fires when metadata is absent.

WHAT THIS CHECKS (a cheap structural proxy, per file): the number of enrichment
markers `* on(tenant) group_left(runbook_url, owner, tier)` equals the number of
void branches `unless on(tenant) tenant_metadata_info`. Every enriched alert
across all 14 packs (kubernetes via :core, the other 12 via inline-duplicate)
satisfies this 1:1 pairing. A new bare inner-join enrichment → markers > branches
→ this lint fails. Semantic proof lives in tests/rulepacks/*-void_test.yaml.

Exit codes: 0 ok / 1 unpaired enrichment (--ci) / 2 error.

Usage:
    python scripts/tools/lint/check_leftouterjoin_enrichment.py        # report
    python scripts/tools/lint/check_leftouterjoin_enrichment.py --ci   # exit 1
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

ENRICH_MARKER = "* on(tenant) group_left(runbook_url, owner, tier)"
VOID_BRANCH = "unless on(tenant) tenant_metadata_info"


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parent.parent.parent


def count_pair(text: str) -> Tuple[int, int]:
    """Pure core: (enrichment markers, void branches) in a rule/configmap file."""
    return text.count(ENRICH_MARKER), text.count(VOID_BRANCH)


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Left-outer-join enrichment invariant lint (ADR-024)")
    parser.add_argument("--ci", action="store_true", help="exit 1 on violation")
    args = parser.parse_args()

    # SOURCE only. The generated configmaps re-serialize the expr (yaml.dump
    # re-wraps lines), which perturbs the literal-substring count — and they are
    # already guaranteed `== source` by generate_rulepack_configmaps.py --check.
    # So source being left-outer-join + configmap == source ⇒ configmap correct.
    repo = _repo_root()
    targets: List[Path] = sorted((repo / "rule-packs").glob("rule-pack-*.yaml"))
    targets = [t for t in targets if t.exists()]
    if not targets:
        print("ERROR: no rule-pack files found", file=sys.stderr)
        return EXIT_CALLER_ERROR

    violations = 0
    for path in targets:
        markers, branches = count_pair(path.read_text(encoding="utf-8"))
        if markers != branches:
            violations += 1
            print(f"  ❌ {path.relative_to(repo)}: {markers} enrichment marker(s) "
                  f"but {branches} void branch(es) — {markers - branches} bare "
                  f"inner-join(s) would drop onboarding-vacuum tenants")

    if violations:
        print(f"\n❌ {violations} pack(s) have unpaired metadata enrichment. Every "
              f"`{ENRICH_MARKER}` must be paired with an `or (… {VOID_BRANCH})` "
              f"branch (ADR-024 PR3-pre Commit 3). See tests/rulepacks/*-void_test.yaml.",
              file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK
    print(f"✅ All {len(targets)} rule pack(s): metadata enrichment is "
          f"left-outer-join (no onboarding-vacuum drop).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
