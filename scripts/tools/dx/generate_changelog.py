#!/usr/bin/env python3
"""Generate CHANGELOG draft entries from conventional commits.

Usage:
    # Draft from last tag to HEAD
    generate_changelog.py

    # Draft from specific tag
    generate_changelog.py --since v1.12.0

    # Draft with version label
    generate_changelog.py --version v1.13.0

    # Output as markdown file
    generate_changelog.py --version v1.13.0 -o changelog-draft.md

    # Check mode: verify all commits since tag follow conventional format
    generate_changelog.py --check
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# Add script dir to path for lib imports
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo tools root
from _lib_python import write_text_secure  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────

# Conventional commit type → display section
TYPE_SECTIONS: Dict[str, str] = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "test": "Tests",
    "build": "Build & Dependencies",
    "ci": "CI/CD",
    "chore": "Chores",
    "style": "Style",
    "revert": "Reverts",
}

# Emoji prefixes matching existing CHANGELOG style
TYPE_EMOJI: Dict[str, str] = {
    "feat": "🏷️",
    "fix": "🐛",
    "perf": "📈",
    "refactor": "♻️",
    "docs": "📝",
    "test": "📊",
    "build": "📦",
    "ci": "🔧",
    "chore": "🔧",
}

# Conventional commit regex
COMMIT_RE = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[^)]+)\))?"
    r"(?P<breaking>!)?"
    r":\s*(?P<desc>.+)$"
)


# ── Git helpers ──────────────────────────────────────────────────────

def git_cmd(args: List[str]) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        print(f"ERROR: git {' '.join(args)} failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def get_latest_tag() -> Optional[str]:
    """Get the most recent tag."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def get_commits_since(since_ref: Optional[str]) -> List[Tuple[str, str]]:
    """Return list of (hash, subject) tuples since a ref."""
    if since_ref:
        log_range = f"{since_ref}..HEAD"
    else:
        log_range = "HEAD"
    raw = git_cmd(["log", log_range, "--pretty=format:%H|%s", "--no-merges"])
    if not raw:
        return []
    commits = []
    for line in raw.split("\n"):
        parts = line.split("|", 1)
        if len(parts) == 2:
            commits.append((parts[0][:12], parts[1]))
    return commits


# ── Parsing ──────────────────────────────────────────────────────────

def parse_commit(subject: str) -> Optional[Dict]:
    """Parse a conventional commit subject line."""
    m = COMMIT_RE.match(subject)
    if not m:
        return None
    return {
        "type": m.group("type"),
        "scope": m.group("scope") or "",
        "breaking": bool(m.group("breaking")),
        "desc": m.group("desc"),
    }


# ── Formatting ───────────────────────────────────────────────────────

def format_changelog(
    grouped: Dict[str, List[Dict]],
    version: str,
    breaking: List[Dict],
) -> str:
    """Format grouped commits into CHANGELOG markdown."""
    lines = []
    lines.append(f"## [{version}] — TITLE (DATE)")
    lines.append("")
    lines.append("ONE-LINE SUMMARY")
    lines.append("")

    # Breaking changes first
    if breaking:
        lines.append("### ⚠️ Breaking Changes")
        lines.append("")
        for c in breaking:
            scope = f"**{c['scope']}**: " if c["scope"] else ""
            lines.append(f"- {scope}{c['desc']}")
        lines.append("")

    # Grouped sections
    for commit_type, section_name in TYPE_SECTIONS.items():
        if commit_type not in grouped:
            continue
        emoji = TYPE_EMOJI.get(commit_type, "")
        header = f"### {emoji} {section_name}" if emoji else f"### {section_name}"
        lines.append(header)
        lines.append("")

        # Sub-group by scope
        by_scope: Dict[str, List[str]] = defaultdict(list)
        for c in grouped[commit_type]:
            by_scope[c["scope"]].append(c["desc"])

        if len(by_scope) == 1 and "" in by_scope:
            # No scopes, flat list
            for desc in by_scope[""]:
                lines.append(f"- {desc}")
        else:
            for scope, descs in sorted(by_scope.items()):
                if scope:
                    lines.append(f"- **{scope}**:")
                    for desc in descs:
                        lines.append(f"  - {desc}")
                else:
                    for desc in descs:
                        lines.append(f"- {desc}")
        lines.append("")

    return "\n".join(lines)


# ── CHANGELOG Lint ───────────────────────────────────────────────────

def lint_changelog(changelog_path: str = "CHANGELOG.md") -> List[str]:
    """Lint the existing CHANGELOG.md for format issues.

    Checks:
    - Each version header has semver format [vX.Y.Z...]
    - Date format is YYYY-MM-DD in parenthetical suffix
    - Each version section has at least one ### subsection
    - No duplicate version entries
    Returns list of issue strings (empty = clean).
    """
    from pathlib import Path
    path = Path(changelog_path)
    if not path.exists():
        return [f"{changelog_path} not found"]

    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    issues: List[str] = []
    seen_versions: Dict[str, int] = {}
    current_version: Optional[str] = None
    has_subsection = False

    semver_re = re.compile(
        r"^\#\# \[v?(\d+\.\d+\.\d+(?:-[a-zA-Z0-9._-]+)?)\]"
    )
    date_re = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")
    subsection_re = re.compile(r"^### ")

    for i, line in enumerate(lines, 1):
        vm = semver_re.match(line)
        if vm:
            # Check previous version had subsections
            if current_version and not has_subsection:
                issues.append(
                    f"L{seen_versions[current_version]}: [{current_version}] "
                    f"has no ### subsections"
                )

            ver = vm.group(1)
            if ver in seen_versions:
                issues.append(
                    f"L{i}: duplicate version [{ver}] "
                    f"(first at L{seen_versions[ver]})"
                )
            seen_versions[ver] = i
            current_version = ver
            has_subsection = False

            # Check date
            dm = date_re.search(line)
            if not dm:
                issues.append(f"L{i}: [{ver}] missing date (YYYY-MM-DD)")

        if subsection_re.match(line) and current_version:
            has_subsection = True

    # Last version
    if current_version and not has_subsection:
        issues.append(
            f"L{seen_versions[current_version]}: [{current_version}] "
            f"has no ### subsections"
        )

    return issues


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    """CLI entry point: Generate CHANGELOG draft entries from conventional commits."""
    parser = argparse.ArgumentParser(
        description="Generate CHANGELOG draft from conventional commits"
    )
    parser.add_argument(
        "--since",
        help="Git ref (tag/commit) to start from (default: latest tag)",
    )
    parser.add_argument(
        "--version",
        default="UNRELEASED",
        help="Version label for the changelog section",
    )
    parser.add_argument(
        "-o", "--output",
        help="Write output to file instead of stdout",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check mode: verify all commits follow conventional format",
    )
    parser.add_argument(
        "--lint",
        action="store_true",
        help="Lint CHANGELOG.md format (semver headers, dates, subsections)",
    )
    args = parser.parse_args()

    # Lint mode — validate existing CHANGELOG format
    if args.lint:
        issues = lint_changelog("CHANGELOG.md")
        if issues:
            print(f"❌ {len(issues)} CHANGELOG format issue(s):")
            for issue in issues:
                print(f"  {issue}")
            return 1
        print("✅ CHANGELOG.md format is clean")
        return 0

    # Determine starting point
    since_ref = args.since
    if not since_ref:
        since_ref = get_latest_tag()
        if since_ref:
            print(f"Using latest tag: {since_ref}", file=sys.stderr)
        else:
            print("No tags found, reading all commits", file=sys.stderr)

    # Get commits
    commits = get_commits_since(since_ref)
    if not commits:
        print("No commits found since reference point.", file=sys.stderr)
        return 0

    print(f"Found {len(commits)} commits", file=sys.stderr)

    # Parse
    grouped: Dict[str, List[Dict]] = defaultdict(list)
    breaking: List[Dict] = []
    non_conventional: List[Tuple[str, str]] = []

    for sha, subject in commits:
        parsed = parse_commit(subject)
        if parsed:
            grouped[parsed["type"]].append(parsed)
            if parsed["breaking"]:
                breaking.append(parsed)
        else:
            non_conventional.append((sha, subject))

    # Check mode
    if args.check:
        if non_conventional:
            print(f"\n❌ {len(non_conventional)} non-conventional commits:", file=sys.stderr)
            for sha, subject in non_conventional:
                print(f"  {sha} {subject}", file=sys.stderr)
            return 1
        print(f"✅ All {len(commits)} commits follow conventional format", file=sys.stderr)
        return 0

    # Generate
    output = format_changelog(grouped, args.version, breaking)

    # Append non-conventional commits as uncategorized
    if non_conventional:
        output += "\n### Uncategorized\n\n"
        for sha, subject in non_conventional:
            output += f"- {sha} {subject}\n"
        output += "\n"

    # Stats
    stats = ", ".join(
        f"{TYPE_SECTIONS.get(t, t)}: {len(cs)}"
        for t, cs in sorted(grouped.items())
        if cs
    )
    output += f"<!-- Stats: {len(commits)} commits ({stats}) -->\n"

    if args.output:
        write_text_secure(args.output, output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
