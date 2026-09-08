#!/usr/bin/env python3
"""check_frontmatter_versions.py — Frontmatter version global scan

Scans all docs/**/*.md frontmatter 'version:' fields and compares against
the platform version declared in CLAUDE.md. Reports version drift.

Usage:
  python3 scripts/tools/lint/check_frontmatter_versions.py
  python3 scripts/tools/lint/check_frontmatter_versions.py --ci
  python3 scripts/tools/lint/check_frontmatter_versions.py --json
  python3 scripts/tools/lint/check_frontmatter_versions.py --fix
"""
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_confd import is_hidden_name  # noqa: E402
from _lib_python import write_text_or_die  # noqa: E402
from _lib_exitcodes import EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_versions import (  # noqa: E402
    read_platform_version as _read_shared_platform_version,
)

# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
DOCS_DIR = REPO_ROOT / "docs"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------
_FM_DELIM = re.compile(r"^---\s*$")
_VERSION_RE = re.compile(r"^version:\s*(.+)$", re.MULTILINE)


@dataclass
class FrontmatterInfo:
    """Parsed frontmatter metadata from a markdown file."""
    file_path: Path
    relative_path: str
    version: Optional[str] = None
    version_line: int = 0
    has_frontmatter: bool = False


@dataclass
class DriftItem:
    """A single version drift finding."""
    file: str
    line: int
    current_version: str
    expected_version: str
    severity: str = "error"  # "error" for mismatch, "warn" for missing

    def to_dict(self) -> Dict:
        return {
            "file": self.file,
            "line": self.line,
            "current_version": self.current_version,
            "expected_version": self.expected_version,
            "severity": self.severity,
        }


def read_platform_version() -> Optional[str]:
    """Read the platform version from CLAUDE.md, without the ``v`` prefix.

    Delegates to :func:`_lib_versions.read_platform_version`, which is the
    reader the doc-map / tool-map generators and the ``version-consistency``
    hook all share. ``None`` when CLAUDE.md is missing or neither spelling
    matches — the caller decides how to report that.

    ⛔ This used to be a fourth private copy of the same lookup, and it read
    CLAUDE.md the looser of the two this change collapses: anchor on the
    first line
    containing ``專案概覽``, then scan that line plus five more for a
    ``(vX.Y.Z)`` token. That shape trades one expiry date for three — an
    earlier mention of 專案概覽 (a table of contents entry, a cross-reference)
    wins the anchor; an admonition under the heading pushes the real lead-in
    out of the window; and ``[^)]*`` swallows any prose sharing the
    parentheses. It happened to be correct on the CLAUDE.md of the day, which
    is exactly how #1480's sibling copy stayed broken for five releases
    without anyone noticing. The shared reader matches two *precise* phrases
    instead, so distance to the heading stops mattering.
    """
    return _read_shared_platform_version(
        default="", claude_md=CLAUDE_MD).lstrip("v") or None


def extract_frontmatter(file_path: Path) -> FrontmatterInfo:
    """Extract frontmatter version from a markdown file."""
    rel = file_path.relative_to(REPO_ROOT) if file_path.is_relative_to(REPO_ROOT) else file_path
    info = FrontmatterInfo(file_path=file_path, relative_path=str(rel))

    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return info

    if not lines or not _FM_DELIM.match(lines[0]):
        return info

    info.has_frontmatter = True

    # Find closing delimiter
    for i, line in enumerate(lines[1:], start=2):
        if _FM_DELIM.match(line):
            # Parse frontmatter block
            fm_block = "\n".join(lines[1:i - 1])
            vm = _VERSION_RE.search(fm_block)
            if vm:
                raw = vm.group(1).strip().strip('"').strip("'")
                # Remove leading 'v' if present for normalization
                info.version = raw.lstrip("v") if raw.startswith("v") else raw
                # Find the actual line number
                for j, fml in enumerate(lines[1:i - 1], start=2):
                    if fml.strip().startswith("version:"):
                        info.version_line = j
                        break
            break

    return info


def scan_docs(docs_dir: Path) -> List[FrontmatterInfo]:
    """Scan all markdown files under docs/ for frontmatter versions.

    Hidden entries are judged on the path *below* ``docs_dir``, never on the
    absolute path. #1790: the previous test walked ``md_file.parts`` — every
    ancestor included — so a checkout living under any dot-directory
    (Claude Code's agent worktrees sit in ``.claude/worktrees/<name>/``)
    skipped the whole tree and reported a green "All 0 frontmatter versions
    match". The predicate is ``_lib_confd.is_hidden_name``.
    """
    results = []
    if not docs_dir.exists():
        return results

    for md_file in sorted(docs_dir.rglob("*.md")):
        # Skip hidden directories/files *inside* docs_dir only.
        rel_parts = md_file.relative_to(docs_dir).parts
        if any(is_hidden_name(part) for part in rel_parts):
            continue
        info = extract_frontmatter(md_file)
        results.append(info)

    return results


def detect_drift(
    scanned: List[FrontmatterInfo],
    expected_version: str,
) -> List[DriftItem]:
    """Compare scanned frontmatter versions against expected platform version."""
    items = []
    for info in scanned:
        if not info.has_frontmatter:
            continue  # Skip files without frontmatter entirely

        if info.version is None:
            # Has frontmatter but no version field — warning
            items.append(DriftItem(
                file=info.relative_path,
                line=1,
                current_version="(missing)",
                expected_version=expected_version,
                severity="warn",
            ))
        elif info.version != expected_version:
            items.append(DriftItem(
                file=info.relative_path,
                line=info.version_line,
                current_version=info.version,
                expected_version=expected_version,
                severity="error",
            ))

    return items


def fix_drift(items: List[DriftItem], expected_version: str) -> int:
    """Fix version drift by updating frontmatter version fields in-place."""
    fixed = 0
    for item in items:
        if item.severity != "error":
            continue  # Only fix mismatches, not missing
        file_path = REPO_ROOT / item.file
        if not file_path.exists():
            continue

        content = file_path.read_text(encoding="utf-8")
        # Replace version in frontmatter
        lines = content.splitlines(keepends=True)
        if item.line > 0 and item.line <= len(lines):
            old_line = lines[item.line - 1]
            new_line = re.sub(
                r"(version:\s*).*",
                rf"\g<1>v{expected_version}",
                old_line,
            )
            if old_line != new_line:
                lines[item.line - 1] = new_line
                # #1641: internal (repo-derived) path — no flag to name; an
                # unwritable doc is still rc=2, not a traceback at rc=1.
                write_text_or_die(str(file_path), "".join(lines))
                fixed += 1

    return fixed


def format_text_report(
    items: List[DriftItem],
    expected: str,
    total_scanned: int,
    total_with_fm: int,
    total_found: Optional[int] = None,
) -> str:
    """Format a human-readable text report.

    ``total_found`` is every ``*.md`` below docs/ before the hidden filter;
    printing it next to ``total_scanned`` is what lets a reader see a scan
    that silently shrank (#1790) without re-running ``find`` themselves.
    """
    lines = []
    lines.append(f"Platform version: v{expected}")
    if total_found is None:
        lines.append(f"Scanned: {total_scanned} files, {total_with_fm} with frontmatter")
    else:
        lines.append(f"Scanned: {total_scanned} of {total_found} markdown file(s) "
                     f"({total_found - total_scanned} hidden), "
                     f"{total_with_fm} with frontmatter")
    lines.append("")

    errors = [i for i in items if i.severity == "error"]
    warnings = [i for i in items if i.severity == "warn"]

    if errors:
        lines.append(f"Version mismatches ({len(errors)}):")
        for item in errors:
            lines.append(
                f"  ❌ {item.file}:{item.line} — "
                f"has v{item.current_version}, expected v{expected}"
            )
        lines.append("")

    if warnings:
        lines.append(f"Missing version field ({len(warnings)}):")
        for item in warnings:
            lines.append(f"  ⚠️  {item.file} — frontmatter has no version: field")
        lines.append("")

    if not items and total_with_fm == 0:
        # #1790: files were enumerated but none carries frontmatter, so no
        # comparison happened. rc stays 0 (frontmatter is per-file optional),
        # but the wording must not be the green "All 0 … match".
        lines.append(f"⚠️  0 of {total_scanned} files carry frontmatter — "
                     "nothing was compared")
    elif not items:
        lines.append(f"✅ All {total_with_fm} frontmatter versions match v{expected}")
    else:
        lines.append(f"Summary: {len(errors)} error(s), {len(warnings)} warning(s)")

    return "\n".join(lines)


def format_json_report(
    items: List[DriftItem],
    expected: str,
    total_scanned: int,
    total_with_fm: int,
    total_found: Optional[int] = None,
    status: str = "ok",
    reason: Optional[str] = None,
) -> str:
    """Format a JSON report.

    ``status``/``reason`` exist for the refused-empty-population path: in
    ``--json`` mode stdout must still be exactly one JSON document
    (dev-rules §13), so that path emits this shape zeroed out with
    ``status="caller_error"`` instead of prose.
    """
    errors = [i for i in items if i.severity == "error"]
    warnings = [i for i in items if i.severity == "warn"]
    result = {
        "status": status,
        "reason": reason,
        "expected_version": expected,
        "total_found": total_found if total_found is not None else total_scanned,
        "total_scanned": total_scanned,
        "total_with_frontmatter": total_with_fm,
        "items": [i.to_dict() for i in items],
        "summary": {
            "errors": len(errors),
            "warnings": len(warnings),
        },
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Scan frontmatter version: fields across all docs/*.md",
    )
    parser.add_argument("--ci", action="store_true",
                        help="Exit 1 on version mismatch errors")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON report")
    parser.add_argument("--fix", action="store_true",
                        help="Auto-fix version mismatches in-place")
    args = parser.parse_args(argv)

    expected = read_platform_version()
    if not expected:
        print("ERROR: Cannot read platform version from CLAUDE.md",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # #1790: an empty population is a caller error, not a clean pass.
    # "Scanned: 0 files" used to exit 0 — indistinguishable from "every file
    # matched" — and nothing could tell the two apart until #1492 wired this
    # tool into a runner. Say WHICH empty shape this is, because the
    # operator's own `find docs -name '*.md'` contradicts the wrong wording:
    # docs/ missing or a file, docs/ unreadable, no markdown at all, or
    # markdown present but every file hidden. The discriminating words come
    # FIRST: validate_all (`make lint-docs`) quotes only the last meaningful
    # stdout line, cut to 80 characters.
    reason: Optional[str] = None
    total_found = 0
    if not DOCS_DIR.is_dir():
        reason = f"not a directory: {DOCS_DIR}"
    elif not os.access(DOCS_DIR, os.R_OK):
        reason = f"not readable: {DOCS_DIR}"
    else:
        total_found = sum(1 for _ in DOCS_DIR.rglob("*.md"))
    scanned = scan_docs(docs_dir=DOCS_DIR) if reason is None else []
    if not scanned:
        if reason is None and total_found:
            reason = (f"all {total_found} markdown file(s) hidden (skipped by "
                      f"_lib_confd.is_hidden_name) under {DOCS_DIR}")
        elif reason is None:
            reason = f"no markdown files found under {DOCS_DIR}"
        msg = f"ERROR: {reason} — nothing was measured"
        print(msg, file=sys.stderr)
        if args.json:
            # dev-rules §13: --json stdout is exactly one JSON document on
            # every terminal path, early exits included.
            print(format_json_report([], expected, 0, 0, total_found=total_found,
                                     status="caller_error", reason=reason))
        else:
            print(msg)
        sys.exit(EXIT_CALLER_ERROR)
    total_scanned = len(scanned)
    total_with_fm = sum(1 for s in scanned if s.has_frontmatter)

    items = detect_drift(scanned, expected)

    if args.fix:
        fixed = fix_drift(items, expected)
        print(f"Fixed {fixed} file(s)")
        # Re-scan after fix
        scanned = scan_docs(docs_dir=DOCS_DIR)
        items = detect_drift(scanned, expected)

    if args.json:
        print(format_json_report(items, expected, total_scanned, total_with_fm,
                                 total_found=total_found))
    else:
        print(format_text_report(items, expected, total_scanned, total_with_fm,
                                 total_found=total_found))

    errors = [i for i in items if i.severity == "error"]
    if args.ci and errors:
        sys.exit(EXIT_VIOLATION)


if __name__ == "__main__":
    main()
