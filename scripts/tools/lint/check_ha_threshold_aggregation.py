#!/usr/bin/env python3
"""HA-max invariant lint: `user_threshold` must be aggregated with `max`.

Codifies the design rule in `docs/design/high-availability.md` §4.3.

WHY: `user_threshold` is a per-tenant CONFIG value emitted by every
threshold-exporter replica. Under HA the exporter runs replicaCount=2 with
NO leader-election, and the metric carries no per-pod label
(`collector.go`: label set = {tenant, metric, component, severity}). So
Prometheus scrapes TWO identical `user_threshold` series per (tenant, metric,
severity). A recording rule that aggregates them with `sum by(tenant)` then
ADDS the two replicas → the threshold DOUBLES (a `70` becomes `140`) → the
alert never fires. `max by(...)` collapses the duplicates back to the single
true value. This bug shipped silently for ~2 months in 4 packs' source
copies (elasticsearch / kubernetes / mongodb / redis) — see ADR-024 PR3-pre.

WHAT THIS CHECKS: every aggregation applied DIRECTLY to `user_threshold`
must use the `max` operator. `sum` is the critical HA-double-count danger;
`avg`/`min` happen to be HA-safe for identical duplicates but are off the
documented convention, so anything other than `max` is flagged.

WHAT THIS DOES **NOT** CHECK (deliberately — Gemini adversarial review):
the labels inside `by(...)`. The lint constrains only the OPERATOR, never
the grouping. Dimensional thresholds (`container_cpu{env="prod"}: "80"` +
`{env="test"}: "60"`) MUST keep their business labels (env, version,
severity, tablespace_re, ...) in `by(...)`; forcing a bare `by(tenant)` here
would flatten them and silently drop a dimension. Operator vs grouping are
separate concerns — this lint owns only the operator.

Real-metric aggregation (`rate(mysql_...)`, `sum(...real metric...)`) is NOT
constrained — only aggregations of the `user_threshold` config metric.

Exit codes:
    0  All `user_threshold` aggregations use `max`
    1  Found a non-`max` aggregation of `user_threshold` (--ci)
    2  Error (YAML parse / no files)

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

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass

# Matches an aggregation operator applied directly to user_threshold:
#   `<op> by(...) (user_threshold...`  or  `<op>(user_threshold...`
# Captures <op>. `by/without(...)` grouping is optional and its CONTENTS are
# intentionally not inspected (operator-only check).
_AGG_OVER_USER_THRESHOLD = re.compile(
    r"\b(sum|avg|min|max|count|group|stddev|stdvar|topk|bottomk|quantile)\b"
    r"\s*(?:(?:by|without)\s*\([^)]*\))?\s*\(\s*user_threshold",
)


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parent.parent.parent


def _iter_rule_groups(path: Path):
    """Yield rule groups from a rule-pack / PrometheusRule / ConfigMap file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return
    if "groups" in data:  # raw rule-pack
        yield from data["groups"]
    elif (data.get("spec") or {}).get("groups"):  # PrometheusRule CRD
        yield from data["spec"]["groups"]
    elif "data" in data:  # ConfigMap wrapping rule YAML in data keys
        for val in (data.get("data") or {}).values():
            sub = yaml.safe_load(val) or {}
            if isinstance(sub, dict):  # defensive: skip non-rule data keys
                yield from sub.get("groups", [])


def nonmax_aggregations(expr: str) -> List[Tuple[str, str]]:
    """Pure core: return [(operator, excerpt)] for each non-`max` aggregation
    applied directly to `user_threshold` in `expr`. Empty list = HA-safe.

    Only the OPERATOR is judged — the `by(...)` grouping is never inspected,
    so `max by(tenant, version, env)` is fine (dimensions preserved) while
    `sum by(tenant)` is flagged (HA double-count)."""
    # Strip PromQL line comments BEFORE collapsing newlines. `#` comments to
    # end-of-line, so flattening first would let a `# ...` comment swallow the
    # following `user_threshold` token and hide a `sum` violation (false
    # negative on a FATAL gate). Strip per-line, then check + flatten.
    s = re.sub(r"#[^\n]*", "", str(expr))
    if "user_threshold" not in s:
        return []
    flat = re.sub(r"\s+", " ", s)
    out: List[Tuple[str, str]] = []
    for m in _AGG_OVER_USER_THRESHOLD.finditer(flat):
        if m.group(1) != "max":
            out.append((m.group(1), flat[max(0, m.start() - 5):m.start() + 60]))
    return out


def check_file(path: Path) -> List[Tuple[str, str, str]]:
    """Return [(rule_name, operator, expr_excerpt)] for non-max aggregations."""
    findings: List[Tuple[str, str, str]] = []
    for group in _iter_rule_groups(path):
        for rule in group.get("rules", []):
            expr = str(rule.get("expr", ""))
            name = rule.get("record") or rule.get("alert") or "?"
            for op, excerpt in nonmax_aggregations(expr):
                findings.append((name, op, excerpt))
    return findings


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="HA-max invariant lint for user_threshold aggregation")
    parser.add_argument("--ci", action="store_true", help="exit 1 on violation")
    args = parser.parse_args()

    repo = _repo_root()
    targets: List[Path] = []
    targets += sorted((repo / "rule-packs").glob("rule-pack-*.yaml"))
    targets += sorted((repo / "operator-manifests").glob("da-rule-pack-*.yaml"))
    targets += sorted((repo / "k8s" / "03-monitoring").glob("configmap-rules-*.yaml"))
    targets = [t for t in targets if t.exists()]
    if not targets:
        print("ERROR: no rule-pack files found", file=sys.stderr)
        return 2

    total_violations = 0
    try:
        for path in targets:
            findings = check_file(path)
            if findings:
                total_violations += len(findings)
                rel = path.relative_to(repo)
                print(f"  ❌ {rel}")
                for name, op, excerpt in findings:
                    print(f"       {name}: aggregates user_threshold with "
                          f"`{op}` (must be `max`) — …{excerpt}…")
    except yaml.YAMLError as exc:
        print(f"ERROR: YAML parse failure: {exc}", file=sys.stderr)
        return 2

    if total_violations:
        print(f"\n❌ {total_violations} non-`max` aggregation(s) of "
              f"user_threshold. Under HA (replicaCount=2, no leader-election) "
              f"`sum` DOUBLES the threshold — use `max by(...)`. "
              f"See docs/design/high-availability.md §4.3.", file=sys.stderr)
        return 1 if args.ci else 0
    print(f"✅ All user_threshold aggregations across {len(targets)} files use `max`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
