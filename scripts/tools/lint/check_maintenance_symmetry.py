#!/usr/bin/env python3
"""check_maintenance_symmetry — per-arm maintenance-clause symmetry guard for rule-pack alerts.

Problem (#947 / PR #977, Gemini disposition)
--------------------------------------------
Most threshold alerts use the ADR-024 two-arm left-outer-join shape:

    ((breach UNLESS maintenance) * group_left(...) tenant_metadata_info)   # enriched arm
    or
    ((breach UNLESS maintenance) unless on(tenant) tenant_metadata_info)   # bare arm

The maintenance ``unless`` is HAND-COPIED once per arm (76 alerts / 13 packs at gate
introduction). Delete or typo ONE copy and every promtool fixture stays green unless it
carries a fifth e-tenant (breach + maintenance + NO metadata, PR #977) — and in
production an onboarding tenant (no metadata yet) gets paged during its maintenance
window. This lint kills the "one arm forgets / typo drifts" class mechanically at the
SOURCE (rule-packs/; the configmap / operator-manifest copies are drift-gated against it).

What this guards (and deliberately does NOT)
--------------------------------------------
For every alert whose expr has the two-arm marker (``unless on(tenant) tenant_metadata_info``):
  * 2 maintenance clauses  -> must sit ONE PER ARM (enriched copy before the group_left
        join, bare copy between the join and the bare marker).
  * 1 maintenance clause   -> only legal as the FACTORED form: a single top-level clause
        AFTER the or-union, i.e. ``((enriched) or (bare)) unless on(tenant) (...)``.
        ⚠️ precedence footgun: PromQL ``or`` binds LOOSER than ``unless``, so
        ``A or B unless M`` parses as ``A or (B unless M)`` — the union MUST be
        parenthesised. A top-level ``or`` left of the clause is therefore a violation.
  * 0 maintenance clauses  -> pass. Suppression may live upstream in a shared ``:core``
        recording rule (kubernetes pack) or the alert may deliberately have no
        maintenance opt-out (e.g. total-outage criticals). WHETHER an alert should have
        a maintenance opt-out is a per-alert design decision — out of scope here.
Only the canonical clause spelling counts:
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
A near-miss (typo'd filter value, dropped ``== 1``) simply does not match, drops the
count to 1-in-arm, and fails — that IS the typo detection.

Usage:
  check_maintenance_symmetry.py        # exit 0 ok / 1 violation / 2 caller-error (dev-rule #13)
  check_maintenance_symmetry.py --ci   # same, explicit for CI
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("check_maintenance_symmetry: PyYAML required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[3]                         # <repo>/scripts/tools/lint/x.py -> <repo>
_RULE_PACKS = _REPO / "rule-packs"

_CANONICAL = 'unless on(tenant) (user_state_filter{filter="maintenance"} == 1)'
_MAINT = re.compile(
    r'unless\s+on\(tenant\)\s*\(\s*user_state_filter\{filter="maintenance"\}\s*==\s*1\s*\)')
_BARE = re.compile(r'unless\s+on\(tenant\)\s+tenant_metadata_info')
_ENRICH = re.compile(r'\*\s+on\(tenant\)\s+group_left\([^)]*\)\s+tenant_metadata_info')
_OR = re.compile(r'(?<![\w:])or(?![\w:])')


def _rule_pack_files() -> list[Path]:
    """Both ``.yaml`` and ``.yml`` so a ``.yml`` pack cannot escape (fail-open; Gemini #969)."""
    return sorted(f for f in _RULE_PACKS.iterdir()
                  if f.is_file() and f.suffix in (".yaml", ".yml"))


def _mask_quotes(expr: str) -> str:
    """Blank out double-quoted label values (position-preserving) so paren-depth and
    top-level-``or`` scans cannot be spoofed by string contents."""
    out, in_q, esc = [], False, False
    for ch in expr:
        if esc:
            out.append(" " if in_q else ch)
            esc = False
            continue
        if ch == "\\":
            out.append(" " if in_q else ch)
            esc = True
            continue
        if ch == '"':
            in_q = not in_q
            out.append('"')
            continue
        out.append(" " if in_q else ch)
    return "".join(out)


def _depths(masked: str) -> list[int]:
    """Paren depth of the position BEFORE consuming each char (index-aligned with expr)."""
    d, out = 0, []
    for ch in masked:
        out.append(d)
        if ch == "(":
            d += 1
        elif ch == ")":
            d = max(0, d - 1)
    return out


def _check_expr(expr: str) -> str | None:
    """None if compliant; else a one-line violation reason."""
    bares = list(_BARE.finditer(expr))
    if not bares:
        return None                                   # single-arm shape — out of scope
    maints = list(_MAINT.finditer(expr))
    enrichs = list(_ENRICH.finditer(expr))
    if len(bares) != 1 or len(enrichs) != 1:
        return (f"unrecognized two-arm shape ({len(enrichs)} enrichment join(s), "
                f"{len(bares)} bare marker(s)) — extend check_maintenance_symmetry "
                "or restructure to the canonical single enriched/bare pair")
    bare, enrich = bares[0], enrichs[0]

    if len(maints) == 0:
        return None       # factored upstream (:core recording rule) or deliberate no-opt-out
    if len(maints) == 2:
        a, b = maints
        if a.end() <= enrich.start() and enrich.end() <= b.start() and b.end() <= bare.start():
            return None   # canonical: one copy per arm
        return ("2 maintenance clauses but NOT one-per-arm (expected: enriched-arm copy "
                "before the group_left join, bare-arm copy before the bare "
                "`unless tenant_metadata_info`)")
    if len(maints) == 1:
        m = maints[0]
        masked = _mask_quotes(expr)
        depth = _depths(masked)
        if m.start() < bare.end() or depth[m.start()] != 0:
            return ("only 1 maintenance clause and it sits INSIDE one arm — the other arm "
                    "has NO maintenance suppression (an onboarding/enriched tenant would "
                    f"page during its maintenance window). Add the twin copy `{_CANONICAL}` "
                    "to the other arm, or factor it out: ((enriched) or (bare)) unless ...")
        toplevel_or = [o for o in _OR.finditer(masked)
                       if depth[o.start()] == 0 and o.start() < m.start()]
        if toplevel_or:
            return ("factored maintenance clause with a TOP-LEVEL `or` left of it — PromQL "
                    "`or` binds looser than `unless`, so `A or B unless M` is "
                    "`A or (B unless M)` and the enriched arm loses suppression. "
                    "Parenthesise the union: ((enriched) or (bare)) unless on(tenant) (...)")
        return None       # properly factored single clause
    return (f"{len(maints)} maintenance clauses in one alert expr (expected 0, 1 factored, "
            "or 2 one-per-arm)")


def check() -> int:
    violations: list[str] = []
    scanned = 0
    for f in _rule_pack_files():
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for grp in (doc.get("groups") or []):
            for rule in (grp.get("rules") or []):
                name = rule.get("alert")
                expr = rule.get("expr")
                if not name or not isinstance(expr, str):
                    continue
                scanned += 1
                reason = _check_expr(expr)
                if reason:
                    violations.append(f"    {f.name}:{name} — {reason}")
    if violations:
        print("check_maintenance_symmetry: per-arm maintenance suppression violation(s) "
              "(#947/#977 bare-arm class):")
        print("\n".join(violations))
        print(f"  canonical clause: {_CANONICAL}")
        return 1
    print(f"check_maintenance_symmetry: OK — {scanned} alert expr(s) scanned, "
          "maintenance clauses symmetric.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ci", action="store_true", help="CI mode (exit 1 on violation)")
    ap.add_argument("files", nargs="*", help="ignored (pre-commit passes changed files)")
    args = ap.parse_args()
    del args  # flags are declarative; behaviour identical
    if not _RULE_PACKS.is_dir():
        print(f"check_maintenance_symmetry: rule-packs/ not found under {_REPO}", file=sys.stderr)
        return 2
    return check()


if __name__ == "__main__":
    sys.exit(main())
