#!/usr/bin/env python3
"""_lint_helpers.py — Shared utilities for lint tools.

v2.4.0: Extracted from duplicated code in check_build_completeness.py,
check_cli_coverage.py, and tests/test_entrypoint.py.

v2.8.0: Added diff-aware helpers for class (b)/(c) lints (per
docs/internal/lint-policy.md): get_diff_added_lines, resolve_diff_base,
parse_bypass_tag.

Provides common parsers for entrypoint.py COMMAND_MAP and build.sh TOOL_FILES.
"""
from __future__ import annotations

import os
import re
import subprocess
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
    # v2.8.0 PR #432 — cross-platform compat helpers (try_utf8_stdout
    # consolidated from 4 callsites). Library, not CLI.
    "_lib_compat.py",
    # #452 Track A — canonical 0/1/2 exit-code constants. Library, not CLI.
    "_lib_exitcodes.py",
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
    # #924 / ADR-028 — long-running revocation-reconciler DAEMON, run as a
    # Deployment via direct invoke (python3 …/_federation_revocation_reconciler.py),
    # not a `da-tools <cmd>` operator CLI. Baked into the image but exempt from
    # COMMAND_MAP; the `_` prefix marks it non-dispatched.
    "_federation_revocation_reconciler.py",
    # #719 — shared SoT extractor for the threshold observed-map. Library
    # imported by threshold_recommend.py (and transitively threshold_govern.py),
    # not a CLI command. Ships together with its data file
    # metric_observed_map.yaml (same-dir lookup via DEFAULT_MAP_PATH).
    "_observed_map_lib.py",
    # #719 — data file for _observed_map_lib.py (non-.py entries are already
    # filtered by the orphan check; listed for symmetry with metric-dictionary.yaml).
    "metric_observed_map.yaml",
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

    Returns set of basenames (e.g. {"check_alert.py", ...}). Delegates the
    actual parsing to :func:`parse_build_sh_tool_paths` (the single source of
    truth) and applies ``basename`` here, so the two APIs cannot drift.
    """
    return {os.path.basename(p) for p in parse_build_sh_tool_paths(path)}


def parse_build_sh_tool_paths(path: Path | None = None) -> Set[str]:
    """Parse TOOL_FILES array from build.sh, keeping relative paths.

    Unlike :func:`parse_build_sh_tools` (basenames, for set comparison with
    COMMAND_MAP), this preserves the ``scripts/tools/``-relative path as
    written in build.sh (e.g. ``"ops/threshold_recommend.py"``) so callers
    can open and inspect the source files (the transitive underscore-import
    scan in check_build_completeness.py needs file contents, not just names).
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
                    tools.add(name)
    return tools


# ---------------------------------------------------------------------------
# Diff-aware lint helpers (lint-policy.md compliance for class b/c lints)
# ---------------------------------------------------------------------------


class DiffBaseMissingError(RuntimeError):
    """Raised when the diff base ref (e.g. origin/main) cannot be resolved.

    Most common cause: GitHub Actions ``actions/checkout@v4`` defaults to
    ``fetch-depth: 1`` (shallow clone), which leaves no ``origin/main`` in
    the CI worker's ``.git`` directory. See ``docs/internal/lint-policy.md``
    §"GitHub Actions 淺拷貝陷阱" — workflows running diff-aware lints must
    set ``fetch-depth: 0`` (full history) or explicitly ``git fetch origin
    <base-ref>`` before invoking the lint.
    """


def resolve_diff_base(env_var: str = "LINT_DIFF_BASE", default: str = "origin/main") -> str:
    """Return the diff base ref, validating it actually exists locally.

    Resolution order:

    1. ``$LINT_DIFF_BASE`` env var (explicit override; useful for testing
       a different branch base locally).
    2. ``origin/$GITHUB_BASE_REF`` (auto-set by GitHub Actions on
       ``pull_request`` events; means "the branch this PR targets").
    3. ``origin/main`` default for local dev not on a PR branch.

    Calls ``git rev-parse --verify`` to confirm the ref resolves; raises
    ``DiffBaseMissingError`` with a fetch-depth hint if not — never
    silently falls through to "scan everything", which would defeat the
    diff-aware purpose per lint-policy.md.
    """
    base = os.environ.get(env_var)
    if not base:
        gh_base = os.environ.get("GITHUB_BASE_REF")
        if gh_base:
            base = f"origin/{gh_base}"
        else:
            base = default
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=10,
    )
    if result.returncode != 0:
        hint_branch = base.removeprefix("origin/")
        raise DiffBaseMissingError(
            f"git diff base ref '{base}' does not resolve in this repo.\n"
            f"  - In CI: ensure actions/checkout@v4 uses fetch-depth: 0\n"
            f"    (or `git fetch origin {hint_branch}` before lint)\n"
            f"  - Locally: ensure you have an up-to-date `origin/main`\n"
            f"    (run `git fetch origin main`)\n"
            f"  - Override with $LINT_DIFF_BASE if your base branch differs\n"
            f"  See docs/internal/lint-policy.md §\"GitHub Actions 淺拷貝陷阱\""
        )
    return base


_HUNK_HEADER_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")


def _parse_unified_zero_diff(diff_text: str) -> list:
    """Parse `git diff --unified=0` output and return added lines with line numbers."""
    added = []
    current_lineno = None
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            m = _HUNK_HEADER_RE.match(line)
            current_lineno = int(m.group(1)) if m else None
            continue
        if current_lineno is None:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append((current_lineno, line[1:]))
            current_lineno += 1
        elif line.startswith("-"):
            pass  # deleted lines don't advance new-file line counter
        else:
            current_lineno += 1  # context line — guard for non-unified=0
    return added


def get_diff_added_lines(file_path: Path, base: str) -> list:
    """Return ``[(line_no, content), ...]`` for lines ADDED in current diff vs ``base``.

    Parses ``git diff --unified=0`` hunks. Existing (unchanged) lines and
    removed (``-``) lines are not returned. ``line_no`` is the line number
    in the *current* (post-diff) file, suitable for citing in lint errors.

    Returns ``[]`` if the file is identical to base. Returns all file lines
    if the file is newly added in this diff (no base version).
    """
    if file_path.is_absolute():
        try:
            rel = file_path.relative_to(REPO_ROOT)
        except ValueError:
            rel = file_path
    else:
        rel = file_path
    result = subprocess.run(
        ["git", "diff", "--unified=0", "--no-color", base, "--", str(rel)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), check=True, timeout=30,
    )
    return _parse_unified_zero_diff(result.stdout)


# PR-body bypass tag matcher per lint-policy.md §4. CI workflows pass
# ${{ github.event.pull_request.body }} via env var or file flag.
_BYPASS_TAG_RE = re.compile(
    r"bypass-lint:\s*(?P<lint_name>[\w-]+)\s*\n\s*reason:\s*(?P<reason>.+?)(?=\n\s*issue:|\n\s*\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def parse_bypass_tag(pr_body, lint_name: str):
    """Return the bypass reason if ``pr_body`` contains a valid ``bypass-lint:
    <lint_name>`` block, else None.

    Spec from lint-policy.md §4: tag must be on its own line, followed by
    ``reason:`` line. Optional ``issue: #NN`` after. Matched case-insensitively.
    """
    if not pr_body:
        return None
    for m in _BYPASS_TAG_RE.finditer(pr_body):
        if m.group("lint_name").lower() == lint_name.lower():
            return m.group("reason").strip()
    return None
