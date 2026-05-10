#!/usr/bin/env python3
"""check_repo_name.py — Prevent wrong repository name in source files.

Scans source files for occurrences of the old/local workspace name
'vibe-k8s-lab' in GitHub URLs, ensuring only the correct public repo
name 'Dynamic-Alerting-Integrations' is used.

Allowed exceptions:
  - Docker/devcontainer paths like /workspaces/vibe-k8s-lab (local dev)
  - Comments explicitly documenting the rename
  - This script itself

Lint class: (b) per docs/internal/lint-policy.md (negative pattern +
false-positive escape allowlist). Default scan scope: **diff-only** —
only lines ADDED in current diff vs base are checked, so a contributor
touching a file that contains pre-existing legitimate exceptions doesn't
get flagged. Override with --full-scan for periodic manual audit.

Usage:
    # Diff-only (default; CI sets LINT_DIFF_BASE / GITHUB_BASE_REF)
    python3 scripts/tools/lint/check_repo_name.py [--ci]

    # Full repo scan (e.g., after merging large branch; --fix only works here)
    python3 scripts/tools/lint/check_repo_name.py --full-scan [--ci] [--fix]

Bypass (per lint-policy.md §4):
    Add to PR description body:
        bypass-lint: repo-name
        reason: <≥30 words explaining why this case is legitimate>
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Make stdout tolerate non-ASCII on Windows shells (cp950, cp1252).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Helpers from this lint family
sys.path.insert(0, str(Path(__file__).parent))
from _lint_helpers import (  # noqa: E402
    DiffBaseMissingError,
    get_diff_added_lines,
    parse_bypass_tag,
    resolve_diff_base,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Pattern: github.com/vencil/vibe-k8s-lab (wrong public repo name)
WRONG_PATTERN = re.compile(r'github\.com/vencil/vibe-k8s-lab')

# Allowed contexts (false positives)
ALLOWED_PATTERNS = [
    re.compile(r'/workspaces/vibe-k8s-lab'),     # Docker dev container path
    re.compile(r'vibe-k8s-lab-cluster'),          # Kind cluster name
    re.compile(r'dynamic-alerting-cluster'),       # Kind cluster name
]

CORRECT_REPO = 'Dynamic-Alerting-Integrations'
WRONG_REPO_URL = 'github.com/vencil/vibe-k8s-lab'
CORRECT_REPO_URL = f'github.com/vencil/{CORRECT_REPO}'

# File extensions to scan
SCAN_EXTENSIONS = {
    '.md', '.yaml', '.yml', '.py', '.jsx', '.html', '.json',
    '.toml', '.cfg', '.ini', '.sh', '.bash',
}

# Directories to skip
SKIP_DIRS = {
    '.git', 'node_modules', '__pycache__', 'site', '.venv',
    'vendor', '.mypy_cache', '.pytest_cache', 'tests',
}

# Files to skip (this script itself documents the wrong name)
SKIP_FILES = {
    'check_repo_name.py',
}


def _line_violates(line):
    """Return True if line contains WRONG_PATTERN and is NOT in an allowed context."""
    if not WRONG_PATTERN.search(line):
        return False
    return not any(ap.search(line) for ap in ALLOWED_PATTERNS)


# Backward-compat alias for tests/lint/test_check_repo_name.py — the
# test imports scan_file directly. Keep both names so existing tests
# don't break; new code prefers scan_file_full for clarity vs scan_file_diff.
def scan_file_full(filepath, fix=False):
    """Full-file scan: returns list of (line_num, line). If fix=True, rewrites file."""
    violations = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except (UnicodeDecodeError, PermissionError):
        return []

    fixed_lines = []
    changed = False
    for i, line in enumerate(lines, 1):
        if _line_violates(line):
            violations.append((i, line.rstrip()))
            if fix:
                line = line.replace(WRONG_REPO_URL, CORRECT_REPO_URL)
                changed = True
        fixed_lines.append(line)

    if fix and changed:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.writelines(fixed_lines)
        os.chmod(filepath, 0o644)

    return violations


def scan_file_diff(filepath, base):
    """Diff-only scan: returns list of (line_num, line) for ADDED lines violating pattern."""
    try:
        added_lines = get_diff_added_lines(Path(filepath), base)
    except subprocess.CalledProcessError:
        # Git error — fall back to full scan
        return scan_file_full(filepath, fix=False)

    violations = []
    for line_no, line in added_lines:
        if _line_violates(line):
            violations.append((line_no, line.rstrip()))
    return violations


def iter_scan_targets(full_scan, base):
    """Yield (filepath, rel_path) tuples for files to scan.

    Full-scan walks REPO_ROOT honoring SKIP_DIRS / SKIP_FILES / SCAN_EXTENSIONS.
    Diff-only asks git for changed files in current diff vs base, then applies
    the same filters.
    """
    if full_scan:
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if fname in SKIP_FILES:
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext not in SCAN_EXTENSIONS:
                    continue
                filepath = os.path.join(root, fname)
                rel = os.path.relpath(filepath, REPO_ROOT)
                yield filepath, rel
    else:
        # Diff-only: ask git for changed files
        try:
            result = subprocess.run(
                ['git', 'diff', '--name-only', base, '--diff-filter=AM'],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
                check=True, timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return
        for rel in result.stdout.splitlines():
            if not rel.strip():
                continue
            fname = os.path.basename(rel)
            if fname in SKIP_FILES:
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SCAN_EXTENSIONS:
                continue
            # Prune skipped directories
            parts = rel.replace('\\', '/').split('/')
            if any(p in SKIP_DIRS for p in parts):
                continue
            filepath = os.path.join(REPO_ROOT, rel)
            if not os.path.isfile(filepath):  # deleted in diff
                continue
            yield filepath, rel


def _read_pr_body(pr_body_file):
    """Read PR body from --pr-body-file or $PR_BODY env var."""
    if pr_body_file:
        try:
            return Path(pr_body_file).read_text(encoding='utf-8')
        except (FileNotFoundError, PermissionError) as e:
            print(f'WARN: cannot read --pr-body-file {pr_body_file}: {e}', file=sys.stderr)
    return os.environ.get('PR_BODY') or None


def main():
    """CLI entry point: Prevent wrong repository name in source files."""
    parser = argparse.ArgumentParser(
        description='Check for wrong repository name in source files'
    )
    parser.add_argument(
        '--ci', action='store_true',
        help='Exit with code 1 if violations found'
    )
    parser.add_argument(
        '--fix', action='store_true',
        help='Auto-fix violations (only with --full-scan; partial-line rewrites in diff are unsafe)'
    )
    parser.add_argument(
        '--full-scan', action='store_true',
        help='Scan entire repo (default is diff-only — recommended for CI).'
    )
    parser.add_argument(
        '--diff-base', default=None,
        help='Override diff base (default: $LINT_DIFF_BASE / $GITHUB_BASE_REF / origin/main).'
    )
    parser.add_argument(
        '--pr-body-file', default=None,
        help='Path to file containing PR body for bypass tag check.'
    )
    args = parser.parse_args()

    if args.fix and not args.full_scan:
        print(
            'ERROR: --fix requires --full-scan (partial-line rewrites in '
            'a diff context are unsafe).', file=sys.stderr,
        )
        return 2

    # Resolve scan mode
    if args.full_scan:
        scan_mode = 'full-repo'
        base = None
    else:
        try:
            base = args.diff_base or resolve_diff_base()
        except DiffBaseMissingError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return 2
        scan_mode = f'diff vs {base}'

    total_violations = 0
    files_with_violations = 0

    for filepath, rel_path in iter_scan_targets(args.full_scan, base):
        if args.full_scan:
            violations = scan_file_full(filepath, fix=args.fix)
        else:
            violations = scan_file_diff(filepath, base)
        if violations:
            files_with_violations += 1
            total_violations += len(violations)
            action = 'FIXED' if args.fix else 'ERROR'
            for line_num, line_text in violations:
                print(f'  {action}: {rel_path}:{line_num}: {line_text}')

    # Bypass check (lint-policy.md §4)
    pr_body = _read_pr_body(args.pr_body_file)
    bypass_reason = parse_bypass_tag(pr_body, 'repo-name')

    if total_violations == 0:
        print(f'OK no wrong repo name found (mode={scan_mode}). All URLs use {CORRECT_REPO}.')
        return 0

    if args.fix:
        print(
            f'\n✓ Fixed {total_violations} occurrence(s) in '
            f'{files_with_violations} file(s).'
        )
        print(f'  Replaced: {WRONG_REPO_URL} → {CORRECT_REPO_URL}')
        return 0

    if bypass_reason:
        print(
            f'\n⚠️  BYPASSED via PR body: {bypass_reason}\n'
            f'   {total_violations} finding(s) above are author-acknowledged.\n'
            f'   Reviewer must confirm bypass is justified.'
        )
        return 0

    print(
        f'\n✗ Found {total_violations} occurrence(s) of wrong repo '
        f'name in {files_with_violations} file(s) (mode={scan_mode}).'
    )
    print(f'  Wrong:   {WRONG_REPO_URL}')
    print(f'  Correct: {CORRECT_REPO_URL}')
    print(
        f'\n  Run with --full-scan --fix to auto-correct, or manually replace.'
        f'\n  Or add to PR description (per lint-policy.md §4):'
        f'\n    bypass-lint: repo-name'
        f'\n    reason: <≥30 words explaining why this is legitimate>'
    )
    if args.ci:
        return 1
    return 0


# Backward-compat alias (see comment above scan_file_full).
scan_file = scan_file_full


if __name__ == '__main__':
    sys.exit(main())
