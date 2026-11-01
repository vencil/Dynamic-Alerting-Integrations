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
So every `on(...)`/`ignoring(...)` vector match (not a set operator, whatever
its group_left/group_right label list) must spell an operand that involves
tenant_metadata_info exactly as `ENRICH_RHS` (whitespace- and case-
insensitively): the right operand always, the left one too under group_right
and for a one-to-one match. `ENRICH_RHS`
collapses the replicas by the label set the exporter serves, then keeps a single
series per tenant. The outer `topk by(tenant) (1, …)` covers the reload window:
the replicas reload their config independently (each polls every reloadInterval,
and the ConfigMap reaches each through its own node's kubelet), so for up to a
minute or two one can serve a tenant's new runbook_url/owner/tier while the
other still serves the old one. `max by` keeps both (they differ in a metadata label), the tenant has two
right-hand series, and the join fails with many-to-many for EVERY tenant of the
rule — one tenant's reload silences everyone's alert. `topk` keeps the first
series it sees on a tie (all values are 1), so that tenant meanwhile carries one
replica's — possibly stale — metadata instead. `by` (not
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
# `max by` collapses replicas that serve identical metadata; `topk by(tenant) (1, …)`
# then keeps one series per tenant when replicas briefly disagree (a config reload
# lands on one replica before the other) — without it that tenant has two
# right-hand series and the whole rule fails for every tenant.
_DEDUP_RHS = f"max by(tenant, {', '.join(METADATA_LABELS)}) (tenant_metadata_info)"
ENRICH_RHS = f"topk by(tenant) (1, {_DEDUP_RHS})"
VOID_BRANCH = "unless on(tenant) tenant_metadata_info"

# Every vector match (`on(...)` / `ignoring(...)`, with or without a
# group_left / group_right label list, whatever its case or spacing): an
# operand that involves tenant_metadata_info must be exactly ENRICH_RHS
# (whitespace- and case-insensitive) — the right operand always, the left one
# too under group_right (its "one" side) and one-to-one (duplicates on either
# side fail). The label list does not matter — `group_left(runbook_url)` or a
# bare `group_left` onto a `max by(...)`-only collapse fails under replica skew
# just the same. The set operators (`and`, `or`, `unless`) match on presence and
# are exempt.
_VECTOR_MATCH = re.compile(
    r"(\b[a-z_]+\b|[-+*/%^]|[<>=!]=?)\s*(?:on|ignoring)\s*\([^)]*\)\s*"
    r"(?:(group_(?:left|right))\b\s*(?:\([^)]*\))?)?",
    re.IGNORECASE)
# The binary operator a `bool` modifier follows (`x > bool on(...) y`).
_OPERATOR_BEFORE = re.compile(r"(?:[-+*/%^]|[<>=!]=|[<>])\s*$")
_SET_OPERATORS = {"and", "or", "unless"}
_IDENT = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_GROUPING = re.compile(r"(?:by|without)\s*\(", re.IGNORECASE)


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _balanced(text: str, i: int) -> int:
    """Index just past the parenthesis group opening at text[i] (quotes skipped)."""
    depth, quote = 0, None
    for j in range(i, len(text)):
        c = text[j]
        if quote:
            # A "..." or '...' literal cannot span lines; ending it at the
            # newline keeps an apostrophe in a `#` comment from eating the file.
            if (c == quote and text[j - 1] != "\\") or (c == "\n" and quote != "`"):
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(text)


def _balanced_back(text: str, j: int) -> int:
    """Index of the `(` matching the `)` at text[j - 1]."""
    depth = 0
    for k in range(j - 1, -1, -1):
        if text[k] == ")":
            depth += 1
        elif text[k] == "(":
            depth -= 1
            if depth == 0:
                return k
    return 0


def _skip_ws_back(text: str, j: int) -> int:
    while j > 0 and text[j - 1].isspace():
        j -= 1
    return j


def _ident_back(text: str, j: int) -> int:
    """Start of the identifier ending at text[j - 1], or j when there is none."""
    k = j
    while k > 0 and (text[k - 1].isalnum() or text[k - 1] in "_:"):
        k -= 1
    return k


def _left_operand(text: str, j: int) -> str:
    """The left-hand operand of a binary operator starting at *j*: a selector,
    a parenthesised group, or a function / aggregation call with its
    `by|without (...)` clause."""
    end = j = _skip_ws_back(text, j)
    if j > 0 and text[j - 1] == "}":
        j = text.rfind("{", 0, j)
        j = _ident_back(text, max(j, 0))
    elif j > 0 and text[j - 1] == ")":
        j = _balanced_back(text, j)
        k = _skip_ws_back(text, j)
        if k > 0 and text[k - 1] == ")":                 # `agg by(...) (...)`
            g = _balanced_back(text, k)
            word = _ident_back(text, _skip_ws_back(text, g))
            if text[word:_skip_ws_back(text, g)].lower() in ("by", "without"):
                j = _ident_back(text, _skip_ws_back(text, word))
        else:
            w = _ident_back(text, k)                     # `fn(...)` / `agg(...)`
            if w < k and text[w:k].lower() not in _SET_OPERATORS | {"on", "ignoring", "bool"}:
                j = w
    else:
        j = _ident_back(text, j)
    return text[j:end]


def _operand(text: str, i: int) -> str:
    """The right-hand operand of a vector match whose modifiers end at *i*: a
    parenthesised group, or a name (selector / function / aggregation) with its
    optional `by|without (...)`, `{...}` and argument group."""
    i = start = _skip_ws(text, i)
    if i < len(text) and text[i] == "(":
        return text[start:_balanced(text, i)]
    m = _IDENT.match(text, i)
    if not m:
        return ""
    i = m.end()
    g = _GROUPING.match(text, _skip_ws(text, i))
    if g:
        i = _balanced(text, g.end() - 1)
    j = _skip_ws(text, i)
    if j < len(text) and text[j] == "{":
        k = text.find("}", j)
        i = len(text) if k < 0 else k + 1
        j = _skip_ws(text, i)
    if j < len(text) and text[j] == "(":
        i = _balanced(text, j)
    return text[start:i]


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _repo_root() -> Path:
    """This tree's root, never an enclosing project's (#2852)."""
    return require_project_root(_THIS_DIR)


def count_pair(text: str) -> Tuple[int, int]:
    """Pure core: (enrichment markers, void branches) in a rule/configmap file."""
    return text.count(ENRICH_MARKER), text.count(VOID_BRANCH)


def noncanonical_joins(text: str) -> List[Tuple[int, str]]:
    """Pure core: (line number, offending text) for every vector match
    (`on(...)` / `ignoring(...)`, any label list, not a set operator) whose
    operand involves tenant_metadata_info but is not ENRICH_RHS, whitespace- and
    case-insensitively. Checked: the right operand always; the left operand too
    under group_right (it is the "one" side) and for a one-to-one match (which
    fails on duplicates on either side) — not under group_left, where the left
    is the "many" side."""
    found = {}
    want = _squash(ENRICH_RHS)
    for m in re.finditer(re.escape(ENRICH_MARKER) + r"\s*", text):
        if _squash(_operand(text, m.end())) != want:
            line = text.count("\n", 0, m.start()) + 1
            rest = text[m.end():].split("\n", 1)[0].strip()
            found[line] = rest
    for m in _VECTOR_MATCH.finditer(text):
        op = m.group(1).lower()
        if op in _SET_OPERATORS:
            continue
        modifier = (m.group(2) or "").lower()
        sides = []
        if modifier != "group_left":                     # group_right / one-to-one
            left_end = m.start()
            if op == "bool":
                b = _OPERATOR_BEFORE.search(text, 0, m.start())
                left_end = b.start() if b else m.start()
            sides.append(_left_operand(text, left_end))
        # The right operand always: under group_right it is the "many" side,
        # but metadata there is still not the agreed spelling.
        sides.append(_operand(text, m.end()))
        for operand in sides:
            if "tenant_metadata_info" in operand and _squash(operand) != want:
                line = text.count("\n", 0, m.start()) + 1
                found.setdefault(line, " ".join(f"{operand} {m.group(0)}".split()))
                break
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
