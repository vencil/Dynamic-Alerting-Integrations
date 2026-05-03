#!/usr/bin/env python3
"""check_tool_registry_jsx_parity.py — every tool-registry.yaml entry must have a backing .jsx file (and vice versa).

Why this exists
---------------
docs/assets/tool-registry.yaml is the SSOT for the 43-ish portal
tools (Hub cards, search, related-tool graph, journey-phase routing
all key off it). When a JSX file gets renamed / deleted but the
registry isn't updated, or when a new JSX file lands in
docs/interactive/tools/ but never gets a registry entry, the Hub
silently drops the tool from the catalog.

This lint catches both directions:

  1. **Registry-orphan**: registry entry whose `file:` path doesn't
     resolve on disk. Hub link will 404.

  2. **JSX-orphan**: a `.jsx` file under docs/interactive/tools/ that
     isn't referenced by any registry entry. Either it should be
     registered (forgotten step) or it's an internal helper that
     should sit under a known opt-out path.

Internal opt-out paths (not flagged as JSX-orphans)
---------------------------------------------------
  - `_common/`           — PR-portal-1 shared library
  - `tenant-manager/`    — PR-2d (#153) sub-component subdirectory
  - `operator-setup-wizard/` — PR-portal-4 sub-component subdirectory
  - top-level files matching `portal-shared.jsx` / `*Tab.jsx` —
    consumed via window.__portalShared by the Self-Service Portal
    tabs, not directly via the Hub

Anything else under docs/interactive/tools/ is expected to have a
registry entry.

Usage
-----
  python3 scripts/tools/lint/check_tool_registry_jsx_parity.py

Exit codes:
  0 = all entries resolve + no JSX orphans
  1 = at least one parity violation
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY = REPO_ROOT / "docs" / "assets" / "tool-registry.yaml"
JSX_ROOT = REPO_ROOT / "docs"

# Paths under docs/interactive/tools/ that are NOT user-facing tools
# and therefore should NOT have a registry entry.
INTERNAL_DIR_PREFIXES = (
    "_common/",
    "tenant-manager/",
    "operator-setup-wizard/",
)
INTERNAL_FILE_PATTERNS = {
    "portal-shared.jsx",
}
INTERNAL_FILE_SUFFIXES = (
    "Tab.jsx",
)


def load_registry() -> list[dict]:
    with REGISTRY.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("tools", [])


def is_internal_jsx(rel_path: str) -> bool:
    """Files that intentionally have no registry entry."""
    parts = rel_path.replace("\\", "/")
    if not parts.startswith("interactive/tools/"):
        # wizard.jsx etc. — registry references via getting-started/...
        return False
    inner = parts[len("interactive/tools/"):]
    if any(inner.startswith(p) for p in INTERNAL_DIR_PREFIXES):
        return True
    if inner in INTERNAL_FILE_PATTERNS:
        return True
    if any(inner.endswith(s) for s in INTERNAL_FILE_SUFFIXES):
        return True
    return False


def main() -> int:
    # argparse with no flags — its purpose here is to enforce the
    # repo-wide CLI contract (test_help_exits_zero / test_invalid_args
    # _exits_nonzero in tests/shared/test_tool_exit_codes.py): `--help`
    # exits 0; unknown flags exit 2 (argparse default behaviour).
    parser = argparse.ArgumentParser(
        description=(
            "Validate that every tool-registry.yaml entry has a backing "
            ".jsx file (and vice versa, except internal opt-out paths)."
        ),
    )
    parser.parse_args()

    if not REGISTRY.exists():
        print(f"ERROR: {REGISTRY} not found", file=sys.stderr)
        return 1

    tools = load_registry()
    issues: list[str] = []

    # Direction 1: every registry `file:` resolves to an actual JSX
    registered_paths: set[str] = set()
    for entry in tools:
        key = entry.get("key", "<no-key>")
        rel = entry.get("file")
        if not rel:
            issues.append(f"registry entry '{key}' has no `file:` field")
            continue
        registered_paths.add(rel.replace("\\", "/"))
        full = JSX_ROOT / rel
        if not full.exists():
            issues.append(
                f"registry-orphan: '{key}' file: '{rel}' does not exist on disk"
            )

    # Direction 2: every top-level .jsx under docs/interactive/tools/
    # appears in the registry, except internal opt-outs.
    tools_dir = JSX_ROOT / "interactive" / "tools"
    if tools_dir.exists():
        for jsx in tools_dir.rglob("*.jsx"):
            rel = jsx.relative_to(JSX_ROOT).as_posix()
            if is_internal_jsx(rel):
                continue
            if rel not in registered_paths:
                issues.append(
                    f"jsx-orphan: '{rel}' is not referenced by any "
                    f"tool-registry.yaml `file:` entry"
                )

    if not issues:
        print(
            f"OK: tool-registry.yaml has {len(tools)} entries; "
            f"all resolve and no JSX orphans."
        )
        return 0

    print(f"FAIL: {len(issues)} parity violation(s):")
    for issue in issues:
        print(f"  - {issue}")
    print(
        "\nFix: either add a registry entry under docs/assets/tool-registry.yaml "
        "for the orphan, OR move the file under one of the internal opt-out "
        "paths (_common/, tenant-manager/, operator-setup-wizard/, *Tab.jsx, "
        "portal-shared.jsx)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
