#!/usr/bin/env python3
"""check_portal_bundle_size.py — Portal dist bundle size budget gate.

TD-032a (#TBD). After TD-030 Option C migrated all 43 portal JSX tools
to ESM dist-bundle, the dist directory has no upstream gate against
silent dependency bloat. This script enforces three budgets:

  1. Per-tool entry bundle:    <= 100 KB    (current max tenant-manager.js = 54 KB)
  2. Shared chunk (chunk-*.js): <= 200 KB   (current max = 142 KB react chunk)
  3. Total dist directory:      <= 4 MB     (current = 0.86 MB across 47 .js files;
                                              .map source maps excluded — they don't
                                              ship to the runtime)

Headroom is generous (~2-4x current). The intent is *catch accidents,
not chase optimization*: a moment.js / lodash full-bundle will trip the
per-tool gate well before the totals matter.

Usage:
    python3 scripts/tools/lint/check_portal_bundle_size.py [--ci] [--json]

Exit codes:
    0 — all budgets met
    1 — budget violation (--ci mode)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DIST_DIR = REPO_ROOT / "docs" / "assets" / "dist"

# Budgets in bytes
PER_TOOL_LIMIT = 100 * 1024          # 100 KB
SHARED_CHUNK_LIMIT = 200 * 1024      # 200 KB
TOTAL_LIMIT = 4 * 1024 * 1024        # 4 MB


def classify(name: str) -> str:
    """Categorize a dist file: 'chunk' for shared bundles, 'tool' for entries."""
    if name.startswith("chunk-"):
        return "chunk"
    return "tool"


def fmt_kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def fmt_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.2f} MB"


def run_check() -> tuple[List[Dict], Dict]:
    """Walk DIST_DIR, return (violations, stats)."""
    violations: List[Dict] = []
    stats = {
        "total_bytes": 0,
        "file_count": 0,
        "largest_tool": {"name": None, "bytes": 0},
        "largest_chunk": {"name": None, "bytes": 0},
    }

    if not DIST_DIR.is_dir():
        return [{
            "severity": "error",
            "kind": "dist-missing",
            "message": f"dist dir not found: {DIST_DIR}. Run `make portal-build` first.",
        }], stats

    for js in sorted(DIST_DIR.glob("*.js")):
        size = js.stat().st_size
        stats["total_bytes"] += size
        stats["file_count"] += 1

        kind = classify(js.name)
        if kind == "tool":
            if size > stats["largest_tool"]["bytes"]:
                stats["largest_tool"] = {"name": js.name, "bytes": size}
            if size > PER_TOOL_LIMIT:
                violations.append({
                    "severity": "error",
                    "kind": "per-tool-exceeded",
                    "file": js.name,
                    "size": size,
                    "limit": PER_TOOL_LIMIT,
                    "message": (
                        f"{js.name} is {fmt_kb(size)} (limit {fmt_kb(PER_TOOL_LIMIT)}). "
                        f"Investigate dependency additions, or split the tool into "
                        f"smaller subtree imports."
                    ),
                })
        elif kind == "chunk":
            if size > stats["largest_chunk"]["bytes"]:
                stats["largest_chunk"] = {"name": js.name, "bytes": size}
            if size > SHARED_CHUNK_LIMIT:
                violations.append({
                    "severity": "error",
                    "kind": "shared-chunk-exceeded",
                    "file": js.name,
                    "size": size,
                    "limit": SHARED_CHUNK_LIMIT,
                    "message": (
                        f"shared chunk {js.name} is {fmt_kb(size)} "
                        f"(limit {fmt_kb(SHARED_CHUNK_LIMIT)}). React + react-dom + "
                        f"shared deps live here; growth means a heavy dep is now "
                        f"shared by 2+ tools. Audit recent imports."
                    ),
                })

    if stats["total_bytes"] > TOTAL_LIMIT:
        violations.append({
            "severity": "error",
            "kind": "total-exceeded",
            "size": stats["total_bytes"],
            "limit": TOTAL_LIMIT,
            "message": (
                f"total dist is {fmt_mb(stats['total_bytes'])} "
                f"(limit {fmt_mb(TOTAL_LIMIT)}). Multiple tools have grown together; "
                f"this is the aggregate-cost gate."
            ),
        })

    return violations, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Portal dist bundle size budget")
    parser.add_argument("--ci", action="store_true", help="exit 1 on violation")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    violations, stats = run_check()

    if args.json:
        print(json.dumps({
            "stats": stats,
            "violations": violations,
            "limits": {
                "per_tool": PER_TOOL_LIMIT,
                "shared_chunk": SHARED_CHUNK_LIMIT,
                "total": TOTAL_LIMIT,
            },
            "summary": {"errors": len(violations)},
        }, ensure_ascii=False, indent=2))
    else:
        print(f"Portal dist budget check ({stats['file_count']} files, "
              f"{fmt_mb(stats['total_bytes'])} total)")
        if stats["largest_tool"]["name"]:
            t = stats["largest_tool"]
            print(f"  largest tool:  {t['name']} = {fmt_kb(t['bytes'])} "
                  f"(limit {fmt_kb(PER_TOOL_LIMIT)})")
        if stats["largest_chunk"]["name"]:
            c = stats["largest_chunk"]
            print(f"  largest chunk: {c['name']} = {fmt_kb(c['bytes'])} "
                  f"(limit {fmt_kb(SHARED_CHUNK_LIMIT)})")
        print()

        if not violations:
            print("✓ All bundle size budgets met.")
        else:
            for v in violations:
                print(f"✗ [{v['kind']}] {v['message']}")
            print(f"\nTotal violations: {len(violations)}")

    if args.ci and violations:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
