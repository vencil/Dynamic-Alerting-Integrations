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

Adversarial-review hardening (#981 fresh-eyes pass, 4 confirmed escapes closed)
-------------------------------------------------------------------------------
* **Token hygiene (fail-closed)**: every ``user_state_filter`` occurrence must match the
  canonical maintenance clause (or a known non-maintenance filter join —
  ``container_crashloop`` / ``container_imagepull``), and every ``tenant_metadata_info``
  occurrence must match the enrichment join or the bare marker. Without this, a PAIRED
  variant spelling (both arms drift together — find-and-replace, ``=~``, ``== bool 1``,
  spacing) dropped the count to 0 ("legal :core-style"), and a variant BARE marker pushed
  the whole alert silently out of scope — both escapes now FAIL loudly instead of
  silently degrading the classification. New deliberate filters must be added to
  ``_KNOWN_OTHER_USF`` (a conscious lint change, not a silent pass).
* **Arm membership**: 2 copies must be separated by the top-level ``or`` union — linear
  ordering alone could not distinguish "second copy pasted after the join but still
  inside the ENRICHED arm (bare arm naked)" from a true one-per-arm layout.
* **Recognition-collapse signal**: the OK line prints the classification distribution
  (2-copy / factored / 0-copy / single-arm) and the test suite pins a floor on the
  two-arm 2-copy count — if a refactor makes the classifier stop RECOGNISING the shape,
  the drop is loud instead of an "OK — N scanned" that never changes.

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
# Known DELIBERATE non-maintenance user_state_filter joins (kubernetes per-alert
# opt-ins). A new filter kind must be added here consciously — unknown spellings FAIL.
_KNOWN_OTHER_USF = re.compile(r'user_state_filter\{filter="container_(?:crashloop|imagepull)"\}')
_USF_TOKEN = re.compile(r'user_state_filter')
_TMI_TOKEN = re.compile(r'tenant_metadata_info')


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


def _token_hygiene(expr: str) -> str | None:
    """Fail-closed spelling guard (adversarial-review P2-2/P2-3): every marker-token
    occurrence must be part of a canonical form. Without it, a PAIRED variant spelling
    drops the maintenance count to 0 ("legal") and a variant bare marker pushes the whole
    alert out of scope — silent degradation either way."""
    covered = [m.span() for m in _MAINT.finditer(expr)]
    covered += [m.span() for m in _KNOWN_OTHER_USF.finditer(expr)]
    for t in _USF_TOKEN.finditer(expr):
        if not any(s <= t.start() and t.end() <= e for s, e in covered):
            return ("non-canonical `user_state_filter` reference — spell the maintenance "
                    f"clause exactly `{_CANONICAL}`, or add a new deliberate "
                    "non-maintenance filter to _KNOWN_OTHER_USF in this lint")
    covered = [m.span() for m in _BARE.finditer(expr)]
    covered += [m.span() for m in _ENRICH.finditer(expr)]
    for t in _TMI_TOKEN.finditer(expr):
        if not any(s <= t.start() and t.end() <= e for s, e in covered):
            return ("non-canonical `tenant_metadata_info` reference — expected the "
                    "enrichment join `* on(tenant) group_left(...) tenant_metadata_info` "
                    "or the bare marker `unless on(tenant) tenant_metadata_info` "
                    "(exact spacing)")
    return None


def _check_expr(expr: str) -> tuple[str, str | None]:
    """(classification, violation reason | None).

    Classifications: ``single-arm`` (out of scope), ``two-arm-2copy``,
    ``two-arm-factored``, ``two-arm-0copy``, ``two-arm-other``."""
    hygiene = _token_hygiene(expr)
    bares = list(_BARE.finditer(expr))
    if not bares:
        return ("single-arm", hygiene)    # hygiene still applies (P2-3 scope escape)
    maints = list(_MAINT.finditer(expr))
    enrichs = list(_ENRICH.finditer(expr))
    if len(bares) != 1 or len(enrichs) != 1:
        return ("two-arm-other", hygiene or (
            f"unrecognized two-arm shape ({len(enrichs)} enrichment join(s), "
            f"{len(bares)} bare marker(s)) — extend check_maintenance_symmetry "
            "or restructure to the canonical single enriched/bare pair"))
    bare, enrich = bares[0], enrichs[0]

    if len(maints) == 0:
        # factored upstream (:core recording rule) or deliberate no-opt-out. A paired
        # VARIANT spelling cannot land here silently — hygiene above already failed it.
        return ("two-arm-0copy", hygiene)
    if len(maints) == 2:
        a, b = maints
        masked = _mask_quotes(expr)
        depth = _depths(masked)
        ordered = (a.end() <= enrich.start() and enrich.end() <= b.start()
                   and b.end() <= bare.start())
        # Arm membership (adversarial-review P2-1): linear ordering alone cannot tell
        # "second copy pasted after the join but still inside the ENRICHED arm (bare arm
        # naked)" from a true one-per-arm layout — the top-level `or` union must sit
        # BETWEEN the enrichment join and the second copy.
        union_between = any(depth[o.start()] == 0 and enrich.end() <= o.start() < b.start()
                            for o in _OR.finditer(masked))
        if ordered and union_between:
            return ("two-arm-2copy", hygiene)   # canonical: one copy per arm
        return ("two-arm-2copy", hygiene or (
            "2 maintenance clauses but NOT one-per-arm (expected: enriched-arm copy "
            "before the group_left join, bare-arm copy in the OTHER arm — past the "
            "top-level `or` union — before the bare `unless tenant_metadata_info`)"))
    if len(maints) == 1:
        m = maints[0]
        masked = _mask_quotes(expr)
        depth = _depths(masked)
        if m.start() < bare.end() or depth[m.start()] != 0:
            return ("two-arm-factored", hygiene or (
                "only 1 maintenance clause and it sits INSIDE one arm — the other arm "
                "has NO maintenance suppression (an onboarding/enriched tenant would "
                f"page during its maintenance window). Add the twin copy `{_CANONICAL}` "
                "to the other arm, or factor it out: ((enriched) or (bare)) unless ... "
                "(If the WHOLE expr is wrapped in an extra outer paren pair, strip it — "
                "the factored clause must sit at paren depth 0.)"))
        toplevel_or = [o for o in _OR.finditer(masked)
                       if depth[o.start()] == 0 and o.start() < m.start()]
        if toplevel_or:
            return ("two-arm-factored", hygiene or (
                "factored maintenance clause with a TOP-LEVEL `or` left of it — PromQL "
                "`or` binds looser than `unless`, so `A or B unless M` is "
                "`A or (B unless M)` and the enriched arm loses suppression. "
                "Parenthesise the union: ((enriched) or (bare)) unless on(tenant) (...)"))
        return ("two-arm-factored", hygiene)    # properly factored single clause
    return ("two-arm-other", hygiene or (
        f"{len(maints)} maintenance clauses in one alert expr (expected 0, 1 factored, "
        "or 2 one-per-arm)"))


def classify_all() -> tuple[dict[str, int], list[str], int]:
    """(classification counts, violations, scanned) over rule-packs/. Shared by check()
    and the distribution-floor test (adversarial-review P2-4: a classifier that stops
    RECOGNISING the two-arm shape must fail loudly, not print an unchanged \"OK — N
    scanned\")."""
    counts = {"two-arm-2copy": 0, "two-arm-factored": 0, "two-arm-0copy": 0,
              "two-arm-other": 0, "single-arm": 0}
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
                kind, reason = _check_expr(expr)
                counts[kind] += 1
                if reason:
                    violations.append(f"    {f.name}:{name} — {reason}")
    return counts, violations, scanned


def check() -> int:
    counts, violations, scanned = classify_all()
    if violations:
        print("check_maintenance_symmetry: per-arm maintenance suppression violation(s) "
              "(#947/#977 bare-arm class):")
        print("\n".join(violations))
        print(f"  canonical clause: {_CANONICAL}")
        return 1
    print(f"check_maintenance_symmetry: OK — {scanned} alert expr(s): "
          f"{counts['two-arm-2copy']} two-arm 2-copy, "
          f"{counts['two-arm-factored']} factored, "
          f"{counts['two-arm-0copy']} 0-copy (:core-style), "
          f"{counts['single-arm']} single-arm out-of-scope; "
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
