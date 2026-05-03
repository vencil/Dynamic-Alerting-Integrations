#!/usr/bin/env python3
"""check_hub_badge_drift.py — detect hardcoded tool counts in the Hub UI (PR-portal-7).

Why this exists
---------------
Pre-PR-portal-7, `docs/interactive/index.html` had per-phase Hub badge
counts hardcoded in two places: the i18n string table (`*-badge: '8
Tools'`) and the static HTML body (`<span id="reference-badge">2
Tools</span>`). These drifted from the tool-registry.yaml ground truth
over many releases — the user spotted it as "Reference 2 Tools" badge
above 3 actual cards. PR-portal-7 replaced the literals with `{N}`
placeholders that get substituted at render time from the live
phase-count cache.

This lint locks that change in. Any future edit that re-introduces a
hardcoded `\\d+ Tools` (EN) or `\\d+ 個工具` (ZH) on a `*-badge` or
`hero-desc` line is flagged.

What it flags
-------------
On `docs/interactive/index.html`:

1. **Hardcoded `\\d+ Tools` next to a `*-badge` id or in a *-badge
   i18n value** — the badge text must use the `{N}` placeholder.
2. **Hardcoded `\\d+ Tools` in the `hero-desc` i18n value** — the
   total tool count must come from the live registry length.
3. **Hardcoded `\\d+ 個工具`** equivalents (ZH side).

What it does NOT flag
---------------------
- Counts in comments / explanatory text
- Counts in unrelated i18n strings (only `*-badge` + `hero-desc`)
- The literal `{N}` placeholder itself

Allowed exemption: per-line `<!-- hub-badge-drift: ignore -->` (3-line
lookback, mirrors the convention in check_undefined_tokens.py).

Usage
-----
  python3 scripts/tools/lint/check_hub_badge_drift.py

Exit codes:
  0 = no hardcoded counts found
  1 = at least one violation
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HUB_HTML = REPO_ROOT / "docs" / "interactive" / "index.html"

# A hardcoded count adjacent to a `*-badge` id/key OR inside hero-desc.
# Patterns intentionally narrow — only catch the actual drift surface.
_RE_HARDCODED_BADGE_HTML = re.compile(
    r'id="[a-z]+-badge"[^>]*>\s*\d+\s+(Tools|個工具)'
)
_RE_HARDCODED_BADGE_I18N = re.compile(
    r"'[a-z]+-badge'\s*:\s*['\"]\d+\s+(Tools|個工具)"
)
_RE_HARDCODED_HERO_I18N = re.compile(
    r"'hero-desc'\s*:\s*['\"][^'\"]*?\b\d+\s+(tools|個[^'\"]*工具)"
)

_IGNORE_MARKER = "<!-- hub-badge-drift: ignore -->"
_IGNORE_LOOKBACK_LINES = 3


def _line_is_ignored(lines: list[str], idx: int) -> bool:
    """True if any of the previous N lines carry the ignore marker."""
    start = max(0, idx - _IGNORE_LOOKBACK_LINES)
    return any(_IGNORE_MARKER in lines[i] for i in range(start, idx))


def _display_path(path: Path) -> str:
    """Render path relative to REPO_ROOT when possible, else absolute.

    Tests scan synthetic files outside the repo (tmp_path); production
    runs scan REPO_ROOT/docs/interactive/index.html. Both must format
    cleanly without crashing.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def scan_hub(path: Path = HUB_HTML) -> list[str]:
    """Return list of violation messages (empty if clean)."""
    if not path.exists():
        return [f"ERROR: {path} not found"]

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    violations: list[str] = []
    display = _display_path(path)

    for i, line in enumerate(lines):
        line_no = i + 1
        if _line_is_ignored(lines, i):
            continue

        for rx, label in (
            (_RE_HARDCODED_BADGE_HTML, "static HTML *-badge"),
            (_RE_HARDCODED_BADGE_I18N, "i18n *-badge value"),
            (_RE_HARDCODED_HERO_I18N, "i18n hero-desc tool count"),
        ):
            if rx.search(line):
                violations.append(
                    f"{display}:{line_no} hardcoded "
                    f"count in {label} — use {{N}} placeholder; counts "
                    f"are populated by renderTools() at fetch-time."
                )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect hardcoded tool counts in the Hub UI "
            "(docs/interactive/index.html). Counts must use the {N} "
            "placeholder substituted by renderTools() at fetch-time."
        ),
    )
    parser.parse_args()

    violations = scan_hub()
    if not violations:
        print(f"OK: {_display_path(HUB_HTML)} has no hardcoded badge counts.")
        return 0

    print(f"FAIL: {len(violations)} hardcoded badge count(s):", file=sys.stderr)
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
    print(
        "\nFix: replace the literal `N Tools` / `N 個工具` with `{N} Tools` "
        "/ `{N} 個工具`. The Hub i18n applier substitutes from the "
        "window.__hubPhaseCounts cache populated by renderTools().",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
