#!/usr/bin/env python3
"""
Add YAML front matter to documentation files for MkDocs/Docusaurus integration.

Scans .md files in docs/, rule-packs/, and root level, adding front matter with:
- title (extracted from filename or first H1)
- tags (assigned by file pattern)
- audience (assigned by file pattern)
- version (extracted from content or CLAUDE.md)
- lang (zh/en based on filename)

Supports --check and --dry-run modes.
"""

import os
import re
import sys
import argparse
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Tag and audience assignments by file pattern
TAG_ASSIGNMENTS = {
    r'architecture-and-design': {
        'tags': ['architecture', 'core-design'],
        'audience': ['platform-engineer']
    },
    r'benchmarks': {
        'tags': ['performance', 'benchmarks'],
        'audience': ['platform-engineer', 'sre']
    },
    r'governance-security': {
        'tags': ['governance', 'security', 'audit'],
        'audience': ['platform-engineer', 'security']
    },
    r'troubleshooting': {
        'tags': ['troubleshooting', 'operations'],
        'audience': ['platform-engineer', 'sre', 'tenant']
    },
    r'migration-engine': {
        'tags': ['migration', 'ast-engine'],
        'audience': ['platform-engineer', 'devops']
    },
    r'migration-guide': {
        'tags': ['migration', 'getting-started'],
        'audience': ['tenant', 'devops']
    },
    r'byo-prometheus': {
        'tags': ['integration', 'prometheus', 'byop'],
        'audience': ['platform-engineer', 'sre']
    },
    r'byo-alertmanager': {
        'tags': ['integration', 'alertmanager'],
        'audience': ['platform-engineer', 'sre']
    },
    r'shadow-monitoring-sop': {
        'tags': ['migration', 'shadow-monitoring', 'sop'],
        'audience': ['sre', 'platform-engineer']
    },
    r'gitops-deployment': {
        'tags': ['gitops', 'deployment', 'ci-cd'],
        'audience': ['platform-engineer', 'devops']
    },
    r'federation-integration': {
        'tags': ['federation', 'multi-cluster'],
        'audience': ['platform-engineer']
    },
    r'custom-rule-governance': {
        'tags': ['governance', 'custom-rules'],
        'audience': ['platform-engineer']
    },
    r'context-diagram': {
        'tags': ['architecture', 'context-diagram'],
        'audience': ['all']
    },
    r'getting-started/for-platform-engineers': {
        'tags': ['getting-started', 'platform-setup'],
        'audience': ['platform-engineer']
    },
    r'getting-started/for-domain-experts': {
        'tags': ['getting-started', 'domain-config'],
        'audience': ['domain-expert']
    },
    r'getting-started/for-tenants': {
        'tags': ['getting-started', 'tenant-onboard'],
        'audience': ['tenant']
    },
    r'scenarios/alert-routing-split': {
        'tags': ['scenario', 'routing', 'dual-perspective'],
        'audience': ['platform-engineer']
    },
    r'internal/test-coverage-matrix': {
        'tags': ['scenario', 'testing', 'maintenance'],
        'audience': ['platform-engineer', 'sre']
    },
    r'scenarios/shadow-monitoring-cutover': {
        'tags': ['scenario', 'shadow-monitoring', 'cutover'],
        'audience': ['sre', 'devops']
    },
    r'scenarios/multi-cluster-federation': {
        'tags': ['scenario', 'federation', 'multi-cluster'],
        'audience': ['platform-engineer']
    },
    r'scenarios/tenant-lifecycle': {
        'tags': ['scenario', 'tenant-lifecycle'],
        'audience': ['all']
    },
    r'docs/README': {
        'tags': ['index', 'navigation'],
        'audience': ['all']
    },
    r'rule-packs/README': {
        'tags': ['rule-packs', 'reference'],
        'audience': ['all']
    },
    r'rule-packs/ALERT-REFERENCE': {
        'tags': ['alerts', 'reference', 'rule-packs'],
        'audience': ['tenant', 'sre']
    },
}

ROOT_LEVEL_PATTERNS = {
    r'^README\.': {
        'tags': ['overview', 'introduction'],
        'audience': ['all']
    },
    r'^CHANGELOG': {
        'tags': ['changelog', 'releases'],
        'audience': ['all']
    },
}


def detect_language(filepath: str) -> str:
    """Detect language from filename: .en.md -> en, else zh"""
    if filepath.endswith('.en.md'):
        return 'en'
    return 'zh'


def extract_version(filepath: str, base_dir: str) -> str:
    """Extract version from file content or fallback to v1.13.0"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read(2000)  # Read first 2000 chars
            match = re.search(r'v(\d+\.\d+\.\d+)', content)
            if match:
                return f'v{match.group(1)}'
    except OSError:
        pass

    # Fallback: try to read CLAUDE.md for version
    claude_path = os.path.join(base_dir, 'CLAUDE.md')
    if os.path.exists(claude_path):
        try:
            with open(claude_path, 'r', encoding='utf-8') as f:
                content = f.read()
                match = re.search(r'v(\d+\.\d+\.\d+)', content)
                if match:
                    return f'v{match.group(1)}'
        except OSError:
            pass

    return 'v1.13.0'


def extract_title(filepath: str, filename: str) -> str:
    """Extract title from first H1 or use filename"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('# ') and not line.startswith('##'):
                    title = line[2:].strip()
                    return title
    except OSError:
        pass

    # Fallback: use filename without extension
    name = filename.replace('.md', '').replace('.en', '')
    # Convert kebab-case to Title Case
    title = ' '.join(word.capitalize() for word in name.split('-'))
    return title


def get_tags_and_audience(filepath: str) -> Tuple[List[str], List[str]]:
    """Get tags and audience for a file based on pattern matching"""
    # Normalize path separators
    check_path = filepath.replace(os.sep, '/')

    # Check root-level patterns first
    filename = os.path.basename(filepath)
    for pattern, config in ROOT_LEVEL_PATTERNS.items():
        if re.match(pattern, filename):
            return config['tags'], config['audience']

    # Check tag assignments
    for pattern, config in TAG_ASSIGNMENTS.items():
        if re.search(pattern, check_path):
            return config['tags'], config['audience']

    # Default fallback
    return ['documentation'], ['all']


def has_frontmatter(filepath: str) -> bool:
    """Check if file already has YAML front matter"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            return first_line == '---'
    except OSError:
        return False


def read_file_content(filepath: str) -> str:
    """Read file content safely"""
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()


def write_file_content(filepath: str, content: str) -> None:
    """Write file content safely with correct permissions"""
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    os.chmod(filepath, 0o644)


def generate_frontmatter(title: str, tags: List[str], audience: List[str],
                        version: str, lang: str) -> str:
    """Generate YAML front matter block"""
    tags_str = ', '.join(f'"{tag}"' if ' ' in tag else tag for tag in tags)
    audience_str = ', '.join(f'"{aud}"' if ' ' in aud else aud for aud in audience)

    return (
        f'---\n'
        f'title: "{title}"\n'
        f'tags: [{tags_str}]\n'
        f'audience: [{audience_str}]\n'
        f'version: {version}\n'
        f'lang: {lang}\n'
        f'---\n'
    )


def process_file(filepath: str, base_dir: str, dry_run: bool = False) -> Tuple[bool, str]:
    """
    Process a single file, adding front matter if needed.

    Returns: (was_modified, message)
    """
    if has_frontmatter(filepath):
        return False, f"Already has front matter: {filepath}"

    # Extract metadata
    filename = os.path.basename(filepath)
    lang = detect_language(filepath)
    version = extract_version(filepath, base_dir)
    title = extract_title(filepath, filename)
    tags, audience = get_tags_and_audience(filepath)

    # Generate front matter
    frontmatter = generate_frontmatter(title, tags, audience, version, lang)

    if dry_run:
        content = read_file_content(filepath)
        new_content = frontmatter + content
        return True, f"Would add front matter to: {filepath}\n{frontmatter}"
    else:
        content = read_file_content(filepath)
        new_content = frontmatter + content
        write_file_content(filepath, new_content)
        return True, f"Added front matter to: {filepath}"


def find_markdown_files(base_dir: str) -> List[str]:
    """Find all .md files in docs/, rule-packs/, and root"""
    md_files = []

    scan_dirs = [
        os.path.join(base_dir, 'docs'),
        os.path.join(base_dir, 'rule-packs'),
        base_dir  # root level
    ]

    for scan_dir in scan_dirs:
        if not os.path.isdir(scan_dir):
            continue

        for root, dirs, files in os.walk(scan_dir):
            # Skip node_modules and hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'node_modules']

            for file in files:
                if file.endswith('.md'):
                    filepath = os.path.join(root, file)
                    # Only include if in expected locations
                    if (filepath.startswith(os.path.join(base_dir, 'docs')) or
                        filepath.startswith(os.path.join(base_dir, 'rule-packs')) or
                        (os.path.dirname(filepath) == base_dir and
                         file in ['README.md', 'README.en.md', 'CHANGELOG.md'])):
                        md_files.append(filepath)

    return sorted(md_files)


def main():
    """CLI entry point: Add YAML front matter to documentation files for MkDocs/Docusaurus integration."""
    parser = argparse.ArgumentParser(
        description='Add YAML front matter to documentation files'
    )
    parser.add_argument('--check', action='store_true',
                       help='Report files missing front matter and exit with code 1 if any found')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be added without modifying files')
    parser.add_argument('--base-dir', default=os.getcwd(),
                       help='Base directory to scan (default: current directory)')

    args = parser.parse_args()

    base_dir = os.path.abspath(args.base_dir)
    if not os.path.isdir(base_dir):
        logger.error(f"Base directory not found: {base_dir}")
        return 1

    # Find all markdown files
    md_files = find_markdown_files(base_dir)
    logger.info(f"Found {len(md_files)} markdown files")

    if not md_files:
        logger.warning("No markdown files found")
        return 0

    # Process files
    modified_count = 0
    missing_frontmatter = []

    for filepath in md_files:
        was_modified, message = process_file(filepath, base_dir, dry_run=args.dry_run)

        if was_modified:
            modified_count += 1
            if args.dry_run:
                logger.info(message)
            else:
                logger.info(message)
        else:
            if args.check:
                # For check mode, only report files missing frontmatter
                pass
            else:
                logger.debug(message)

    # Count files without front matter for check mode
    if args.check:
        missing_count = 0
        for filepath in md_files:
            if not has_frontmatter(filepath):
                missing_count += 1
                logger.warning(f"Missing front matter: {filepath}")

        if missing_count > 0:
            logger.error(f"Found {missing_count} files missing front matter")
            return 1
        else:
            logger.info("All files have front matter")
            return 0

    # Regular mode: report what was done
    if args.dry_run:
        logger.info(f"Dry run: would modify {modified_count} files")
        return 0
    else:
        logger.info(f"Successfully processed {modified_count} files")
        return 0


if __name__ == '__main__':
    sys.exit(main())
