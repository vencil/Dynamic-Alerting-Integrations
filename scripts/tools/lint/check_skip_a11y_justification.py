#!/usr/bin/env python3
r"""check_skip_a11y_justification.py — Require ticket-justification for `skipA11y: true` in E2E specs (testing-playbook §LL §5, TD-039).

Why this exists
---------------
TD-035 (PR #272) audited 25 portal-tool specs that were marked
`skipA11y: true` to ship PR-C/D/E in scope. The audit found:

  - 13 / 25 specs were already a11y-clean — `skipA11y: true` was
    pure debt, no real reason to skip
  - 12 / 25 had real critical violations that ought to be fixed
    at source

testing-playbook.md §LL "v2.8.0 — Portal E2E coverage push + ESM
dist regression" rule §5 codifies the lesson: "axe critical=0 is the
right gate; non-critical uses budget=N, not skipA11y." Direct
`skipA11y: true` should be the exception, not the default.

This hook codifies that rule. Direct `skipA11y: true` is allowed
ONLY when accompanied by a justification comment within the 3 lines
preceding the config line. The justification must reference a TD ticket
(e.g. `// skipA11y: TD-040 third-party widget pulls in non-AA color`)
so the debt is tracked and discoverable.

Detection rule
--------------
Scan `tests/e2e/*.spec.ts` for `skipA11y:\s*true`. For each match:

- Look at the 3 lines preceding the match (inclusive of the match line
  itself).
- If any of those lines contains a comment starting with `//` and
  matches `skipA11y:.*TD-\d+`, the use is justified.
- Otherwise it's a "silent skip" — flag.

Allowed (deliberately NOT flagged):
- `// skipA11y: TD-040 reason here` within 3 lines preceding
- `allowedNonCriticalViolations: N` — the budget pattern, not a skip
- `skipA11y: false` — the default; explicit-no-skip is fine

Severity model
--------------
Auto-stage FATAL on findings. Codebase audit at scaffold time: ✓ 0
violations (TD-035 cleared all 25 cases — none use `skipA11y: true`
any more; the budget=5 pattern was adopted instead).

Usage
-----
    pre-commit run skip-a11y-justification-check --all-files
    python3 scripts/tools/lint/check_skip_a11y_justification.py --ci
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# `skipA11y: true` — the call site we're looking for. Allow flexible
# whitespace and trailing comma.
RE_SKIP_TRUE = re.compile(r"\bskipA11y\s*:\s*true\b")

# A justification comment: `// skipA11y: TRK-NNN <free text>` (or the legacy
# `TD-NNN` form, accepted during the v2.8.1 namespace transition window) within
# the preceding lines. The ticket number is required so the debt is tracked.
# Letter suffix on TRK ids (e.g. TRK-230c) is permitted; legacy TD-NN has none.
RE_JUSTIFICATION = re.compile(r"//\s*skipA11y:\s*(?:TRK-\d+[a-z]?|TD-\d+)\b")

LOOKBACK_LINES = 3


def find_violations(text: str) -> List[Tuple[int, str]]:
    """Return list of (line_no_1indexed, snippet) for unjustified skipA11y.

    Lines inside `/* ... */` block comments and `//` line comments are
    skipped — they're not real config sites. We track block-comment state
    line-by-line to handle JSDoc-style multi-line comments at the top
    of spec files, which often mention `skipA11y: true` in plain prose.
    """
    violations: List[Tuple[int, str]] = []
    lines = text.split("\n")

    in_block_comment = False
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        # Track block comment state. Naive tracker — fine for the kind
        # of files we scan (TS specs with conventional JSDoc / inline
        # comments; no string-literal comment markers in test code).
        if in_block_comment:
            # Still inside; does this line end the block?
            if "*/" in line:
                in_block_comment = False
            continue
        # Not in block comment — does it open here?
        if stripped.startswith("/*"):
            # Check whether it ALSO closes on the same line.
            after_open = stripped[2:]
            if "*/" not in after_open:
                in_block_comment = True
            continue
        # Line-level comment — skip.
        if stripped.startswith("//"):
            continue

        # Real code line. Now check for the pattern.
        if not RE_SKIP_TRUE.search(line):
            continue
        # Justification window: this line + LOOKBACK_LINES preceding.
        start = max(0, idx - LOOKBACK_LINES)
        window = "\n".join(lines[start : idx + 1])
        if RE_JUSTIFICATION.search(window):
            continue
        violations.append((idx + 1, line.strip()))

    return violations


def scan(paths: List[Path]) -> List[Tuple[Path, int, str]]:
    findings: List[Tuple[Path, int, str]] = []
    for p in paths:
        if not p.is_file():
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            display = p.relative_to(REPO_ROOT)
        except ValueError:
            display = p
        for line_no, snippet in find_violations(txt):
            findings.append((display, line_no, snippet))
    return findings


def collect_default_paths() -> List[Path]:
    e2e_dir = REPO_ROOT / "tests" / "e2e"
    if not e2e_dir.is_dir():
        return []
    # Only spec files; fixtures don't have describe blocks.
    return list(e2e_dir.glob("*.spec.ts"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Require TD-ticket justification for skipA11y: true (testing-playbook §LL §5)."
    )
    parser.add_argument("--ci", action="store_true", help="exit 1 on findings")
    parser.add_argument(
        "paths",
        nargs="*",
        help="optional explicit file paths (else defaults to tests/e2e/*.spec.ts)",
    )
    args = parser.parse_args()

    if args.paths:
        paths = [Path(p).resolve() for p in args.paths]
    else:
        paths = collect_default_paths()

    findings = scan(paths)

    if not findings:
        print(f"skip-a11y-justification: ✓ {len(paths)} specs clean (testing-playbook §LL §5)")
        return 0

    print(f"skip-a11y-justification: ✗ {len(findings)} unjustified `skipA11y: true` use(s)")
    print()
    for path, line_no, snippet in findings:
        print(f"  {path}:{line_no}: {snippet}")
    print()
    print("Either:")
    print("  (a) Fix the a11y issue at source and remove `skipA11y: true`")
    print("      (preferred — see TD-035 / PR #272 for the audit pattern)")
    print()
    print("  (b) Replace with a budget for non-critical violations:")
    print("      allowedNonCriticalViolations: 5  // matches alert-builder etc.")
    print()
    print("  (c) Keep `skipA11y: true` ONLY with a tracked-debt justification")
    print("      comment within the 3 lines preceding the config line:")
    print("      // skipA11y: TD-040 third-party widget pulls non-AA contrast")
    print("      Use option (c) only when source fix needs cross-team coordination.")
    print()
    print("Background: testing-playbook.md §LL v2.8.0 §5 (TD-035 audit retrospective).")

    if args.ci:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
