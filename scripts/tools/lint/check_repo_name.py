#!/usr/bin/env python3
"""check_repo_name.py — Prevent wrong repository name in source files.

Scans source files for occurrences of the old/local workspace name
'vibe-k8s-lab' in GitHub URLs, ensuring only the correct public repo
name 'Dynamic-Alerting-Integrations' is used.

Allowed exceptions:
  - Docker/devcontainer paths like /workspaces/vibe-k8s-lab (local dev)
  - Comments explicitly documenting the rename
  - This script itself

Usage:
    python3 scripts/tools/check_repo_name.py [--ci] [--fix]
"""
import os
import re
import sys
import argparse
from pathlib import Path

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
    'vendor', '.mypy_cache', '.pytest_cache',
}

# Files to skip (this script itself documents the wrong name)
SKIP_FILES = {
    'check_repo_name.py',
}


def scan_file(filepath, fix=False):
    """Scan a single file for wrong repo name. Returns list of (line_num, line)."""
    violations = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except (UnicodeDecodeError, PermissionError):
        return []

    fixed_lines = []
    changed = False

    for i, line in enumerate(lines, 1):
        if WRONG_PATTERN.search(line):
            # Check if it's an allowed context
            is_allowed = any(ap.search(line) for ap in ALLOWED_PATTERNS)
            if not is_allowed:
                violations.append((i, line.rstrip()))
                if fix:
                    line = line.replace(WRONG_REPO_URL, CORRECT_REPO_URL)
                    changed = True
        fixed_lines.append(line)

    if fix and changed:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.writelines(fixed_lines)

    return violations


def main():
    parser = argparse.ArgumentParser(
        description='Check for wrong repository name in source files'
    )
    parser.add_argument(
        '--ci', action='store_true',
        help='Exit with code 1 if violations found'
    )
    parser.add_argument(
        '--fix', action='store_true',
        help='Auto-fix violations (replace wrong name with correct name)'
    )
    args = parser.parse_args()

    total_violations = 0
    files_with_violations = 0

    for root, dirs, files in os.walk(REPO_ROOT):
        # Prune skipped directories
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for fname in files:
            if fname in SKIP_FILES:
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SCAN_EXTENSIONS:
                continue

            filepath = os.path.join(root, fname)
            rel_path = os.path.relpath(filepath, REPO_ROOT)

            violations = scan_file(filepath, fix=args.fix)
            if violations:
                files_with_violations += 1
                total_violations += len(violations)
                action = 'FIXED' if args.fix else 'ERROR'
                for line_num, line_text in violations:
                    print(f'  {action}: {rel_path}:{line_num}: {line_text}')

    if total_violations > 0:
        if args.fix:
            print(
                f'\n✓ Fixed {total_violations} occurrence(s) in '
                f'{files_with_violations} file(s).'
            )
            print(
                f'  Replaced: {WRONG_REPO_URL} → {CORRECT_REPO_URL}'
            )
        else:
            print(
                f'\n✗ Found {total_violations} occurrence(s) of wrong repo '
                f'name in {files_with_violations} file(s).'
            )
            print(
                f'  Wrong:   {WRONG_REPO_URL}'
            )
            print(
                f'  Correct: {CORRECT_REPO_URL}'
            )
            print(
                f'\n  Run with --fix to auto-correct, or manually replace.'
            )
            if args.ci:
                sys.exit(1)
    else:
        print(f'✓ No wrong repo name found. All URLs use {CORRECT_REPO}.')


if __name__ == '__main__':
    main()
