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
import shlex
import subprocess
from functools import lru_cache
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
    # v2.10.0 (da-tools ROI r5) — minimal CRD YAML serializer shared by
    # operator_generate + migrate_to_operator. Library, not CLI.
    "_lib_yaml.py",
    # v2.8.0 PR-2 — shared dispatcher absorbs ~95% of guard /
    # batchpr / parser dispatcher boilerplate. Library, not CLI.
    "_lib_godispatch.py",
    # v2.8.0 PR #432 — cross-platform compat helpers (try_utf8_stdout
    # consolidated from 4 callsites). Library, not CLI.
    "_lib_compat.py",
    # #452 Track A — canonical 0/1/2 exit-code constants. Library, not CLI.
    "_lib_exitcodes.py",
    # #1339 — single answer to "what is in a conf.d/": the recursive read
    # plus the guard a flat reader calls so a hierarchical tree can never
    # look empty. Library, not CLI.
    "_lib_confd.py",
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
    # #1310 — threshold-registry loader lib. Imported at top level by
    # scaffold_tenant.py for `is_shipped_optional_key` (which optional_overrides
    # keys the platform ships on the declared list, and therefore which ones the
    # interactive prompt may pre-fill). Library, not CLI: the registry gate that
    # drives it is a pre-commit lint, not a `da-tools <cmd>`.
    "_registry_lib.py",
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
    return _parse_build_sh_array(path or BUILD_SH_PATH, "TOOL_FILES")


def parse_build_sh_repo_data_files(path: Path | None = None) -> Set[str]:
    """Parse the ``REPO_DATA_FILES`` array from build.sh (repo-root-relative).

    ``TOOL_FILES`` entries resolve against ``scripts/tools``; this second array
    carries the files a shipped tool reads from elsewhere in the repo tree and
    which therefore need copying into the flat image by their own loop (#1494).
    Returned paths are PROJECT_ROOT-relative as written, e.g.
    ``k8s/03-monitoring/configmap-rules-platform.yaml``.

    An absent array is not an error — it yields the empty set, so callers that
    only care about ``TOOL_FILES`` keep working against an older build.sh.
    """
    return _parse_build_sh_array(path or BUILD_SH_PATH, "REPO_DATA_FILES")


_BASH_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")


def strip_bash_comment(line: str) -> str:
    """Remove a bash comment from *line*, using bash's own rule.

    ⛔ ``#`` opens a comment only at the START OF A WORD — line start or after
    whitespace. A naive ``split("#", 1)`` also truncates ``ops/a.py#tag`` and
    ``"ops/c#d.py"``, which bash keeps whole; the reader would then look for a
    file under a shortened name, find nothing, and SILENTLY skip it. That is
    the fail-open direction, so the rule is transcribed rather than
    approximated.

    Verified against bash: ``TOOL_FILES=( ops/a.py#no-space  ops/b.py  # c
    "ops/c#d.py" )`` expands to three words, two of which keep their ``#``.
    """
    return _BASH_COMMENT_RE.sub("", line).strip()


def array_open_pattern(array_name: str) -> str:
    """Regex source matching a bash array header for *array_name*.

    ⛔ Anchored at a word start. A plain ``f"{name}=(" in line`` substring test
    also matches ``EXTRA_TOOL_FILES=(``, so a second, unrelated array opens the
    block and donates its entries to this one's caller.

    The ``append`` group distinguishes ``NAME+=(`` from ``NAME=(``: bash
    appends for the first and RESETS for the second, and a reader that treats
    both as "add" over-reports a reassigned array.
    """
    return rf"(?:^|[\s;]){re.escape(array_name)}(?P<append>\+)?=\("


@lru_cache(maxsize=None)
def _array_open_re(array_name: str) -> "re.Pattern[str]":
    return re.compile(array_open_pattern(array_name))


def _array_words(text: str) -> "list[str]":
    """Split one array fragment into words the way bash would.

    ``shlex`` rather than ``.split()`` so a quoted entry survives, and rather
    than ``.strip("\\"'(),")`` so an entry is not silently reshaped. Unbalanced
    quotes fall back to whitespace splitting: this reader must never raise on a
    build.sh it cannot fully understand — a crash here takes down the lint that
    was supposed to report the problem.
    """
    try:
        words = shlex.split(text, comments=False)
    except ValueError:
        words = text.split()
    return [w for w in (x.strip("(),") for x in words) if w]


def parse_build_sh_array_text(text: str, array_name: str) -> Set[str]:
    """Parse one bash array out of build.sh **source text**.

    ⛔ THE reader for these arrays — there is no second copy.
    ``check_image_pin_capability.py`` needs the same rules against a git TAG's
    blob (text, not a file), and used to keep its own transcription;
    ``test_text_parsers_match_lib_lint_helpers_on_head`` existed to notice when
    the two drifted. A transcription that must be kept in sync is the defect,
    not the drift detector: measured, four parsing rules had to be applied in
    two places and one round fixed only one side. The file-reading wrapper is
    :func:`_parse_build_sh_array`; everything else lives here.

    Rules, each with the measured failure it prevents:

    * **Strip the comment before every other test.** ``# TOOL_FILES=(`` must
      not open a block (measured: yielded ``ops/ghost.py``) and ``)  # end of
      list`` must close one (measured: swallowed every remaining line of
      build.sh as entries).
    * **Anchor the array name at a word start.** ``in stripped`` let
      ``EXTRA_TOOL_FILES=(`` match (measured: yielded ``ops/extra.py``).
    * **Parse the header line's remainder, and close on it.** Without this,
      ``TOOL_FILES=()`` and the one-line ``TOOL_FILES=( a b )`` opened a block
      that never closed — same swallow-the-rest failure as the trailing-comment
      case, and the one-line form did not even yield its own entries. This is
      why the fix is not "handle three more spellings": the *class* is "a line
      that both opens and closes", and the class is closed by parsing the line
      rather than by pattern-matching its shape.
    * **``+=`` appends, plain ``=`` resets**, as bash does. A second ``=(``
      block silently added to the first before, over-reporting the shipped set.
    * A repeated header **inside** an unclosed block is not an entry (measured:
      yielded a literal ``TOOL_FILES=``).
    """
    entries: Set[str] = set()
    in_block = False
    open_re = _array_open_re(array_name)
    for line in text.split("\n"):
        stripped = strip_bash_comment(line.strip())
        if not stripped:
            continue
        m = open_re.search(stripped)
        if m is not None:
            if in_block:
                continue
            if not m.group("append"):
                entries.clear()
            in_block = True
            rest = stripped[m.end():]
            closed = ")" in rest
            if closed:
                rest = rest.split(")", 1)[0]
            entries.update(_array_words(rest))
            if closed:
                in_block = False
            continue
        if not in_block:
            continue
        # ⛔ 沒有 `stripped == ")"` 那一格：`")".endswith(")")` 為真，下面這格
        # 已經涵蓋它（`_array_words("")` 是空的）。留著是等價分支——變異實測
        # 把它停用後零測試轉紅，那不是「缺守衛」而是「這段程式碼不存在」。
        if stripped.endswith(")"):
            entries.update(_array_words(stripped[:-1]))
            in_block = False
            continue
        entries.update(_array_words(stripped))
    return entries


def _parse_build_sh_array(path: Path, array_name: str) -> Set[str]:
    """File-reading wrapper over :func:`parse_build_sh_array_text`.

    ⛔ ``utf-8-sig``. A BOM is invisible to bash and to Python's import
    machinery, but ``\\ufeff`` is not whitespace, so the word-start anchor
    could not match a header on line 1 and the array read as EMPTY — the
    silent, worse direction. This repo already assumes BOMs exist (two
    scanners in ``check_build_completeness.py`` open with ``utf-8-sig`` for the
    same reason, citing Windows editors).
    """
    return parse_build_sh_array_text(
        path.read_text(encoding="utf-8-sig"), array_name)


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
