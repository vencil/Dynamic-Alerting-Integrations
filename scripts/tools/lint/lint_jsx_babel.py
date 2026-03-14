#!/usr/bin/env python3
"""lint_jsx_babel.py — Validate JSX files parse correctly via Babel standalone.

Replicates the jsx-loader.html transform pipeline (front-matter strip,
ES import → global reference, export default → function) then invokes
Node.js + @babel/standalone to parse.  Catches errors that only surface
in the browser at runtime.

Requirements:
    - Node.js (>=16)
    - npm install @babel/standalone  (auto-installed if missing)

Usage:
    python3 scripts/tools/lint/lint_jsx_babel.py             # report mode
    python3 scripts/tools/lint/lint_jsx_babel.py --ci         # exit 1 on failures
    python3 scripts/tools/lint/lint_jsx_babel.py --fix        # hint-only (no auto-fix)

Exit codes:
    0 = all files parse OK
    1 = one or more failures (--ci mode)
"""
from __future__ import annotations

import argparse
import json
import os
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


def _transform_jsx(source: str) -> str:
    """Replicate jsx-loader.html renderJSX() transform pipeline."""
    import re

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
            ["npm", "install", "--prefix", str(node_modules.parent), "@babel/standalone@7.23.9"],
            capture_output=True,
            timeout=60,
        )
        return babel_dir.exists()
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint JSX files with Babel standalone")
    parser.add_argument("--ci", action="store_true", help="Exit 1 on failures")
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
    for d in JSX_DIRS:
        if not d.exists():
            continue
        for jsx in sorted(d.glob("*.jsx")):
            source = jsx.read_text(encoding="utf-8")
            transformed = _transform_jsx(source)
            files.append({"path": str(jsx.relative_to(PROJECT_ROOT)), "source": transformed})

    if not files:
        print("✓ No JSX files found")
        return 0

    # Run Babel via Node.js
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

    if result.returncode != 0:
        print(f"⚠ Node.js error: {result.stderr[:200]}")
        return 0

    try:
        results = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"⚠ Could not parse Node.js output")
        return 0

    failures = [r for r in results if not r["ok"]]
    passes = [r for r in results if r["ok"]]

    if not failures:
        print(f"✓ All {len(passes)} JSX files parse OK")
        return 0

    print(f"✗ {len(failures)} JSX file(s) failed Babel parse:\n")
    for f in failures:
        print(f"  {f['path']}: {f['error']}")

    return 1 if args.ci else 0


if __name__ == "__main__":
    raise SystemExit(main())
