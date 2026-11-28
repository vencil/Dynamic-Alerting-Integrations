#!/usr/bin/env python3
"""HA-max invariant lint: `user_threshold` must be aggregated with `max`.

Codifies the design rule in `docs/design/high-availability.md` §4.3.

WHY: `user_threshold` is a per-tenant CONFIG value emitted by every
threshold-exporter replica. Under HA the exporter runs replicaCount=2 with
NO leader-election. The exporter's own label set is the same on every
replica (`collector.go`: {tenant, metric, component, severity}), but the
platform's Prometheus scrapes per pod (#3483, PR #3515: the raw config's
`platform-components` job discovers `role: endpointslice` and adds `pod`;
Prometheus adds `instance` / `job`). So each replica's copy arrives as its
OWN series, differing from the other copy only in those scrape-identity
labels. A recording rule that aggregates them with `sum by(tenant)` then
ADDS the two replicas → the threshold DOUBLES (a `70` becomes `140`) → the
alert never fires; a `count` counts replicas, not values. `max by(...)`
collapses the copies back to the single true value. This bug shipped
silently for ~2 months in 4 packs' source copies (elasticsearch /
kubernetes / mongodb / redis).

WHAT THIS CHECKS: every aggregation whose argument still carries the raw
replica copies of `user_threshold` must use the `max` operator. `sum` is
the critical HA-double-count danger; `avg`/`min` happen to be HA-safe for
identical duplicates but are off the documented convention, so anything
other than `max` is flagged.

HOW (Prometheus' parse tree, `da-read promql-shape` via
`_lib_consumer_read.promql_shape_each`, #4215): the expression is parsed, not matched
as text, so an aggregation that reaches `user_threshold` through a function
or parentheses (`count by(x) (label_replace(user_threshold, ...))`,
`sum(abs(user_threshold))`, `topk(1, (user_threshold))`) is judged exactly
like `sum(user_threshold)`. A subtree "carries" the copies when it reaches
a `user_threshold` selector through only these nodes:

  - function calls (`label_replace`, `abs`, `clamp_*`, `*_over_time`, ...):
    they map series one-to-one and keep the scrape-identity labels — except
    `absent`, `absent_over_time` and `scalar`, whose result is no longer
    one series per replica;
  - parentheses, unary minus, range selectors, subqueries, `offset` / `@`;
  - binary operators, through EITHER operand (`sum(user_threshold * 2)`
    still adds two copies) — except the RIGHT operand of `and` / `unless`,
    which only filters the left side and contributes no series. Vector
    matching cardinality is not modelled: a copy-carrying operand counts
    whatever the `on` / `group_*` clause says (over-flagging, never under).

An aggregation stops the walk: past any aggregation the copies count as
collapsed (operator-only — see below), so `count(max by(...)
(user_threshold))` passes.

A selector counts as (possibly) selecting `user_threshold` unless it rules
the metric out: a metric name other than `user_threshold`, or an `__name__`
matcher (`=`, `!=`, `=~`, `!~`) that rejects it. So a nameless `{severity="warning"}` or `{__name__!="foo"}` counts: Prometheus would
select `user_threshold` with it. Other label matchers never rule it out (the
lint does not know which label values `user_threshold` series carry). The
name is matched exactly: `user_threshold_x` or a recording rule named
`user_threshold:foo` is another metric. Prometheus matches regexes with RE2;
this lint uses Python `re` (anchored, `fullmatch`). Where the two can
disagree it does not guess: a regex Python cannot compile (`\\pL`) or that
uses a POSIX class (`[[:alpha:]]`, which Python reads as a different set)
counts as accepting `user_threshold` (fail closed). Other RE2/`re`
differences in the semantics of a regex both accept are not modelled.

An expression Prometheus' parser refuses is an error (exit 2), never a
pass. Every expression here was already accepted by Prometheus' rule
loader (`_lib_consumer_read`), which uses the same parser, so this cannot
happen on the files the lint reads; a direct caller of `nonmax_aggregations`
gets the refusal, naming the expression.

WHAT THIS DOES **NOT** CHECK (deliberately — Gemini adversarial review):
the labels inside `by(...)`. The lint constrains only the OPERATOR, never
the grouping. Dimensional thresholds (`container_cpu{env="prod"}: "80"` +
`{env="test"}: "60"`) MUST keep their business labels (env, version,
severity, tablespace_re, ...) in `by(...)`; forcing a bare `by(tenant)` here
would flatten them and silently drop a dimension. Operator vs grouping are
separate concerns — this lint owns only the operator. The flip side: a
`max by(pod)` / `max without(tenant)` that KEEPS a scrape-identity label
leaves the copies apart, and an aggregation above it is not flagged.

Real-metric aggregation (`rate(mysql_...)`, `sum(...real metric...)`) is NOT
constrained — only aggregations of the `user_threshold` config metric.

Exit codes:
    0  All `user_threshold` aggregations use `max`
    1  Found a non-`max` aggregation of `user_threshold` (--ci)
    2  Error (YAML parse / no files / an expr Prometheus' parser refuses)

Usage:
    python scripts/tools/lint/check_ha_threshold_aggregation.py        # report
    python scripts/tools/lint/check_ha_threshold_aggregation.py --ci   # exit 1
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import require_project_root  # noqa: E402  (#2852)
import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass

_METRIC = "user_threshold"
# Their result is no longer one series per replica copy (see module doc).
_NON_CARRYING_FUNCS = frozenset({"absent", "absent_over_time", "scalar"})
# Set operators whose RIGHT operand only filters the left one.
_FILTER_SET_OPS = frozenset({"and", "unless"})
_EXCERPT_LEN = 100


class UnparseableExpr(ValueError):
    """Prometheus' parser refused an expression, or the walk met a node it
    does not know. Never a pass: the lint cannot say what that expression
    aggregates."""


def _repo_root() -> Path:
    """This tree's root, never an enclosing project's (#2852)."""
    return require_project_root(_THIS_DIR)


def _regex_accepts(pattern: str) -> "bool | None":
    """Whether Prometheus' anchored regex *pattern* matches `user_threshold`,
    or None when Python `re` cannot tell the way RE2 would (see module doc)."""
    if "[:" in pattern:
        return None  # POSIX class: Python reads `[[:alpha:]]` as another set
    try:
        return re.fullmatch(pattern, _METRIC) is not None
    except re.error:
        return None  # RE2-only syntax (`\pL`, ...)


def _name_matcher_accepts(m: dict) -> bool:
    """Whether `__name__` matcher *m* (a `promql_shape` matcher) lets
    `user_threshold` through. Undecidable regexes let it through (fail closed)."""
    if m["type"] == "=":
        return m["value"] == _METRIC
    if m["type"] == "!=":
        return m["value"] != _METRIC
    hit = _regex_accepts(m["value"])
    if hit is None:
        return True
    return hit if m["type"] == "=~" else not hit


def _may_select_metric(sel: dict) -> bool:
    """True unless selector *sel* rules `user_threshold` out: a metric name
    other than it, or a `__name__` matcher rejecting it. A nameless
    `{severity="warning"}` may select it."""
    if sel["name"] and sel["name"] != _METRIC:
        return False
    return all(_name_matcher_accepts(m) for m in sel["matchers"] if m["name"] == "__name__")


def _walk(node: dict, src: bytes, out: List[Tuple[str, str]]) -> bool:
    """Append a finding to *out* for every non-`max` aggregation in *node*
    (a `promql_shape` tree over *src*) whose argument carries raw
    `user_threshold` replica copies. Return whether *node* itself still
    carries them (see the module doc)."""
    kind = node["kind"]
    if kind == "aggregate":
        if node["param"] is not None:
            _walk(node["param"], src, out)  # topk's k, quantile's φ: findings only
        if _walk(node["expr"], src, out) and node["op"] != "max":
            excerpt = src[node["start"]:node["end"]].decode("utf-8", "replace")
            out.append((node["op"], excerpt[:_EXCERPT_LEN]))
        return False  # past an aggregation the copies count as collapsed
    if kind == "selector":
        return _may_select_metric(node)
    if kind in ("paren", "unary", "subquery"):
        return _walk(node["expr"], src, out)
    if kind == "call":
        carried = [_walk(arg, src, out) for arg in node["args"]]
        return any(carried) and node["name"] not in _NON_CARRYING_FUNCS
    if kind == "binary":
        lhs = _walk(node["lhs"], src, out)
        rhs = _walk(node["rhs"], src, out)
        return lhs or (rhs and node["op"] not in _FILTER_SET_OPS)
    if kind in ("number", "string"):
        return False
    # A node kind this walk does not know: refuse rather than pass it by.
    raise UnparseableExpr(f"unhandled PromQL node kind {kind!r}")


def _findings(expr: str, tree) -> List[Tuple[str, str]]:
    if isinstance(tree, _lib_consumer_read.InputRefused):
        raise UnparseableExpr(
            f"Prometheus' parser refuses the expression: {_lib_consumer_read.error_text(tree)}")
    out: List[Tuple[str, str]] = []
    _walk(tree, expr.encode("utf-8"), out)
    return out


def nonmax_aggregations(expr: str) -> List[Tuple[str, str]]:
    """Pure core: return [(operator, excerpt)] for each non-`max` aggregation
    whose argument carries raw `user_threshold` replica copies in `expr` —
    directly or through functions, parentheses and binary operators (see the
    module doc). Empty list = HA-safe.

    Only the OPERATOR is judged — the `by(...)` grouping is never inspected,
    so `max by(tenant, version, env)` is fine (dimensions preserved) while
    `sum by(tenant)` is flagged (HA double-count).

    Raises `UnparseableExpr` when Prometheus' parser refuses `expr`. The
    expression is parsed, so `#` comments need no stripping: the parser
    drops them, and a `#` inside a string stays text. No textual pre-filter:
    a nameless `{severity="warning"}` selects user_threshold without spelling
    it, so every expression is parsed and walked."""
    s = str(expr)
    return _findings(s, _lib_consumer_read.promql_shape_each([s])[0])


def check_groups(groups: list) -> List[Tuple[str, str, str]]:
    """Return [(rule_name, operator, expr_excerpt)] for non-max aggregations.
    Every rule's expression is read in one da-read run (#4215). Raises
    `UnparseableExpr` naming the rule whose expr cannot be parsed."""
    rules = [rule for group in groups for rule in group.get("rules", [])]
    exprs = [str(rule.get("expr", "")) for rule in rules]
    findings: List[Tuple[str, str, str]] = []
    for rule, expr, tree in zip(rules, exprs, _lib_consumer_read.promql_shape_each(exprs)):
        name = rule.get("record") or rule.get("alert") or "?"
        try:
            found = _findings(expr, tree)
        except UnparseableExpr as exc:
            raise UnparseableExpr(f"{name}: {exc}") from exc
        for op, excerpt in found:
            findings.append((name, op, excerpt))
    return findings


def findings_by_file(paths: List[Path]) -> "dict[str, list]":
    """{str(path): `check_groups` findings} over whatever rule files each of
    *paths* hands to Prometheus — `_lib_consumer_read.rule_files_in` (#3426):
    a raw rule pack, a ConfigMap's values, a PrometheusRule's spec, decided
    by the readers, never by the directory a file sits in. Raises
    `RuleFileSourcesRefused` when no reader reads a file, `RuleFilesRefused`
    when Prometheus' rule loader refuses a rule file."""
    found = _lib_consumer_read.rule_files_in(paths, only_declared=False)
    _lib_consumer_read.require_read([rf for p in paths for rf in found[str(p)]])
    return {str(p): [f for rf in found[str(p)] for f in check_groups(rf.groups)]
            for p in paths}


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="HA-max invariant lint for user_threshold aggregation")
    parser.add_argument("--ci", action="store_true", help="exit 1 on violation")
    args = parser.parse_args()

    repo = _repo_root()
    targets: List[Path] = []
    try:
        # #3426/#3471: which files are rule packs and their copies has one
        # declaration (`RULE_SOURCES`: rule-pack-*, configmap-rules-*,
        # .yaml|yml; an undeclared YAML there is named) —
        # not a glob of this lint's own.
        for source in _lib_consumer_read.RULE_SOURCES:
            targets += source.files_under(repo)
    except _lib_consumer_read.ConsumerReadError as exc:
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not targets:
        print("ERROR: no rule-pack files found", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        by_file = findings_by_file(targets)
    except _lib_consumer_read.RuleFilesRefused as exc:
        # A rule file Prometheus would not load is malformed input — the
        # parse-failure exit this lint always had, now naming each file.
        for rf in exc.refused:
            print(f"ERROR: Prometheus' rule loader refuses {rf.path}: {rf.problem}",
                  file=sys.stderr)
        return EXIT_CALLER_ERROR
    except _lib_consumer_read.ConsumerReadError as exc:
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except UnparseableExpr as exc:
        # Prometheus' parser refused it: this lint cannot judge the
        # expression, so it does not pass it.
        print(f"ERROR: cannot judge a rule expression: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    total_violations = 0
    for path in targets:
        findings = by_file.get(str(path), [])
        if findings:
            total_violations += len(findings)
            rel = path.relative_to(repo)
            print(f"  ❌ {rel}")
            for name, op, excerpt in findings:
                print(f"       {name}: aggregates user_threshold with "
                      f"`{op}` (must be `max`) — …{excerpt}…")

    if total_violations:
        print(f"\n❌ {total_violations} non-`max` aggregation(s) of "
              f"user_threshold. Under HA (replicaCount=2, no leader-election) "
              f"`sum` DOUBLES the threshold — use `max by(...)`. "
              f"See docs/design/high-availability.md §4.3.", file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK
    print(f"✅ All user_threshold aggregations across {len(targets)} files use `max`.")
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
