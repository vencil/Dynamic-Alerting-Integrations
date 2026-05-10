#!/usr/bin/env python3
"""check_bat_ascii_purity.py -- L1 guard for pitfall #45 (CJK-in-.bat).

Why this hook exists (byte-level root cause)
--------------------------------------------
Desktop Commander's ``start_process`` launches Windows .bat files through
a child cmd.exe that inherits the parent process's OEM codepage (typically
cp950 on zh-TW Windows, cp437 on en-US) -- *NOT* cp65001.

cmd.exe's batch parser reads the .bat file byte-by-byte and does NOT
normalize UTF-8 multi-byte sequences. Any byte >= 0x80 inside a REM
comment or string can land on what the parser treats as a shell
metacharacter (the 0x80-0xBF range covers several cp1252 punctuation
bytes like em-dash continuations). The parser's internal state then
corrupts *downstream* lines, which is why the symptom is that
``@echo off`` / ``setlocal`` / ``goto`` appear to "not exist" on lines
*after* the CJK one.

``cmd /c`` indirection does NOT help: the child cmd still inherits
the parent codepage. ``chcp 65001`` *inside* the .bat is too late,
because the parser has already read the preamble using the wrong
codepage.

PowerShell-invoked .bat does NOT hit this path -- PowerShell runtime
decodes the file to UTF-16 before handing the command line to cmd, so
the byte-level collision happens one level earlier.

Conclusion: ``.bat`` files under ``scripts/ops/`` (which can be invoked
by start_process) MUST be pure ASCII.

What this hook enforces
-----------------------
For every ``scripts/ops/*.bat`` (restricted by pre-commit ``files:``):

1. No byte >= 0x80 (ASCII-only, no CJK / em-dash / non-breaking space)
2. No UTF-8 BOM prefix (``EF BB BF`` breaks the first command)
3. No bare LF line endings (cmd.exe needs CRLF -- pitfall row #2)

Companion to ``tests/dx/test_bat_label_integrity.py`` which enforces
the same rules at pytest time. Defence-in-depth: pre-commit stops bad
commits locally, pytest catches anything that slipped through or was
added via the Windows-side ``--no-verify`` escape hatch.

Lint class & scope (lint-policy.md §3)
--------------------------------------
Class **(b)** — negative pattern (forbidden bytes / line endings) +
allowlist scope (only ``scripts/ops/*.bat``). Default invocation is
already effectively **diff-aware** via pre-commit's ``files:`` filter
(only staged .bat files reach this hook). For ad-hoc CLI invocation:

* No paths, no flag → diff-aware (only files in current diff vs base)
* ``--full-scan`` → scan every ``scripts/ops/*.bat``
* Explicit paths → scan those (pre-commit pass-through)

Bypass (per lint-policy.md §4):
    Add to PR description body:
        bypass-lint: bat-ascii-purity
        reason: <≥30 words explaining why this case is legitimate>

Exit codes
----------
0 = all files OK (or bypass matched)
1 = one or more .bat files have violations
2 = diff base ref missing — fix CI workflow's fetch-depth or base ref
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Helpers from this lint family
sys.path.insert(0, str(Path(__file__).parent))
from _lint_helpers import (  # noqa: E402
    DiffBaseMissingError,
    parse_bypass_tag,
    resolve_diff_base,
)


def find_repo_root() -> Path:
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return Path(__file__).resolve().parents[3]


def scan_bat(path: Path) -> list[str]:
    """Return list of human-readable violation strings for a single .bat."""
    try:
        data = path.read_bytes()
    except OSError as e:
        return [f"{path}: cannot read ({e})"]

    violations: list[str] = []

    # 1. UTF-8 BOM check
    if data.startswith(b"\xef\xbb\xbf"):
        violations.append(
            f"{path}: starts with UTF-8 BOM (EF BB BF). cmd.exe reads the "
            "BOM bytes as part of the first command -- remove the BOM."
        )

    # 2. Non-ASCII byte check (pitfall #45)
    lines = data.split(b"\r\n") if b"\r\n" in data else data.split(b"\n")
    ascii_hits: list[tuple[int, int, int, str]] = []
    for ln, line in enumerate(lines, 1):
        for col, b in enumerate(line):
            if b >= 0x80:
                try:
                    preview = line.decode("utf-8", errors="replace")[:80]
                except Exception:
                    preview = repr(line[:80])
                ascii_hits.append((ln, col, b, preview))
                break  # one hit per line is enough for the report
    if ascii_hits:
        violations.append(
            f"{path}: {len(ascii_hits)} line(s) contain byte(s) >= 0x80 "
            "-- pitfall #45 forbids non-ASCII in scripts/ops/*.bat."
        )
        for ln, col, b, preview in ascii_hits[:5]:
            violations.append(f"  L{ln} col{col}: byte=0x{b:02x}  |  {preview}")
        if len(ascii_hits) > 5:
            violations.append(f"  ... and {len(ascii_hits) - 5} more")

    # 3. Bare LF (missing CR) check -- pitfall row #2
    bare_lf_lines: list[int] = []
    line_no = 1
    for i, b in enumerate(data):
        if b == 0x0A:
            if i == 0 or data[i - 1] != 0x0D:
                bare_lf_lines.append(line_no)
            line_no += 1
    if bare_lf_lines:
        violations.append(
            f"{path}: {len(bare_lf_lines)} bare LF line-ending(s) -- .bat "
            f"files must be CRLF (pitfall row #2). First offending line(s): "
            f"{bare_lf_lines[:5]}."
        )

    return violations


def _read_pr_body(pr_body_file: str | None) -> str | None:
    """Read PR body from --pr-body-file or $PR_BODY env var."""
    if pr_body_file:
        try:
            return Path(pr_body_file).read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError) as e:
            print(f"WARN: cannot read --pr-body-file {pr_body_file}: {e}", file=sys.stderr)
    return os.environ.get("PR_BODY") or None


def _diff_changed_bats(repo: Path, base: str) -> list[Path]:
    """Return scripts/ops/*.bat changed in current diff vs base."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=AM", base, "--",
             "scripts/ops/*.bat"],
            capture_output=True, text=True, cwd=str(repo),
            check=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    out: list[Path] = []
    for rel in result.stdout.splitlines():
        rel = rel.strip()
        if rel and rel.endswith(".bat"):
            full = repo / rel
            if full.is_file():
                out.append(full)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Enforce ASCII-only + CRLF + no-BOM for .bat files under "
            "scripts/ops/ (pitfall #45 + pitfall row #2). "
            "Companion to tests/dx/test_bat_label_integrity.py."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=(
            "Explicit file paths to check (pre-commit passes the staged .bat "
            "files here). If empty + no flag: diff-only. With --full-scan: "
            "all scripts/ops/*.bat."
        ),
    )
    parser.add_argument(
        "--full-scan", action="store_true",
        help="Scan every scripts/ops/*.bat (manual audit; ignores diff).",
    )
    parser.add_argument(
        "--diff-base", default=None,
        help="Override diff base (default: $LINT_DIFF_BASE / $GITHUB_BASE_REF / origin/main).",
    )
    parser.add_argument(
        "--pr-body-file", default=None,
        help="Path to file containing PR body for bypass tag check.",
    )
    args = parser.parse_args()

    repo = find_repo_root()

    # Determine which .bat files to scan
    if args.paths:
        bat_paths = [Path(p) for p in args.paths]
        scan_mode = "explicit-paths"
    elif args.full_scan:
        bat_paths = sorted((repo / "scripts" / "ops").glob("*.bat"))
        scan_mode = "full-scan"
    else:
        try:
            base = args.diff_base or resolve_diff_base()
        except DiffBaseMissingError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        bat_paths = _diff_changed_bats(repo, base)
        scan_mode = f"diff vs {base}"

    # Only apply the rule to scripts/ops/*.bat -- other .bat (e.g. dev-
    # container bind-mount scripts) don't go through Desktop Commander
    # start_process and aren't subject to pitfall #45.
    target_prefix = "scripts/ops/"
    filtered: list[Path] = []
    for p in bat_paths:
        try:
            rel = p.resolve().relative_to(repo).as_posix()
        except ValueError:
            rel = p.as_posix()
        if rel.startswith(target_prefix):
            filtered.append(p)

    if not filtered:
        # Nothing in scope -- silent pass (pre-commit may invoke us with
        # non-.bat files in edge cases; don't spam).
        return 0

    all_violations: list[str] = []
    for p in filtered:
        all_violations.extend(scan_bat(p))

    if not all_violations:
        return 0

    # Bypass check (lint-policy.md §4)
    pr_body = _read_pr_body(args.pr_body_file)
    bypass_reason = parse_bypass_tag(pr_body, "bat-ascii-purity")

    print(
        "[check_bat_ascii_purity] FAIL: pitfall #45 rule violated in "
        f"{len(filtered)} file(s) under scripts/ops/ (mode={scan_mode})",
        file=sys.stderr,
    )
    for line in all_violations:
        print(line, file=sys.stderr)

    if bypass_reason:
        print(
            f"\n⚠️  BYPASSED via PR body: {bypass_reason}\n"
            f"   {len(all_violations)} finding(s) above are author-acknowledged.\n"
            f"   Reviewer must confirm bypass is justified.",
            file=sys.stderr,
        )
        return 0

    print(
        "\n  Fix:\n"
        "    * Replace CJK headings in REM comments with ASCII prose, "
        'e.g. "see: \\"MCP Shell Pitfalls\\" section" instead of '
        '"§MCP Shell Pitfalls".\n'
        "    * Replace em-dash (\\u2014) with `--`.\n"
        "    * Re-save as CRLF (Write tool on Windows defaults to CRLF).\n"
        "    * Strip UTF-8 BOM if present.\n"
        "\n  Why it matters: Desktop Commander start_process spawns cmd.exe\n"
        "  with OEM codepage (cp950 / cp437), not cp65001. cmd's batch\n"
        "  parser reads byte-by-byte without UTF-8 normalization; a single\n"
        "  byte >= 0x80 corrupts parser state on downstream lines, silently\n"
        "  breaking @echo off / setlocal / goto.\n"
        "\n  See docs/internal/windows-mcp-playbook.md pitfall #45 for the\n"
        "  full byte-level analysis + dogfood proof.\n"
        "  Or add to PR description (per lint-policy.md §4):\n"
        "    bypass-lint: bat-ascii-purity\n"
        "    reason: <≥30 words explaining why this case is legitimate>\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
