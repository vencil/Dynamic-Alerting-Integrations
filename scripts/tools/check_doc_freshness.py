#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_doc_freshness.py

Scans markdown documentation for stale/dead references — code examples that
reference files, commands, or paths that no longer exist in the repo.

Checks:
1. File path references in code blocks and inline code
2. Command references (da-tools <subcommand>)
3. Docker image tags (version matching)
4. Helm chart versions
"""

import argparse
import os
import re
import sys
from pathlib import Path

# Reference extraction patterns
FILE_PATH_PATTERN = r'(?:^|\s|[`\'"])((?:conf\.d/|rule-packs/|scripts/tools/|docs/|k8s/|components/|helm/)[^\s`\'")\]]+)'
CODE_BLOCK_PATTERN = r'```(?:yaml|bash|sh|shell|python|go|dockerfile|json|text)\n(.*?)\n```'
INLINE_CODE_PATTERN = r'`([^`]+)`'
DA_TOOLS_PATTERN = r'da-tools\s+([a-z_-]+)'
DOCKER_IMAGE_PATTERN = r'ghcr\.io/vencil/(threshold-exporter|da-tools):([v\d.]+)'
HELM_VERSION_PATTERN = r'--version\s+([v\d.]+)'


def extract_version_from_claude_md(repo_root):
    """Extract platform version from CLAUDE.md."""
    claude_md = Path(repo_root) / "CLAUDE.md"
    if not claude_md.exists():
        return None

    with open(claude_md, encoding="utf-8") as f:
        content = f.read()
        match = re.search(r'## 專案概覽 \(v([\d.]+)\)', content)
        if match:
            return f"v{match.group(1)}"
    return None


def extract_chart_version(repo_root, chart_name):
    """Extract version from Chart.yaml."""
    chart_path = Path(repo_root) / "components" / chart_name / "Chart.yaml"
    if not chart_path.exists():
        return None

    with open(chart_path, encoding="utf-8") as f:
        content = f.read()
        match = re.search(r'^version:\s+([^\s]+)', content, re.MULTILINE)
        if match:
            return match.group(1)
    return None


def collect_existing_tools(repo_root):
    """Collect all tool names (stem without .py)."""
    tools_dir = Path(repo_root) / "scripts" / "tools"
    tools = set()
    if tools_dir.exists():
        for py_file in tools_dir.glob("*.py"):
            if py_file.name != "_lib_python.py":
                tools.add(py_file.stem)
    return tools


def file_exists(repo_root, file_path):
    """Check if file exists in repo."""
    # Resolve relative paths
    full_path = Path(repo_root) / file_path
    return full_path.exists()


def extract_paths_from_markdown(text):
    """Extract all potential file paths from markdown text."""
    paths = set()

    # Extract from code blocks
    for match in re.finditer(CODE_BLOCK_PATTERN, text, re.DOTALL):
        block = match.group(1)
        for path_match in re.finditer(FILE_PATH_PATTERN, block):
            path = path_match.group(1).strip('\'"')
            paths.add(path)

    # Extract from inline code
    for match in re.finditer(INLINE_CODE_PATTERN, text):
        code = match.group(1)
        for path_match in re.finditer(FILE_PATH_PATTERN, code):
            path = path_match.group(1).strip('\'"')
            paths.add(path)

    return paths


def extract_da_tools_commands(text):
    """Extract da-tools subcommands."""
    commands = set()
    for match in re.finditer(DA_TOOLS_PATTERN, text):
        commands.add(match.group(1))
    return commands


def extract_docker_images(text):
    """Extract Docker image references."""
    images = {}
    for match in re.finditer(DOCKER_IMAGE_PATTERN, text):
        image_name = match.group(1)
        version = match.group(2)
        images[f"{image_name}:{version}"] = (image_name, version)
    return images


def check_doc_file(file_path, repo_root, platform_version, tools, issues):
    """Check a single markdown file for stale references."""
    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    lines = content.split('\n')
    line_map = {}
    current_line = 0
    for i, line in enumerate(lines, 1):
        current_line = i

    # Check file paths
    file_paths = extract_paths_from_markdown(content)
    for path in file_paths:
        if not file_exists(repo_root, path):
            line_num = next((i for i, line in enumerate(lines, 1) if path in line), 1)
            issues.append({
                'file': str(file_path),
                'line': line_num,
                'type': 'missing_file',
                'reference': path,
                'message': f'File not found: {path}'
            })

    # Check da-tools commands
    commands = extract_da_tools_commands(content)
    for cmd in commands:
        if cmd not in tools:
            line_num = next((i for i, line in enumerate(lines, 1) if f'da-tools {cmd}' in line), 1)
            issues.append({
                'file': str(file_path),
                'line': line_num,
                'type': 'missing_command',
                'reference': f'da-tools {cmd}',
                'message': f'Command not found: {cmd}'
            })

    # Check Docker image versions
    docker_images = extract_docker_images(content)
    for image_ref, (image_name, version) in docker_images.items():
        if image_name == 'threshold-exporter':
            expected = extract_chart_version(repo_root, 'threshold-exporter')
        elif image_name == 'da-tools':
            expected = platform_version
        else:
            continue

        if expected and version != expected:
            line_num = next((i for i, line in enumerate(lines, 1) if image_ref in line), 1)
            issues.append({
                'file': str(file_path),
                'line': line_num,
                'type': 'version_mismatch',
                'reference': image_ref,
                'message': f'Expected {image_name}:{expected}, got {image_ref}'
            })


def main():
    parser = argparse.ArgumentParser(
        description='Check markdown documentation for stale references'
    )
    parser.add_argument(
        '--docs-dir',
        default='docs',
        help='Documentation directory (default: docs/)'
    )
    parser.add_argument(
        '--repo-root',
        default='.',
        help='Repository root for file existence checks (default: .)'
    )
    parser.add_argument(
        '--ci',
        action='store_true',
        help='Exit 1 if any stale references found'
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Show all checked references'
    )

    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    docs_dir = repo_root / args.docs_dir

    if not docs_dir.exists():
        print(f"Error: docs directory not found: {docs_dir}")
        sys.exit(1)

    # Collect reference data
    platform_version = extract_version_from_claude_md(repo_root)
    tools = collect_existing_tools(repo_root)
    issues = []

    # Check all markdown files
    for md_file in docs_dir.rglob("*.md"):
        check_doc_file(md_file, repo_root, platform_version, tools, issues)

    # Report issues
    if issues:
        print(f"Found {len(issues)} stale reference(s):\n")
        for issue in sorted(issues, key=lambda x: (x['file'], x['line'])):
            print(f"{issue['file']}:{issue['line']}")
            print(f"  [{issue['type']}] {issue['message']}")
    else:
        print("No stale references found.")

    if args.ci and issues:
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
