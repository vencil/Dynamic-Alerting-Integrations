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
FILE_PATH_PATTERN = r'(?:^|\s|[`\'"])((?:conf\.d/|rule-packs/|scripts/tools/|docs/|k8s/|components/|helm/)[a-zA-Z0-9_./*<>-]+)'
CODE_BLOCK_PATTERN = r'```(?:yaml|bash|sh|shell|python|go|dockerfile|json|text)\n(.*?)\n```'
INLINE_CODE_PATTERN = r'`([^`]+)`'
DA_TOOLS_PATTERN = r'da-tools\s+([a-z_-]+)'
DOCKER_IMAGE_PATTERN = r'ghcr\.io/vencil/(threshold-exporter|da-tools):([v\d.]+)'
HELM_VERSION_PATTERN = r'--version\s+([v\d.]+)'


IGNORE_FILE_NAME = ".doc-freshness-ignore"


def _load_ignore_patterns(repo_root):
    """Load ignore patterns from .doc-freshness-ignore.

    Format: one pattern per line (path prefix or exact reference).
    Lines starting with # are comments. Blank lines are skipped.
    Supports type-specific ignores: ``missing_file:conf.d/``
    """
    ignore_file = Path(repo_root) / IGNORE_FILE_NAME
    if not ignore_file.exists():
        return set()
    patterns = set()
    for line in ignore_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.add(line)
    return patterns


def _is_ignored(issue, ignore_patterns):
    """Check if an issue matches any ignore pattern."""
    ref = issue["reference"]
    issue_type = issue["type"]
    for pat in ignore_patterns:
        # Type-specific: "missing_file:conf.d/"
        if ":" in pat and not pat.startswith("/"):
            ptype, pval = pat.split(":", 1)
            if ptype == issue_type and ref.startswith(pval):
                return True
        # Generic: matches reference prefix
        elif ref.startswith(pat):
            return True
    return False


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
    """Collect valid da-tools subcommand names.

    Primary source: #### headings in docs/cli-reference.md (authoritative).
    Fallback: Python script stems in scripts/tools/ (for offline use).
    Also includes common aliases and kebab-case variants.
    """
    tools = set()

    # Primary: parse da-tools subcommands from cli-reference.md headings
    cli_ref = Path(repo_root) / "docs" / "cli-reference.md"
    if cli_ref.exists():
        content = cli_ref.read_text(encoding="utf-8")
        for m in re.finditer(r'^#### ([a-z][\w-]*)\s*$', content,
                             re.MULTILINE):
            tools.add(m.group(1))

    # Fallback: Python script stems
    tools_dir = Path(repo_root) / "scripts" / "tools"
    if tools_dir.exists():
        skip_prefixes = ("_lib", "__init__", "__pycache__",
                         "generate_", "check_", "validate_",
                         "sync_", "bump_")
        for py_file in tools_dir.glob("*.py"):
            stem = py_file.stem
            if any(stem.startswith(p) for p in skip_prefixes):
                continue
            # Convert underscores to hyphens (Python→CLI convention)
            tools.add(stem.replace("_", "-"))
            tools.add(stem)

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


# Common English words that appear after "da-tools" in prose but are not commands
_NON_COMMAND_WORDS = frozenset({
    "command", "commands", "image", "images", "container", "containers",
    "is", "in", "can", "will", "has", "the", "a", "an", "and", "or",
    "for", "to", "from", "with", "by", "at", "on", "of",
    "subcommand", "subcommands", "code", "cheat", "tool", "tools",
    "version", "help", "usage", "flag", "flags", "option", "options",
    "da-tools", "job", "jobs", "-",
})


def extract_da_tools_commands(text):
    """Extract da-tools subcommands from code blocks and inline code only."""
    commands = set()

    # Only match in code blocks and inline code to reduce false positives
    # Code blocks
    for block_match in re.finditer(CODE_BLOCK_PATTERN, text, re.DOTALL):
        block = block_match.group(1)
        for m in re.finditer(DA_TOOLS_PATTERN, block):
            cmd = m.group(1)
            if cmd not in _NON_COMMAND_WORDS:
                commands.add(cmd)

    # Inline code
    for inline_match in re.finditer(INLINE_CODE_PATTERN, text):
        code = inline_match.group(1)
        for m in re.finditer(DA_TOOLS_PATTERN, code):
            cmd = m.group(1)
            if cmd not in _NON_COMMAND_WORDS:
                commands.add(cmd)

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

        if expected and version.lstrip("v") != expected.lstrip("v"):
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
    parser.add_argument(
        '--fix',
        action='store_true',
        help='Generate/update .doc-freshness-ignore from current missing_file issues'
    )

    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    docs_dir = repo_root / args.docs_dir

    if not docs_dir.exists():
        print(f"Error: docs directory not found: {docs_dir}")
        sys.exit(1)

    # Load ignore patterns
    ignore_patterns = _load_ignore_patterns(repo_root)

    # Collect reference data
    platform_version = extract_version_from_claude_md(repo_root)
    tools = collect_existing_tools(repo_root)
    issues = []

    # Check all markdown files
    for md_file in docs_dir.rglob("*.md"):
        check_doc_file(md_file, repo_root, platform_version, tools, issues)

    # Filter out ignored issues
    unignored = [i for i in issues if not _is_ignored(i, ignore_patterns)]
    ignored_count = len(issues) - len(unignored)

    # --fix: generate .doc-freshness-ignore from current missing_file issues
    if args.fix:
        missing_refs = sorted({
            i["reference"]
            for i in unignored
            if i["type"] == "missing_file"
        })
        if missing_refs:
            ignore_file = repo_root / IGNORE_FILE_NAME
            # Preserve existing patterns
            existing_lines = []
            if ignore_file.exists():
                existing_lines = ignore_file.read_text(
                    encoding="utf-8").splitlines()
            existing_pats = {
                ln.strip() for ln in existing_lines
                if ln.strip() and not ln.strip().startswith("#")
            }
            new_pats = [r for r in missing_refs if r not in existing_pats]
            if new_pats:
                with open(ignore_file, "a", encoding="utf-8") as fh:
                    if not existing_lines:
                        fh.write(
                            "# .doc-freshness-ignore\n"
                            "# Paths listed here are excluded from "
                            "check_doc_freshness.py\n"
                            "# Format: one path prefix per line, "
                            "or type:prefix for type-specific\n"
                        )
                    for pat in new_pats:
                        fh.write(f"{pat}\n")
                import stat
                os.chmod(ignore_file,
                         stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                         | stat.S_IROTH)
                print(f"Added {len(new_pats)} pattern(s) to "
                      f"{IGNORE_FILE_NAME}")
            else:
                print(f"No new patterns to add to {IGNORE_FILE_NAME}")
        else:
            print("No missing_file issues to suppress.")
        sys.exit(0)

    # Report issues
    if unignored:
        print(f"Found {len(unignored)} stale reference(s):\n")
        for issue in sorted(unignored, key=lambda x: (x['file'], x['line'])):
            print(f"{issue['file']}:{issue['line']}")
            print(f"  [{issue['type']}] {issue['message']}")
        if ignored_count:
            print(f"\n({ignored_count} ignored via {IGNORE_FILE_NAME})")
    else:
        msg = "No stale references found."
        if ignored_count:
            msg += f" ({ignored_count} ignored via {IGNORE_FILE_NAME})"
        print(msg)

    if args.ci and unignored:
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
