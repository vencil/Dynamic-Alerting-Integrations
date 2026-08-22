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


def _strip_bom(text: str) -> str:
    """Drop a leading BOM.

    ⛔ Both text readers need this and both used to do it inline — two copies
    of a one-liner is still two copies, and the first version of the fix landed
    in only one of them. The BOM matters because ``\\ufeff`` is not whitespace:
    a word-start anchor cannot match a header on line 1 behind one, so the
    array (or the command map) reads as EMPTY. The file wrappers open with
    ``utf-8-sig``, but the tag-blob caller decodes with plain ``utf-8`` and
    hands the BOM straight through.
    """
    return text.lstrip("﻿")


_COMMAND_MAP_ENTRY_RE = re.compile(r'"([a-z][a-z0-9-]+)":\s*"([^"]+)"')


def parse_command_map_text(text: str) -> Dict[str, str]:
    """Parse ``COMMAND_MAP`` (command → script filename) from entrypoint source.

    ⛔ THE reader — ``check_image_pin_capability.py`` imports this rather than
    keeping its own transcription for tag blobs. It used to keep one, and that
    copy sat thirty lines above a ``⛔`` comment saying transcriptions-that-must-
    be-kept-in-sync are themselves the defect: the admonition had a live
    instance in its own file.
    """
    commands: Dict[str, str] = {}
    in_map = False
    for line in _strip_bom(text).split("\n"):
        stripped = line.strip()
        if stripped.startswith("COMMAND_MAP"):
            in_map = True
            continue
        if in_map:
            if stripped == "}":
                break
            m = _COMMAND_MAP_ENTRY_RE.match(stripped)
            if m:
                commands[m.group(1)] = m.group(2)
    return commands


def parse_command_map(path: Path | None = None) -> Dict[str, str]:
    """Parse COMMAND_MAP from entrypoint.py.

    Returns dict mapping command name → script filename.
    e.g. {"check-alert": "check_alert.py", ...}
    """
    return parse_command_map_text(
        (path or ENTRYPOINT_PATH).read_text(encoding="utf-8-sig"))


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

    ⚠️ ``NAME+=(`` deliberately does NOT match: see :func:`parse_build_sh_array_text`
    for why modelling bash's assignment semantics textually was reverted.
    """
    return rf"(?:^|[\s;]){re.escape(array_name)}=\("


@lru_cache(maxsize=None)
def _array_open_re(array_name: str) -> "re.Pattern[str]":
    return re.compile(array_open_pattern(array_name))


def _split_at_array_close(text: str) -> "tuple[str, bool]":
    """``(before_the_close, closed)`` for one line of an array literal.

    ⛔ ONE rule for the close, used on the header line and on every body line
    alike. Matching a shape instead — ``stripped == ")"``, then
    ``stripped.endswith(")")`` — is what kept the "block opens and never
    closes" class alive through three rounds of fixes: each round added the
    spelling it had been shown (`)`, `)  # comment`, `TOOL_FILES=()`) and
    declared the class closed, and the next round found `);` and
    ``) > /dev/null``. Both are ordinary bash and both made the reader swallow
    the whole rest of build.sh as entries.

    Quote-aware, because ``"ops/a(1).py"`` is a legal entry and the ``)``
    inside it does not close anything. Backslash escapes are honoured for the
    same reason.
    """
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "\\":
            i += 2
            continue
        elif ch == ")":
            return text[:i], True
        i += 1
    return text, False


def _array_words(text: str) -> "list[str]":
    """Split one array fragment into words the way bash would.

    ``shlex`` rather than ``.split()`` so a quoted entry survives whole.

    ⛔ A trailing lone ``\\`` is a LINE CONTINUATION, not a word. bash joins the
    lines and yields the entries either side; this reader works line by line,
    so it drops the marker instead — leaving it in produced a phantom entry
    named ``\\`` (measured against real bash, which yields two entries and no
    such token).

    Unbalanced quotes fall back to whitespace splitting: this reader must never
    raise on a build.sh it cannot fully understand — a crash here takes down
    the lint that was supposed to report the problem. ⚠️ The fallback strips
    quote characters too, because leaving a leading ``"`` on the name makes the
    file read as absent, and "absent" is silently skipped.
    """
    try:
        words = shlex.split(text, comments=False)
    except ValueError:
        words = [w.strip("\"'") for w in text.split()]
    return [w for w in (x.strip("(),") for x in words) if w and w != "\\"]


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
    * **Stop at the first close.** A revision tried to model bash's assignment
      semantics instead (``+=`` appends, plain ``=`` resets) and kept scanning
      to EOF so a later block could be seen. Measured against real ``bash``,
      that made THREE ordinary constructs wrong, every one of them in the
      silent direction — the array was replaced by whatever the last textual
      ``NAME=(`` happened to be:

      ==============================================  ==================  ================
      build.sh construct                              bash                that revision
      ==============================================  ==================  ================
      ``usage() { echo "  TOOL_FILES=( ops/x.py )" }``  ``a.py b.py``       ``x.py``
      ``if [ "$M" = 1 ]; then TOOL_FILES=( t.py ); fi``  ``a.py``            ``t.py``
      a heredoc that mentions the array                ``a.py``            ``doc.py``
      ==============================================  ==================  ================

      Text scanning cannot tell an assignment from a mention of one; only a
      shell can. Stopping at the close is what the reader can actually justify,
      and it is what every one of those cases needs.

      ⚠️ Consequence, disclosed rather than papered over: a later
      ``NAME+=( … )`` is NOT read. ``build.sh`` uses no ``+=`` today (checked),
      and the failure direction if one is added is under-reporting, which the
      bidirectional check in ``check_build_completeness`` reports loudly.
    """
    entries: Set[str] = set()
    in_block = False
    open_re = _array_open_re(array_name)
    # ⛔ Strip a BOM HERE, not only in the file wrapper. The tag-blob caller
    # decodes with plain ``utf-8``, so a BOM reached this reader untouched, the
    # word-start anchor could not match a header on line 1, and that tag read
    # as shipping NO tools at all. Fixing only the wrapper left the shared
    # reader carrying the defect for its other caller.
    for line in _strip_bom(text).split("\n"):
        stripped = strip_bash_comment(line.strip())
        if not stripped:
            continue
        if not in_block:
            m = open_re.search(stripped)
            if m is None:
                continue
            in_block = True
            stripped = stripped[m.end():]
        # ⛔ 同一條收尾規則，陣列頭那一行與每一行本體都走它。用形狀比對
        # （`== ")"`、`endswith(")")`）是這一類活過三輪修法的原因：每一輪都補上
        # 剛被指出的那個拼法然後宣告關閉，下一輪就找到 `);` 與 `) > /dev/null`。
        body, closed = _split_at_array_close(stripped)
        entries.update(_array_words(body))
        if closed:
            break
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
