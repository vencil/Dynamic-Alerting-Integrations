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

SECOND CHECK — the join's right-hand side (#3483). threshold-exporter runs more
than one replica and every replica serves `tenant_metadata_info`. Scraped per
pod (a ServiceMonitor, or any pod/endpoints discovery), the same tenant arrives
once per replica, differing only in scrape-identity labels (instance, pod, …),
and `* on(tenant) group_left(...) tenant_metadata_info` then fails at evaluation
with "many-to-many matching not allowed" — the whole alert stops evaluating.
So the right-hand side of every enrichment must be exactly `ENRICH_RHS`, which
collapses the replicas by the label set the exporter serves. `by` (not
`without`) is deliberate: `without` would have to list every per-replica label
a scrape config could add (a relabelled `node`, say), and any it missed brings
the error back. `by` cannot silently drop a metadata label either, because
`METADATA_LABELS` is pinned to the exporter's own label list by
tests/lint/test_check_leftouterjoin_enrichment.py (it reads collector.go).
The void branch keeps the raw metric: `unless` matches on presence and does not
fail on duplicate right-hand series.

Exit codes: 0 ok / 1 unpaired or non-canonical enrichment (--ci) / 2 error.

Usage:
    python scripts/tools/lint/check_leftouterjoin_enrichment.py        # report
    python scripts/tools/lint/check_leftouterjoin_enrichment.py --ci   # exit 1
"""
from __future__ import annotations

import argparse
import os
import re
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
from _lib_compat import require_project_root  # noqa: E402  (#2852)
import _lib_consumer_read  # noqa: E402  (#3471: which files are rule packs)

# The labels tenant_metadata_info carries besides `tenant` — the one place they
# are spelled. Pinned to the exporter's NewDesc label list by the test suite.
METADATA_LABELS = ("runbook_url", "owner", "tier")
ENRICH_MARKER = f"* on(tenant) group_left({', '.join(METADATA_LABELS)})"
# The only accepted right-hand side of an enrichment join. The Go compiler
# (custom_alert_compile.go: metadataJoinRHS) emits the same string; a test pins it.
ENRICH_RHS = (f"max by(tenant, {', '.join(METADATA_LABELS)}) "
              f"(tenant_metadata_info)")
VOID_BRANCH = "unless on(tenant) tenant_metadata_info"

# Any vector match onto the raw metric, whatever its case, spacing, label list or
# cardinality modifier (group_left, group_right, or none = one-to-one) — every
# arithmetic/comparison match on it breaks under per-pod scraping. The set
# operators (`and`, `or`, `unless`) match on presence and are exempt.
_RAW_JOIN = re.compile(
    r"(\b[a-z_]+\b|[-+*/%^]|[<>=!]=?)\s*(?:on|ignoring)\s*\([^)]*\)\s*"
    r"(?:group_(?:left|right)\s*(?:\([^)]*\))?\s*)?\(?\s*tenant_metadata_info\b",
    re.IGNORECASE)
_SET_OPERATORS = {"and", "or", "unless"}
# A group_left/group_right carrying exactly the metadata labels, in any case or
# spacing: its right-hand side must be ENRICH_RHS (whitespace-insensitive), or a
# spelling that misses ENRICH_MARKER would escape both checks above.
_METADATA_GROUP = re.compile(r"group_(?:left|right)\s*\(([^)]*)\)", re.IGNORECASE)


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _repo_root() -> Path:
    """This tree's root, never an enclosing project's (#2852)."""
    return require_project_root(_THIS_DIR)


def count_pair(text: str) -> Tuple[int, int]:
    """Pure core: (enrichment markers, void branches) in a rule/configmap file."""
    return text.count(ENRICH_MARKER), text.count(VOID_BRANCH)


def noncanonical_joins(text: str) -> List[Tuple[int, str]]:
    """Pure core: (line number, offending text) for every enrichment join whose
    right-hand side is not ENRICH_RHS, plus every group_left onto the raw metric."""
    found = {}
    for m in re.finditer(re.escape(ENRICH_MARKER) + r"\s*", text):
        if not text.startswith(ENRICH_RHS, m.end()):
            line = text.count("\n", 0, m.start()) + 1
            rest = text[m.end():].split("\n", 1)[0].strip()
            found[line] = rest
    for m in _RAW_JOIN.finditer(text):
        if m.group(1).lower() in _SET_OPERATORS:
            continue
        line = text.count("\n", 0, m.start()) + 1
        found.setdefault(line, " ".join(m.group(0).split()))
    want = _squash(ENRICH_RHS)
    for m in _METADATA_GROUP.finditer(text):
        labels = {x.strip() for x in m.group(1).split(",") if x.strip()}
        if labels != set(METADATA_LABELS):
            continue
        if not _squash(text[m.end():m.end() + 4 * len(ENRICH_RHS)]).startswith(want):
            line = text.count("\n", 0, m.start()) + 1
            found.setdefault(line, " ".join(text[m.start():].split("\n", 1)[0].split()))
    return sorted(found.items())


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
    try:
        # #3471: the rule packs are the one declaration's (rule-pack-*.yaml|yml;
        # an undeclared YAML beside them is named).
        targets: List[Path] = _lib_consumer_read.rule_pack_files(
            repo / _lib_consumer_read.RULE_PACKS.directory)
    except _lib_consumer_read.ConsumerReadError as exc:
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not targets:
        print("ERROR: no rule-pack files found", file=sys.stderr)
        return EXIT_CALLER_ERROR

    violations = 0
    rhs_violations = 0
    for path in targets:
        text = path.read_text(encoding="utf-8")
        markers, branches = count_pair(text)
        if markers != branches:
            violations += 1
            print(f"  ❌ {path.relative_to(repo)}: {markers} enrichment marker(s) "
                  f"but {branches} void branch(es) — {markers - branches} bare "
                  f"inner-join(s) would drop onboarding-vacuum tenants")
        for line, rest in noncanonical_joins(text):
            rhs_violations += 1
            print(f"  ❌ {path.relative_to(repo)}:{line}: enrichment join right-hand "
                  f"side is `{rest}`, not `{ENRICH_RHS}`")

    if violations:
        print(f"\n❌ {violations} pack(s) have unpaired metadata enrichment. Every "
              f"`{ENRICH_MARKER}` must be paired with an `or (… {VOID_BRANCH})` "
              f"branch (ADR-024 PR3-pre Commit 3). See tests/rulepacks/*-void_test.yaml.",
              file=sys.stderr)
    if rhs_violations:
        print(f"\n❌ {rhs_violations} enrichment join(s) do not use `{ENRICH_RHS}` "
              f"as the right-hand side. threshold-exporter runs several replicas; "
              f"scraped per pod, the raw metric has one series per replica and the "
              f"join fails with many-to-many matching (#3483).",
              file=sys.stderr)
    if violations or rhs_violations:
        return EXIT_VIOLATION if args.ci else EXIT_OK
    print(f"✅ All {len(targets)} rule pack(s): metadata enrichment is "
          f"left-outer-join (no onboarding-vacuum drop) and joins `{ENRICH_RHS}`.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
