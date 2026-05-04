#!/usr/bin/env python3
"""_lint_helpers.py — Shared utilities for lint tools.

v2.4.0: Extracted from duplicated code in check_build_completeness.py,
check_cli_coverage.py, and tests/test_entrypoint.py.

Provides common parsers for entrypoint.py COMMAND_MAP and build.sh TOOL_FILES.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Set

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

ENTRYPOINT_PATH = REPO_ROOT / "components" / "da-tools" / "app" / "entrypoint.py"
BUILD_SH_PATH = REPO_ROOT / "components" / "da-tools" / "app" / "build.sh"

# build.sh items that are libraries/data, not CLI commands
BUILD_EXEMPT = frozenset({
    "_lib_python.py",
    "_lib_constants.py",
    "_lib_validation.py",
    "_lib_prometheus.py",
    "_lib_io.py",
    # v2.8.0 PR-2 — shared dispatcher absorbs ~95% of guard /
    # batchpr / parser dispatcher boilerplate. Library, not CLI.
    "_lib_godispatch.py",
    # v2.8.0 PR-3a — generate_alertmanager_routes.py split into 5 helpers.
    # These are library modules consumed by the main file via re-export,
    # not CLI commands themselves.
    "_grar_validate.py",
    "_grar_merge.py",
    "_grar_parse.py",
    "_grar_routes.py",
    "_grar_render.py",
    "metric-dictionary.yaml",
    "generate_tenant_mapping_rules.py",
    # v2.8.0 Phase B Track A A5: ship-but-not-public CLI design tradeoff.
    # describe_tenant.py is a v2.7.0 internal tool that ships in the docker
    # image as a transitive dependency for tenant_verify.py (which IS
    # public via `da-tools tenant-verify`). The arg shape may change before
    # describe_tenant gets its own promotion to a stable da-tools subcommand,
    # so we deliberately keep it out of COMMAND_MAP. See
    # components/da-tools/app/build.sh near the dx/describe_tenant.py entry
    # for the full rationale.
    "describe_tenant.py",
})


def parse_command_map(path: Path | None = None) -> Dict[str, str]:
    """Parse COMMAND_MAP from entrypoint.py.

    Returns dict mapping command name → script filename.
    e.g. {"check-alert": "check_alert.py", ...}
    """
    path = path or ENTRYPOINT_PATH
    commands: Dict[str, str] = {}
    in_map = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("COMMAND_MAP"):
                in_map = True
                continue
            if in_map:
                if stripped == "}":
                    break
                m = re.match(r'"([a-z][a-z0-9-]+)":\s*"([^"]+)"', stripped)
                if m:
                    commands[m.group(1)] = m.group(2)
    return commands


def parse_command_map_keys(path: Path | None = None) -> Set[str]:
    """Parse COMMAND_MAP keys only (command names, no script filenames)."""
    return set(parse_command_map(path).keys())


def parse_build_sh_tools(path: Path | None = None) -> Set[str]:
    """Parse TOOL_FILES array from build.sh.

    Returns set of basenames (e.g. {"check_alert.py", ...}).
    """
    path = path or BUILD_SH_PATH
    tools: Set[str] = set()
    in_block = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if "TOOL_FILES=(" in stripped:
                in_block = True
                continue
            if in_block:
                if stripped == ")":
                    break
                if not stripped or stripped.startswith("#"):
                    continue
                name = stripped.strip("\"'(),").strip()
                if name:
                    tools.add(os.path.basename(name))
    return tools
