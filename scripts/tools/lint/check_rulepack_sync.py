#!/usr/bin/env python3
"""Rule-pack copy drift guard (ADR-024 PR3-pre).

Every rule pack lives in THREE places that must stay semantically identical:

  1. rule-packs/rule-pack-<name>.yaml                 (canonical source)
  2. k8s/03-monitoring/configmap-rules-<name>.yaml    (ConfigMap deploy copy)
  3. operator-manifests/da-rule-pack-<name>.yaml      (PrometheusRule CRD copy)

The three have DIFFERENT serializations (raw `groups:` block scalars vs a
ConfigMap wrapping rules in `data:` keys vs a CRD with machine-serialized
single-line `expr` strings), so a raw `diff` / `sha256sum` ALWAYS differs even
when the rules are identical — a byte comparison is the wrong tool here.

This guard instead compares them SEMANTICALLY: each source is parsed, every
rule is reduced to a canonical identity → content map (expr whitespace
collapsed; all other fields YAML-normalized), and the three maps are compared.
Comparing PARSED structures (not raw text) is what makes the check robust to
serialization / YAML-emitter differences — the fragility that bites a naive
text diff simply does not exist once both sides are valid YAML parsed into
the same shape.

Why this matters: nothing else enforces content parity — the `Rule Pack stats
drift` hook only counts rules, so a hot-fix that edits one copy but forgets
another would ship behavior-divergent PromQL to production (a copy firing on
stale thresholds) and CI would wave it through. This guard makes any such
divergence a hard CI failure.

Exit codes:
    0  All packs' three copies are semantically identical
    1  Drift detected (--ci) — details printed
    2  Error (missing file, YAML parse failure)

Usage:
    python scripts/tools/lint/check_rulepack_sync.py            # report
    python scripts/tools/lint/check_rulepack_sync.py --ci       # exit 1 on drift
    python scripts/tools/lint/check_rulepack_sync.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover - compat shim optional
    def try_utf8_stdout() -> None:  # type: ignore
        pass


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parent.parent.parent


def _norm_expr(expr: Any) -> str:
    """Canonicalize a PromQL expression for serialization-agnostic comparison.

    Three normalizations so logically-equivalent PromQL compares equal no
    matter how it was serialized (block scalar vs `\\n`-escaped string):
      1. Strip `#` line comments BEFORE collapsing newlines (Gemini review):
         `#` comments to end-of-line, so flattening first would let a comment
         swallow following tokens → a false drift/no-drift.
      2. Collapse all whitespace runs to a single space.
      3. Remove whitespace adjacent to operator/grouping punctuation so
         `by(tenant)` == `by (tenant)` == `by(tenant, version)` (token-level
         minify, Gemini review) — emitters differ on this spacing.
    """
    s = re.sub(r"#[^\n]*", "", str(expr))
    s = re.sub(r"\s+", " ", s).strip()
    # Drop spaces around ( ) , and comparison operators; normalize the space
    # after by/without/on/group_left/group_right to none before '('.
    s = re.sub(r"\s*([(),])\s*", r"\1", s)
    s = re.sub(r"\s*(==|!=|>=|<=|>|<)\s*", r"\1", s)
    s = re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\s+\(", r"\1(", s)
    return s


def _norm_rule(rule: dict) -> dict:
    """Canonicalize a single rule for comparison: expr whitespace-collapsed,
    every other field left as YAML-parsed (already serialization-normalized)."""
    out = dict(rule)
    if "expr" in out:
        out["expr"] = _norm_expr(out["expr"])
    return out


def _rule_identity(group_name: str, rule: dict) -> str:
    if "record" in rule:
        return f"{group_name}::record::{rule['record']}"
    if "alert" in rule:
        return f"{group_name}::alert::{rule['alert']}"
    return f"{group_name}::unknown::{json.dumps(rule, sort_keys=True)[:40]}"


def _extract(groups: List[dict]) -> Dict[str, str]:
    """Reduce a list of rule groups to {rule_identity: canonical_json}.

    The group's `interval` is folded into each rule's content so an interval
    drift between copies is also caught.
    """
    out: Dict[str, str] = {}
    for g in groups or []:
        gname = g.get("name", "")
        interval = g.get("interval")
        for rule in g.get("rules", []):
            ident = _rule_identity(gname, rule)
            payload = {"interval": interval, "rule": _norm_rule(rule)}
            out[ident] = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return out


def _groups_from_rulepack(path: Path) -> List[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("groups", [])


def _groups_from_configmap(path: Path) -> List[dict]:
    """A ConfigMap wraps rules in `data:` keys; each value is a YAML document
    with its own top-level `groups:`. Merge them all."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    groups: List[dict] = []
    for _key, val in (data.get("data") or {}).items():
        sub = yaml.safe_load(val) or {}
        groups.extend(sub.get("groups", []))
    return groups


def _groups_from_operator(path: Path) -> List[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (data.get("spec") or {}).get("groups", [])


def _diff_maps(canonical: Dict[str, str], other: Dict[str, str]) -> List[str]:
    """Return human-readable divergences of `other` vs the canonical source."""
    findings: List[str] = []
    ck, ok = set(canonical), set(other)
    for missing in sorted(ck - ok):
        findings.append(f"missing in copy: {missing}")
    for extra in sorted(ok - ck):
        findings.append(f"extra in copy (not in source): {extra}")
    for ident in sorted(ck & ok):
        if canonical[ident] != other[ident]:
            findings.append(f"content differs: {ident}")
    return findings


def check_pack(repo: Path, name: str) -> Tuple[bool, List[str]]:
    """Compare the three copies of one pack. Returns (ok, findings)."""
    rp = repo / "rule-packs" / f"rule-pack-{name}.yaml"
    cm = repo / "k8s" / "03-monitoring" / f"configmap-rules-{name}.yaml"
    op = repo / "operator-manifests" / f"da-rule-pack-{name}.yaml"

    findings: List[str] = []
    if not cm.exists():
        findings.append(f"configmap copy missing: {cm}")
    if not op.exists():
        findings.append(f"operator copy missing: {op}")
    if findings:
        return False, findings

    canonical = _extract(_groups_from_rulepack(rp))
    cm_map = _extract(_groups_from_configmap(cm))
    op_map = _extract(_groups_from_operator(op))

    for label, other in (("configmap", cm_map), ("operator", op_map)):
        for f in _diff_maps(canonical, other):
            findings.append(f"[{label}] {f}")

    return (len(findings) == 0), findings


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description="Rule-pack 3-copy semantic drift guard")
    parser.add_argument("--ci", action="store_true", help="exit 1 on any drift")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    repo = _repo_root()
    pack_files = sorted((repo / "rule-packs").glob("rule-pack-*.yaml"))
    if not pack_files:
        print("ERROR: no rule packs found under rule-packs/", file=sys.stderr)
        return 2

    results = {}
    any_drift = False
    try:
        for pf in pack_files:
            name = pf.stem.replace("rule-pack-", "")
            ok, findings = check_pack(repo, name)
            results[name] = {"ok": ok, "findings": findings}
            if not ok:
                any_drift = True
    except yaml.YAMLError as exc:
        print(f"ERROR: YAML parse failure: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for name, r in results.items():
            mark = "✅" if r["ok"] else "❌"
            print(f"  {mark} {name}")
            for f in r["findings"]:
                print(f"       {f}")
        total = len(results)
        bad = sum(1 for r in results.values() if not r["ok"])
        print(f"\nRule-pack sync: {total - bad}/{total} packs in sync"
              + (f" — {bad} with drift" if bad else ""))

    if any_drift and args.ci:
        print("\n❌ Rule-pack copy drift detected. The 3 copies "
              "(rule-packs/ ↔ configmap ↔ operator) must match semantically.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
