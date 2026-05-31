#!/usr/bin/env python3
"""check_lint_toolchain_fit.py — meta-lint: stop reinventing ESLint/stylelint.

Why this exists (#444 follow-up, user directive: "盡量自動化的偵測")
------------------------------------------------------------------
The repo accumulated ~10 DIY Python lints that parse JS/JSX/CSS *content*
(hardcoded hex, i18n strings, module syntax, …). Per lint-policy.md §7, a lint
whose TARGET is JS/JSX/TSX/CSS should default to an ESLint/stylelint rule —
those engines already have a parser, autofix, editor integration, and a rule
ecosystem. A hand-rolled regex scanner is a reinvented wheel: more code, no
autofix, brittle parsing.

A checklist in a doc is not enforcement — authors forget. This meta-lint makes
the decision gate *automated*: it scans the lint directory and FAILS when a
lint file targets JS-toolchain-reachable content unless that file is in the
grandfather ALLOWLIST below (each entry carries a one-line justification).

To add a NEW lint that parses JS/JSX/CSS you must either:
  (a) implement it as an ESLint/stylelint rule instead (preferred), OR
  (b) add it to ALLOWLIST with a reason ESLint/stylelint genuinely can't cover
      (e.g. cross-file registry parity, bilingual semantics, diff-only + PR
      bypass plumbing that off-the-shelf engines don't model).

Lint class (lint-policy.md §2): (b) convention — fail = policy violation.
Scan scope: full-scan of scripts/tools/lint/ (the population is tiny + the
signal is "a new file appeared", so diff-only adds no value here).

Usage:
    python3 scripts/tools/lint/check_lint_toolchain_fit.py [--ci]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LINT_DIR = REPO_ROOT / "scripts" / "tools" / "lint"

# A lint "targets JS-toolchain content" — the ESLint/stylelint-shaped signal —
# if it GLOBS a directory for JS/JSX/CSS files (i.e. walks the source tree to
# scan their content). Deliberately NARROW to avoid the meta-lint's own
# false-positive trap: we do NOT match bare filename mentions, `.ts`/`.js`
# (too ubiquitous in tool strings), or version/orchestration utilities that
# merely reference a path. Only directory-content scanning counts.
_TARGET_EXT_RE = re.compile(
    r"""(?x)
    (?:glob|rglob)\s*\(\s*          # .glob( / .rglob(
    (?:f?['"])                      # opening quote (optionally f-string)
    [^'"]*\*?\.(?:jsx|tsx|css|scss) # a glob ending in *.jsx / *.tsx / *.css / *.scss
    """
)

# Grandfathered DIY lints that parse JS/JSX/CSS content. Each MUST carry a
# reason. Migratable=YES → tracked in lint-policy.md §8 quarterly list for
# eventual natural replacement; Migratable=NO → genuinely needs Python.
ALLOWLIST: dict[str, str] = {
    # --- genuinely Python (registry / filesystem / toolchain) — Migratable=NO
    "check_tool_registry_jsx_parity.py":
        "registry↔filesystem parity (YAML SSOT vs .jsx existence), not a JS lint",
    "check_portal_i18n.py":
        "cross-references tool-registry.yaml; not pure single-file JS linting",
    "check_jsx_loader_compat.py":
        "validates against the custom JSX loader allowlist + babel; toolchain-coupled",
    "lint_jsx_babel.py":
        "already invokes @babel/node to validate parse-ability; IS the toolchain",
    "lint_tool_consistency.py":
        "cross-checks tool-registry.yaml ↔ JSX ↔ markdown; registry-graph lint, not single-file JS",
    "validate_docs_versions.py":
        "version-literal consistency across many file types incl .jsx; version governance, not a JS rule",
    # --- migratable to ESLint someday (lint-policy.md §8) — Migratable=YES
    "check_design_token_usage.py":
        "Migratable=YES(§8): style={{}} hex/px; kept DIY for diff-only+bypass+"
        "bilingual (ESLint <60% coverage today; revisit if FE adopts Tailwind)",
    "check_jsx_i18n.py":
        "Migratable=YES(§8): CJK hardcoded-string detection in JSX",
    "check_window_x_no_fallback.py":
        "Migratable=YES(§8): module-scope `const X = window.__X` AST pattern",
    "check_undefined_tokens.py":
        "Migratable=YES(§8): --da-* token refs not defined in design-tokens.css",
    "check_i18n_coverage.py":
        "Migratable=YES(§8): i18n key coverage across JSX",
}

# This meta-lint itself contains the extension regex above → would self-flag.
_SELF = "check_lint_toolchain_fit.py"


def targets_js_toolchain(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # Ignore matches that appear only inside the ALLOWLIST/comment of THIS file.
    return bool(_TARGET_EXT_RE.search(text))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Meta-lint: flag new DIY lints that should be ESLint/stylelint rules."
    )
    parser.add_argument("--ci", action="store_true",
                        help="exit 1 on violation (default also exits 1; flag kept for parity)")
    parser.add_argument("--list", action="store_true",
                        help="print every lint file the detector matches, then exit 0")
    # argv defaults to [] (not sys.argv) so importers/tests calling main() are
    # not handed pytest's own argv → argparse SystemExit(2).
    args = parser.parse_args([] if argv is None else argv)

    if not LINT_DIR.is_dir():
        print(f"ERROR: lint dir not found: {LINT_DIR}", file=sys.stderr)
        return 1

    if args.list:
        for py in sorted(LINT_DIR.glob("*.py")):
            if py.name == _SELF or py.name.startswith("_"):
                continue
            if targets_js_toolchain(py):
                tag = "allowlisted" if py.name in ALLOWLIST else "UNJUSTIFIED"
                print(f"  [{tag}] {py.name}")
        return 0

    offenders: list[str] = []
    for py in sorted(LINT_DIR.glob("*.py")):
        name = py.name
        if name == _SELF or name.startswith("_"):
            continue
        if not targets_js_toolchain(py):
            continue
        if name not in ALLOWLIST:
            offenders.append(name)

    # Also surface allowlist entries whose file vanished (keeps list honest).
    stale = [n for n in ALLOWLIST if not (LINT_DIR / n).exists()]

    if not offenders and not stale:
        print(
            f"OK: {len(ALLOWLIST)} grandfathered JS-targeting lints; "
            f"no new un-justified reinvented-wheel lints."
        )
        return 0

    if offenders:
        print("FAIL: new lint(s) parse JS/JSX/CSS content but are not justified:")
        for o in offenders:
            print(f"  - {o}")
        print(
            "\nPer lint-policy.md §7: a lint whose TARGET is JS/JSX/TSX/CSS should\n"
            "default to an ESLint/stylelint rule (parser + autofix + editor support\n"
            "already exist). Either implement it there, OR add it to ALLOWLIST in\n"
            "scripts/tools/lint/check_lint_toolchain_fit.py with a one-line reason\n"
            "ESLint/stylelint genuinely cannot cover."
        )
    if stale:
        print("\nFAIL: ALLOWLIST entries point at missing files (remove them):")
        for s in stale:
            print(f"  - {s}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
