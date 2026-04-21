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

Exit codes
----------
0 = all files OK
1 = one or more .bat files have violations
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


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
            "files here). If empty, scans all scripts/ops/*.bat."
        ),
    )
    args = parser.parse_args()

    repo = find_repo_root()
    if args.paths:
        bat_paths = [Path(p) for p in args.paths]
    else:
        bat_paths = sorted((repo / "scripts" / "ops").glob("*.bat"))

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

    print(
        "[check_bat_ascii_purity] FAIL: pitfall #45 rule violated in "
        f"{len(filtered)} file(s) under scripts/ops/",
        file=sys.stderr,
    )
    for line in all_violations:
        print(line, file=sys.stderr)
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
        "  full byte-level analysis + dogfood proof.\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
