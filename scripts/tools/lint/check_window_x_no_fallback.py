#!/usr/bin/env python3
"""check_window_x_no_fallback.py — Forbid module-scope `const X = window.__X;` no-fallback reads (dev-rules.md §S6).

Why this exists
---------------
TRK-233 (PR #270) traced a 20-spec regression in main back to module-
scope `const X = window.__X;` reads (no fallback) in JSX components.
PR-E (#269) rebuilt the portal dist for an unrelated template-gallery
fix; esbuild's chunk-allocation heuristic reshuffled which chunks
contained which side-effect global writes vs reads. After reshuffle,
some consumer chunks evaluated BEFORE the chunks that wrote `window.__X`,
leaving the consumer's `const X = window.__X;` reading `undefined`.
Render aborted, no testid surfaced, smoke specs hit 30s timeout.

The same pattern was used for React hooks: `const { useState } = React;`
relied on the (now-retired) `define: { React: 'globalThis.__bundledReact' }`
build hack + a side-effect entry import that set the global. Same race,
same root cause.

dev-rules.md §S6 codifies the durable form: ESM imports for everything,
no module-scope no-fallback global reads. This lint mechanically
enforces §S6 so future contributors can't silently regress.

What it flags
-------------
On any `.jsx` / `.js` file under `docs/interactive/` and
`docs/getting-started/`:

1. **`const X = window.__Y;`** — module-scope no-fallback read of a
   `__`-prefixed window global.
2. **`const X = globalThis.__Y;`** — same shape, globalThis variant.
3. **`const { useState, ... } = React;`** — destructure of bare `React`
   identifier (relies on the retired `define`).
4. **`const { a, b } = window.__Y;`** — destructure (incl. multi-line)
   of a `__`-prefixed window global. This form crashed the whole
   self-service-portal bundle at load time: the three Tab modules
   destructured `window.__portalShared` at module scope but nothing in
   the bundle graph imported the producer module, so the read threw
   TypeError on `undefined` during module evaluation. The original
   single-identifier regex missed it.
   (Flat destructures only — nested `{ a: { b } }` is not matched.)

Allowed (deliberately NOT flagged):
- `const t = window.__t || ((zh, en) => en);` — fallback form gives
  deterministic behavior when the global is undefined.
- `import { useState } from 'react';` — proper ESM import.
- Per-line `<!-- window-x-no-fallback: ignore -->` (3-line lookback).
- File-level frontmatter `--- ... ---` (skipped via the same regex that
  jsx-loader-compat uses).

Output / severity
-----------------
Auto-stage FATAL on any finding. Per-line escape if there's a real
reason (e.g. illustrative example in a comment); none expected in
practice after TRK-233 / TRK-234.

Usage
-----
    pre-commit run window-x-no-fallback --all-files
    python3 scripts/tools/lint/check_window_x_no_fallback.py --ci
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple
import os

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Module-scope reads only (line MUST start at column 0 — no leading whitespace).
# Function-scope `const X = window.__X;` is fine: it runs when the function
# is called (post-init), not at module-load time, so no chunk-order race.
#
# Pattern A: `const X = window.__Y;` or `const X = globalThis.__Y;`
RE_GLOBAL_READ = re.compile(
    r"^const\s+\w+\s*=\s*(?:window|globalThis)\.__\w+\s*;",
    re.MULTILINE,
)

# Pattern B: `const { hook1, hook2 } = React;` at column 0.
# Bare React destructure relies on the retired define hack.
RE_REACT_DESTRUCTURE = re.compile(
    r"^const\s*\{\s*[^}]+\s*\}\s*=\s*React\s*;",
    re.MULTILINE,
)

# Pattern C: `const { a, b } = window.__Y;` at column 0, including the
# multi-line form (`[^}]` matches newlines inside the braces). No
# trailing `|| fallback` — the `;` must directly follow the global, so
# the deterministic fallback form stays allowed. Flat destructures
# only; nested `{ a: { b } }` is out of scope (never seen in repo).
RE_GLOBAL_DESTRUCTURE = re.compile(
    r"^const\s*\{[^}]*\}\s*=\s*(?:window|globalThis)\.__\w+\s*;",
    re.MULTILINE,
)

# Per-line escape — checked across the line itself + 3 lines above
# (same pattern as check_jsx_loader_compat.py).
ESCAPE_MARKER = "<!-- window-x-no-fallback: ignore -->"
ESCAPE_LOOKBACK = 3

# Frontmatter (YAML preamble) is skipped: anything between top-of-file
# `---` and the next `---` line.
RE_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\s*(?:\r?\n|$)", re.DOTALL)


def find_violations(text: str, path: Path) -> List[Tuple[int, str, str]]:
    """Return list of (line_no, kind, snippet) for violations in `text`."""
    # Strip frontmatter so its byte offsets don't confuse line numbers; we
    # replace it with same-count newlines to preserve line numbering.
    fm = RE_FRONTMATTER.match(text)
    if fm:
        newlines = "\n" * fm.group(0).count("\n")
        body = newlines + text[fm.end():]
    else:
        body = text

    lines = body.split("\n")
    violations: List[Tuple[int, str, str]] = []

    def has_escape(line_idx: int) -> bool:
        # Check current + ESCAPE_LOOKBACK lines above.
        start = max(0, line_idx - ESCAPE_LOOKBACK)
        for i in range(start, line_idx + 1):
            if ESCAPE_MARKER in lines[i]:
                return True
        return False

    for m in RE_GLOBAL_READ.finditer(body):
        line_no = body[: m.start()].count("\n")
        if has_escape(line_no):
            continue
        violations.append((line_no + 1, "global-read", lines[line_no].strip()))

    for m in RE_REACT_DESTRUCTURE.finditer(body):
        line_no = body[: m.start()].count("\n")
        if has_escape(line_no):
            continue
        violations.append((line_no + 1, "react-destructure", lines[line_no].strip()))

    for m in RE_GLOBAL_DESTRUCTURE.finditer(body):
        line_no = body[: m.start()].count("\n")
        if has_escape(line_no):
            continue
        violations.append((line_no + 1, "global-destructure", lines[line_no].strip()))

    return violations


def scan(paths: List[Path]) -> List[Tuple[Path, int, str, str]]:
    findings: List[Tuple[Path, int, str, str]] = []
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
            # Path outside the repo (e.g. /tmp/test.jsx for sanity checks).
            display = p
        for line_no, kind, snippet in find_violations(txt, p):
            findings.append((display, line_no, kind, snippet))
    return findings


def collect_default_paths() -> List[Path]:
    # TRK-242 monorepo restructure: portal source moved from docs/* to
    # tools/portal/src/*. Old paths kept as fallback for any future
    # contributor splitting source back into docs/ (shouldn't happen,
    # but cheap insurance).
    roots = [
        REPO_ROOT / "tools" / "portal" / "src" / "interactive",
        REPO_ROOT / "tools" / "portal" / "src" / "getting-started",
    ]
    paths: List[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        paths.extend(root.rglob("*.jsx"))
        paths.extend(root.rglob("*.js"))
    return paths


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Forbid module-scope window.__X / globalThis.__X / React-destructure no-fallback reads (dev-rules.md §S6)."
    )
    parser.add_argument("--ci", action="store_true", help="exit 1 on findings")
    parser.add_argument("paths", nargs="*", help="optional explicit file paths (else defaults to docs/interactive/ + docs/getting-started/)")
    args = parser.parse_args()

    if args.paths:
        paths = [Path(p).resolve() for p in args.paths]
    else:
        paths = collect_default_paths()

    findings = scan(paths)

    if not findings:
        print(f"window-x-no-fallback: ✓ {len(paths)} files clean (dev-rules.md §S6)")
        return EXIT_OK

    # Group by kind for a tidy report.
    by_kind = {"global-read": [], "react-destructure": [], "global-destructure": []}
    for path, line_no, kind, snippet in findings:
        by_kind[kind].append((path, line_no, snippet))

    print(f"window-x-no-fallback: ✗ {len(findings)} violations of dev-rules.md §S6")
    if by_kind["global-read"]:
        print()
        print(f"  Module-scope no-fallback global read ({len(by_kind['global-read'])}):")
        for path, line_no, snippet in by_kind["global-read"]:
            print(f"    {path}:{line_no}: {snippet}")
    if by_kind["react-destructure"]:
        print()
        print(f"  React destructure (use `import {{ useState }} from 'react'`) ({len(by_kind['react-destructure'])}):")
        for path, line_no, snippet in by_kind["react-destructure"]:
            print(f"    {path}:{line_no}: {snippet}")
    if by_kind["global-destructure"]:
        print()
        print(f"  Module-scope destructure of a window global ({len(by_kind['global-destructure'])}):")
        for path, line_no, snippet in by_kind["global-destructure"]:
            print(f"    {path}:{line_no}: {snippet}")

    print()
    print("Fix: replace each line with an ESM import. Examples:")
    print("    const X = window.__X;             → import { X } from './_common/.../X.js';")
    print("    const { a, b } = window.__X;      → import { a, b } from './_common/.../X.js';")
    print("    const { useState } = React;       → import { useState } from 'react';")
    print()
    print("Allowed: `const t = window.__t || ((zh, en) => en);` — fallback form is fine.")
    print("Per-line escape (rare, illustrative-only): place `<!-- window-x-no-fallback: ignore -->` within 3 lines above.")
    print()
    print("Background: see dev-rules.md §S6 + testing-playbook.md (TRK-233/034 LL).")

    if args.ci:
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
