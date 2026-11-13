#!/usr/bin/env python3
"""check_jsx_linecount.py — Flag portal JSX files grown too large to audit (issue #152).

PR #150 uncovered three latent bugs in tenant-manager.jsx (1671 lines) that
would have been trivially spotted in 200-line modules. Soft cap warns above
1500 lines, hard cap fails above 2500. Pure Python: no node, so a missing
toolchain can never cost this check its verdict (#3669; it was a pass of
``lint_jsx_babel.py`` and went down with the Babel pass whenever node did).

Usage:
    python3 scripts/tools/lint/check_jsx_linecount.py              # report: exit 0
    python3 scripts/tools/lint/check_jsx_linecount.py --ci         # exit 1 on a hard-cap file
    python3 scripts/tools/lint/check_jsx_linecount.py --ci --strict  # also on a soft-cap file

Exit codes:
    0 = no file over a fatal cap (findings without --ci are printed, exit 0)
    1 = --ci and a file over the hard cap, or --ci --strict and one over the soft cap
    2 = no verdict: a scanned directory is missing, or it holds no .jsx file
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

JSX_DIRS = [
    PROJECT_ROOT / "tools" / "portal" / "src" / "interactive" / "tools",
    PROJECT_ROOT / "tools" / "portal" / "src" / "getting-started",
]

# Soft cap: the size at which decomposition starts paying off. Picked from
# observing where tenant-manager.jsx became hard to audit — the file whose
# three latent bugs PR #150 paid for.
# ⚠️ The file-size distribution that justified this number is deliberately NOT
# restated here. The constants themselves are pinned by TestThresholdConstants
# (#152), but no assertion relates them to the sizes of the files actually
# scanned, so a snapshot written here would go stale with nothing to notice.
# Re-derive it from the scanned set when the caps come up for review.
LINE_COUNT_WARN = 1500

# Hard cap: headroom over the soft cap so it does not insta-fail current
# reality, while still blocking the next file that grows the way
# tenant-manager.jsx did before it was decomposed.
LINE_COUNT_FAIL = 2500


def _run_line_count_check(filepath: str, source: str) -> list[dict]:
    """At most one issue describing a line-count threshold breach, tagged
    ``severity`` ``hard`` (fatal under --ci) or ``soft`` (fatal under --strict)."""
    # Count lines the way `wc -l` does (newlines), +1 when the file does not
    # end with one — what a person means by "1691 lines".
    line_count = source.count("\n")
    if source and not source.endswith("\n"):
        line_count += 1
    if line_count > LINE_COUNT_FAIL:
        return [{
            "path": filepath,
            "severity": "hard",
            "error": (
                f"(line-count/hard) {line_count} lines exceeds hard cap of "
                f"{LINE_COUNT_FAIL} — split into a directory of focused "
                f"modules (see PR-2d / issue #153 for the tenant-manager.jsx "
                f"decomposition pattern)"
            ),
        }]
    if line_count > LINE_COUNT_WARN:
        return [{
            "path": filepath,
            "severity": "soft",
            "error": (
                f"(line-count/soft) {line_count} lines exceeds soft cap of "
                f"{LINE_COUNT_WARN} — consider extracting hooks/views before "
                f"the file crosses {LINE_COUNT_FAIL}"
            ),
        }]
    return []


def _compute_exit_code(*, ci: bool, strict: bool, hard: list, soft: list) -> int:
    """Without --ci always 0 (report only); with --ci a hard-cap file is
    fatal, and with --strict a soft-cap file too."""
    if not ci:
        return 0
    return 1 if hard or (strict and soft) else 0


def main() -> int:
    """CLI entry point: flag portal JSX files grown too large to audit."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description="Line-count caps for the portal's JSX files")
    parser.add_argument("--ci", action="store_true", help="Exit 1 on a file over the hard cap")
    parser.add_argument("--strict", action="store_true",
                        help=f"With --ci, also exit 1 on a file over the soft cap "
                             f"({LINE_COUNT_WARN} < N <= {LINE_COUNT_FAIL})")
    parser.add_argument("dirs", nargs="*", type=Path, metavar="DIR",
                        help="directories to scan (default: the portal's tool and "
                             "getting-started trees)")
    args = parser.parse_args()

    import _lib_input
    import _lib_paths
    dirs = args.dirs or JSX_DIRS
    _lib_paths.raise_path_problems(
        [_lib_paths.classify_path(d, "dir", lang="en") for d in dirs])

    hard: list[dict] = []
    soft: list[dict] = []
    total = 0
    for d in dirs:
        # rglob: PR-2d-style decomposed directories (tenant-manager/components/*.jsx), #153.
        for jsx in sorted(d.rglob("*.jsx")):
            total += 1
            try:
                label = str(jsx.relative_to(PROJECT_ROOT))
            except ValueError:
                label = str(jsx)
            source = jsx.read_text(encoding="utf-8", errors="replace")
            for issue in _run_line_count_check(label, source):
                (hard if issue["severity"] == "hard" else soft).append(issue)
    if total == 0:
        raise _lib_input.refuse_empty(", ".join(str(d) for d in dirs), "a .jsx file", lang="en")

    if hard:
        print(f"✗ {len(hard)} JSX file(s) exceed the hard line-count cap ({LINE_COUNT_FAIL}):\n")
        for f in hard:
            print(f"  {f['path']}: {f['error']}")
        print()
    if soft:
        print(f"⚠ {len(soft)} JSX file(s) exceed the soft line-count cap ({LINE_COUNT_WARN}):\n")
        for f in soft:
            print(f"  {f['path']}: {f['error']}")
        print()
    if not hard and not soft:
        print(f"✓ All {total} JSX files are within the line-count caps")
    elif args.ci and soft and not args.strict:
        print(f"Note: {len(soft)} soft-cap warning(s) — use --strict to fail on these.")
    return _compute_exit_code(ci=args.ci, strict=args.strict, hard=hard, soft=soft)


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
