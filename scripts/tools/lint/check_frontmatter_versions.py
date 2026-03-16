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
from _lib_python import write_text_secure  # noqa: E402

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
    """Read the platform version from CLAUDE.md frontmatter / header."""
    if not CLAUDE_MD.exists():
        return None
    content = CLAUDE_MD.read_text(encoding="utf-8")
    m = re.search(r"專案概覽 \(v([0-9]+\.[0-9]+\.[0-9]+[^)]*)\)", content)
    if m:
        return m.group(1)
    return None


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
    """Scan all markdown files under docs/ for frontmatter versions."""
    results = []
    if not docs_dir.exists():
        return results

    for md_file in sorted(docs_dir.rglob("*.md")):
        # Skip hidden directories
        if any(part.startswith(".") for part in md_file.parts):
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
                write_text_secure(str(file_path), "".join(lines))
                fixed += 1

    return fixed


def format_text_report(
    items: List[DriftItem],
    expected: str,
    total_scanned: int,
    total_with_fm: int,
) -> str:
    """Format a human-readable text report."""
    lines = []
    lines.append(f"Platform version: v{expected}")
    lines.append(f"Scanned: {total_scanned} files, {total_with_fm} with frontmatter")
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

    if not items:
        lines.append(f"✅ All {total_with_fm} frontmatter versions match v{expected}")
    else:
        lines.append(f"Summary: {len(errors)} error(s), {len(warnings)} warning(s)")

    return "\n".join(lines)


def format_json_report(
    items: List[DriftItem],
    expected: str,
    total_scanned: int,
    total_with_fm: int,
) -> str:
    """Format a JSON report."""
    errors = [i for i in items if i.severity == "error"]
    warnings = [i for i in items if i.severity == "warn"]
    result = {
        "expected_version": expected,
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
        sys.exit(1)

    scanned = scan_docs(docs_dir=DOCS_DIR)
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
        print(format_json_report(items, expected, total_scanned, total_with_fm))
    else:
        print(format_text_report(items, expected, total_scanned, total_with_fm))

    errors = [i for i in items if i.severity == "error"]
    if args.ci and errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
