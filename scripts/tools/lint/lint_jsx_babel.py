#!/usr/bin/env python3
"""lint_jsx_babel.py — Validate JSX files parse correctly via Babel standalone.

Replicates the jsx-loader.html transform pipeline (front-matter strip,
ES import → global reference, export default → function) then runs THREE
validation passes:

  1. **Static pattern check** — catches ``style={{ }}`` and other patterns
     that Babel's programmatic API accepts but the browser script-tag mode
     (``Babel.transformScriptTags()``) silently breaks on.
  2. **Line-count guard** (issue #152) — flags files that have grown to
     the size where latent-bug archaeology becomes infeasible. PR #150
     uncovered three pre-existing latent bugs in tenant-manager.jsx
     (1671 lines) that would have been trivially spotted in 200-line
     modules. Soft cap warns at 1500, hard cap fails at 2500.
     # TODO(v2.9.0): revisit thresholds once we have a year of data.
  3. **Babel parse** — runs ``Babel.transform()`` via Node.js to catch
     syntax errors.

Requirements:
    - Node.js (>=16)
    - npm install @babel/standalone  (auto-installed if missing)

Usage:
    python3 scripts/tools/lint/lint_jsx_babel.py             # report mode
    python3 scripts/tools/lint/lint_jsx_babel.py --ci         # exit 1 on parse errors / hard-cap line-count
    python3 scripts/tools/lint/lint_jsx_babel.py --ci --strict # also fail on static + soft-cap warnings
    python3 scripts/tools/lint/lint_jsx_babel.py --fix        # hint-only (no auto-fix)

Severity split (added in docs/harness-hardening):
    - Babel parse errors → ALWAYS fatal under --ci (catches NUL bytes,
      broken syntax, the architecture-quiz.jsx regression)
    - Line-count hard cap (> 2500 lines) → ALWAYS fatal under --ci
      (#152 — files that big virtually guarantee latent bugs go undetected)
    - Static pattern warnings (style={{ }} etc.) → only fatal under --strict
    - Line-count soft cap (1500 < N ≤ 2500) → only fatal under --strict
    pre-commit stays on default so commits are not blocked by pre-existing
    drift; CI runs --strict to surface everything.

Exit codes:
    0 = all files pass
    1 = Babel parse failure / hard-cap line-count (always fatal under --ci),
        OR static / soft-cap warnings under --strict
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

JSX_DIRS = [
    PROJECT_ROOT / "docs" / "interactive" / "tools",
    PROJECT_ROOT / "docs" / "getting-started",
]

NODE_SCRIPT = r"""
const Babel = require('@babel/standalone');
const fs = require('fs');

// Read file list from stdin (JSON array of {path, source})
const input = fs.readFileSync(0, 'utf8');
const files = JSON.parse(input);
const results = [];

for (const f of files) {
  try {
    Babel.transform(f.source, { presets: ['react'], filename: f.path });
    results.push({ path: f.path, ok: true });
  } catch (e) {
    const msg = e.message.split('\n')[0];
    results.push({ path: f.path, ok: false, error: msg });
  }
}

process.stdout.write(JSON.stringify(results));
"""

# ---------------------------------------------------------------------------
# Static pattern checks — catch patterns that Babel.transform() accepts
# but Babel.transformScriptTags() (browser mode) silently breaks on.
# ---------------------------------------------------------------------------

# Matches style={{ ... }} — the double-curly pattern is mishandled by
# browser-mode Babel (the value is stripped entirely).
_RE_STYLE_DOUBLE_CURLY = re.compile(r'style\s*=\s*\{\{')

# Matches self-closing HTML elements (div, span, p, etc.) that are not
# valid self-closing in some Babel parsers.  <div ... /> is fine in React
# but can confuse older Babel standalone + script-tag mode.
_RE_SELF_CLOSING_HTML = re.compile(
    r'<(div|span|p|a|section|main|header|footer|article|aside|nav|ul|ol|li|label|button|h[1-6])\b[^>]*/\s*>'
)

_STATIC_CHECKS = [
    (
        _RE_STYLE_DOUBLE_CURLY,
        "style={{ }} double-curly pattern breaks browser Babel — "
        "extract style object to a variable: const s = { ... }; style={s}",
    ),
]


def _run_static_checks(filepath: str, source: str) -> list[dict]:
    """Return list of {path, line, error} for each static pattern match."""
    issues = []
    lines = source.split("\n")
    for lineno, line in enumerate(lines, 1):
        for pattern, msg in _STATIC_CHECKS:
            if pattern.search(line):
                issues.append({
                    "path": filepath,
                    "line": lineno,
                    "error": f"(static) {msg}",
                    "snippet": line.strip()[:120],
                })
    return issues


# ---------------------------------------------------------------------------
# Line-count guard (issue #152) — files larger than these thresholds become
# infeasible to maintain because latent bugs hide in them. PR #150 paid the
# tuition: tenant-manager.jsx (1671 lines) accumulated 3 latent bugs over
# months that all surfaced together once mocked-API e2e tests let `loading`
# flip from true → false fast (hook-count mismatch / missing useState /
# loading-state never cleared on success path).
# ---------------------------------------------------------------------------

# Soft cap: the size at which decomposition starts paying off. Picked from
# observing where tenant-manager.jsx became hard to audit. Most of the 39
# interactive JSX tools sit between 200 and 1200 lines today; 1500 is the
# top-decile signal.
LINE_COUNT_WARN = 1500

# Hard cap: tenant-manager.jsx at 1671 already had 3 latent bugs; 2500 gives
# ~67% headroom over today's worst offender so it doesn't insta-fail current
# reality, but blocks the next such offender from landing.
LINE_COUNT_FAIL = 2500


def _run_line_count_check(filepath: str, source: str) -> list[dict]:
    """Return at most one issue describing a line-count threshold breach.

    Severity is tagged into the error message so the main reporter can
    distinguish hard-cap (always fatal under --ci) from soft-cap (fatal
    only under --strict).
    """
    # Count lines the same way `wc -l` does (newlines), but +1 if the file
    # doesn't end with one. This matches user intuition ("1691 lines") and
    # `wc -l` output that the issue body cites.
    line_count = source.count("\n")
    if source and not source.endswith("\n"):
        line_count += 1

    if line_count > LINE_COUNT_FAIL:
        return [{
            "path": filepath,
            "line": 1,
            "severity": "hard",
            "error": (
                f"(line-count/hard) {line_count} lines exceeds hard cap of "
                f"{LINE_COUNT_FAIL} — split into a directory of focused "
                f"modules (see PR-2d / issue #153 for the tenant-manager.jsx "
                f"decomposition pattern)"
            ),
            "snippet": "",
        }]
    if line_count > LINE_COUNT_WARN:
        return [{
            "path": filepath,
            "line": 1,
            "severity": "soft",
            "error": (
                f"(line-count/soft) {line_count} lines exceeds soft cap of "
                f"{LINE_COUNT_WARN} — consider extracting hooks/views before "
                f"the file crosses {LINE_COUNT_FAIL}"
            ),
            "snippet": "",
        }]
    return []


def _transform_jsx(source: str) -> str:
    """Replicate jsx-loader.html renderJSX() transform pipeline."""

    # 1) Strip YAML front matter
    source = re.sub(r"^---[\s\S]*?---\s*\n?", "", source)

    # 2a) React imports: import React, { useState, ... } from 'react'
    source = re.sub(
        r"^import\s+React\s*,?\s*\{([^}]*)\}\s*from\s*['\"]react['\"];?\s*$",
        lambda m: "const { "
        + ", ".join(n.strip() for n in m.group(1).split(",") if n.strip())
        + " } = React;",
        source,
        flags=re.MULTILINE,
    )
    # 2b) import { ... } from 'react'
    source = re.sub(
        r"^import\s+\{([^}]*)\}\s*from\s*['\"]react['\"];?\s*$",
        lambda m: "const { "
        + ", ".join(n.strip() for n in m.group(1).split(",") if n.strip())
        + " } = React;",
        source,
        flags=re.MULTILINE,
    )
    # 2c) import React from 'react'
    source = re.sub(
        r"^import\s+React\s+from\s*['\"]react['\"];?\s*$",
        "",
        source,
        flags=re.MULTILINE,
    )

    # 3) lucide-react: stub icon components
    source = re.sub(
        r"^import\s+\{([^}]*)\}\s*from\s*['\"]lucide-react['\"];?\s*$",
        lambda m: "\n".join(
            f"const {n.strip()} = () => null;"
            for n in m.group(1).split(",")
            if n.strip()
        ),
        source,
        flags=re.MULTILINE,
    )

    # 4) export default function Name → function Name
    source = re.sub(
        r"^export\s+default\s+function\s+(\w+)",
        r"function \1",
        source,
        flags=re.MULTILINE,
    )
    # export default Name;
    source = re.sub(
        r"^export\s+default\s+(\w+)\s*;?\s*$",
        "",
        source,
        flags=re.MULTILINE,
    )

    return source


def _ensure_babel(node_modules: Path) -> bool:
    """Install @babel/standalone if not already present."""
    babel_dir = node_modules / "@babel" / "standalone"
    if babel_dir.exists():
        return True
    try:
        subprocess.run(
            ["npm", "install", "--prefix", str(node_modules.parent), "@babel/standalone@7.26.4"],
            capture_output=True,
            timeout=60,
        )
        return babel_dir.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> int:
    """CLI entry point: Validate JSX files parse correctly via Babel standalone."""
    parser = argparse.ArgumentParser(description="Lint JSX files with Babel standalone")
    parser.add_argument("--ci", action="store_true", help="Exit 1 on Babel parse errors")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail on static pattern warnings (style={{ }} etc.)",
    )
    args = parser.parse_args()

    # Check Node.js
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("⚠ Node.js not found — skipping JSX Babel lint")
        return 0

    # Ensure @babel/standalone is installed
    babel_prefix = Path(tempfile.gettempdir()) / "da-babel-lint"
    babel_prefix.mkdir(exist_ok=True)
    node_modules = babel_prefix / "node_modules"
    if not _ensure_babel(node_modules):
        print("⚠ Could not install @babel/standalone — skipping")
        return 0

    # Collect JSX files
    files = []
    static_failures = []
    linecount_hard_failures = []
    linecount_soft_failures = []
    for d in JSX_DIRS:
        if not d.exists():
            continue
        for jsx in sorted(d.glob("*.jsx")):
            source = jsx.read_text(encoding="utf-8")
            rel_path = str(jsx.relative_to(PROJECT_ROOT))
            # Pass 1: static pattern checks (on original source, before transform)
            static_failures.extend(_run_static_checks(rel_path, source))
            # Pass 2: line-count guard (issue #152)
            for issue in _run_line_count_check(rel_path, source):
                if issue["severity"] == "hard":
                    linecount_hard_failures.append(issue)
                else:
                    linecount_soft_failures.append(issue)
            transformed = _transform_jsx(source)
            files.append({"path": rel_path, "source": transformed})

    if not files:
        print("✓ No JSX files found")
        return 0

    # Pass 2: Babel parse via Node.js
    env = os.environ.copy()
    env["NODE_PATH"] = str(node_modules)
    result = subprocess.run(
        ["node", "-e", NODE_SCRIPT],
        input=json.dumps(files),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    babel_failures = []
    if result.returncode != 0:
        print(f"⚠ Node.js error: {result.stderr[:200]}")
    else:
        try:
            results = json.loads(result.stdout)
            babel_failures = [r for r in results if not r["ok"]]
        except json.JSONDecodeError:
            print("⚠ Could not parse Node.js output")

    # Combine results
    all_failures = (
        static_failures
        + babel_failures
        + linecount_hard_failures
        + linecount_soft_failures
    )
    total_files = len(files)
    unique_failing = set()
    for f in all_failures:
        unique_failing.add(f["path"])

    if not all_failures:
        print(
            f"✓ All {total_files} JSX files pass "
            f"(Babel parse + static checks + line-count)"
        )
        return 0

    # Report static pattern issues
    if static_failures:
        print(f"✗ {len(static_failures)} browser-incompatible pattern(s) found:\n")
        for f in static_failures:
            print(f"  {f['path']}:{f['line']}: {f['error']}")
            print(f"    → {f['snippet']}")
        print()

    # Report line-count hard-cap (always fatal under --ci)
    if linecount_hard_failures:
        print(
            f"✗ {len(linecount_hard_failures)} JSX file(s) exceed hard line-count cap "
            f"({LINE_COUNT_FAIL}):\n"
        )
        for f in linecount_hard_failures:
            print(f"  {f['path']}: {f['error']}")
        print()

    # Report line-count soft-cap (warn unless --strict)
    if linecount_soft_failures:
        print(
            f"⚠ {len(linecount_soft_failures)} JSX file(s) exceed soft line-count cap "
            f"({LINE_COUNT_WARN}):\n"
        )
        for f in linecount_soft_failures:
            print(f"  {f['path']}: {f['error']}")
        print()

    # Report Babel parse failures
    if babel_failures:
        print(f"✗ {len(babel_failures)} JSX file(s) failed Babel parse:\n")
        for f in babel_failures:
            print(f"  {f['path']}: {f['error']}")
        print()

    passed = total_files - len(unique_failing)
    print(f"Summary: {passed}/{total_files} files OK, "
          f"{len(unique_failing)} file(s) have issues")

    # Severity computation:
    #   - Babel parse + line-count hard cap → ALWAYS fatal under --ci
    #   - Static pattern + line-count soft cap → fatal only under --strict
    soft_warnings = bool(static_failures) or bool(linecount_soft_failures)
    fatal = (
        bool(babel_failures)
        or bool(linecount_hard_failures)
        or (args.strict and soft_warnings)
    )
    if args.ci and fatal:
        return 1
    if args.ci and soft_warnings and not args.strict:
        soft_total = len(static_failures) + len(linecount_soft_failures)
        print(
            f"\nNote: {soft_total} soft warning(s) (static + line-count) — "
            f"use --strict to fail on these."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
