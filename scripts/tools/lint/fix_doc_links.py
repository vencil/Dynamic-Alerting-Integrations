#!/usr/bin/env python3
"""fix_doc_links.py — Auto-fix broken MkDocs cross-reference links.

Parses mkdocs build warnings and applies systematic fixes:
  1. Redundant subdirectory prefix (adr/adr/X → adr/X)
  2. Wrong relative path from subdirectory to parent
  3. README-root docs/ prefix stripping
  4. External file refs → GitHub blob URLs
  5. Missing CHANGELOG-archive stub

Usage:
    python3 scripts/tools/fix_doc_links.py [--dry-run] [--verbose]
"""
import os
import re
import sys
import argparse

DOCS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'docs')
DOCS_DIR = os.path.normpath(DOCS_DIR)

GITHUB_BLOB = 'https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main'


def get_subdir(filepath):
    """Return the subdirectory of a doc file relative to docs/, or ''."""
    rel = os.path.relpath(filepath, DOCS_DIR)
    parts = rel.replace('\\', '/').split('/')
    if len(parts) > 1:
        return parts[0]
    return ''


def fix_links_in_file(filepath, dry_run=False, verbose=False):
    """Fix broken links in a single markdown file. Returns count of fixes."""
    with open(filepath, 'r', encoding='utf-8') as f:
        original = f.read()

    content = original
    fixes = 0
    rel_path = os.path.relpath(filepath, DOCS_DIR).replace('\\', '/')
    subdir = get_subdir(filepath)

    # --- Pattern 1: Redundant subdirectory prefix ---
    # Files in adr/ linking to adr/XXX.md → should be just XXX.md
    # Files in scenarios/ linking to scenarios/XXX.md → just XXX.md
    # Files in getting-started/ linking to getting-started/XXX.md → just XXX.md
    # Files in internal/ linking to internal/XXX.md → just XXX.md
    # Files in api/ linking to api/XXX.md → just XXX.md
    if subdir in ('adr', 'scenarios', 'getting-started', 'internal', 'api'):
        pattern = re.compile(
            r'(\[(?:[^\]]*)\]\()' + re.escape(subdir) + r'/([^)]+\))',
            re.MULTILINE
        )
        new_content = pattern.sub(r'\1\2', content)
        if new_content != content:
            count = len(pattern.findall(content))
            fixes += count
            if verbose:
                print(f'  [{rel_path}] Removed {count}x redundant "{subdir}/" prefix')
            content = new_content

    # --- Pattern 2: Wrong relative path from subdirectory to parent docs ---
    # Files in adr/ linking to ./architecture-and-design.md → ../architecture-and-design.md
    # Files in adr/ linking to ./context-diagram.md → ../context-diagram.md
    if subdir:
        parent_docs = [
            'architecture-and-design', 'context-diagram', 'migration-guide',
            'benchmarks', 'byo-alertmanager-integration', 'byo-prometheus-integration',
            'cli-reference', 'cheat-sheet', 'custom-rule-governance',
            'federation-integration', 'gitops-deployment', 'glossary',
            'grafana-dashboards', 'shadow-monitoring-sop', 'troubleshooting',
            'governance-security', 'migration-engine', 'CHANGELOG',
        ]
        for doc_name in parent_docs:
            # ./doc.md → ../doc.md  (from subdir)
            for suffix in ['.md', '.en.md']:
                old_ref = f'./{doc_name}{suffix}'
                new_ref = f'../{doc_name}{suffix}'
                # Use negative lookbehind to only match ./ NOT ../
                pat = re.compile(r'(?<!\.)' + re.escape(old_ref))
                if pat.search(content):
                    # Only fix if the target doesn't exist in the subdir
                    target_in_subdir = os.path.join(DOCS_DIR, subdir, f'{doc_name}{suffix}')
                    target_in_parent = os.path.join(DOCS_DIR, f'{doc_name}{suffix}')
                    if not os.path.exists(target_in_subdir) and os.path.exists(target_in_parent):
                        content = pat.sub(new_ref, content)
                        fixes += 1
                        if verbose:
                            print(f'  [{rel_path}] {old_ref} → {new_ref}')

    # --- Pattern 3: README-root files with docs/ prefix ---
    if rel_path.startswith('README-root'):
        pattern = re.compile(r'(\[(?:[^\]]*)\]\()docs/([^)]+\))')
        new_content = pattern.sub(r'\1\2', content)
        if new_content != content:
            count = len(pattern.findall(content))
            fixes += count
            if verbose:
                print(f'  [{rel_path}] Removed {count}x "docs/" prefix')
            content = new_content

    # --- Pattern 4: External file references → GitHub URLs ---
    # ../../scripts/tools/XXX → GitHub blob URL
    ext_patterns = [
        (r'(\[(?:[^\]]*)\]\()(\.\./)*\.\./scripts/tools/([^)]+)\)',
         lambda m: m.group(1) + GITHUB_BLOB + '/scripts/tools/' + m.group(3) + ')'),
        (r'(\[(?:[^\]]*)\]\()(\.\./)*\.\./components/([^)]+)\)',
         lambda m: m.group(1) + GITHUB_BLOB + '/components/' + m.group(3) + ')'),
        (r'(\[(?:[^\]]*)\]\()(\.\./)*\.\./CLAUDE\.md\)',
         lambda m: m.group(1) + GITHUB_BLOB + '/CLAUDE.md)'),
        (r'(\[(?:[^\]]*)\]\()(\.\./)*\.\./CHANGELOG\.md\)',
         lambda m: m.group(1) + GITHUB_BLOB + '/CHANGELOG.md)'),
    ]
    for pat, repl in ext_patterns:
        new_content = re.sub(pat, repl, content)
        if new_content != content:
            fixes += 1
            if verbose:
                print(f'  [{rel_path}] Converted external ref to GitHub URL')
            content = new_content

    # --- Pattern 5: README.en.md self-references ---
    # ../README.en.md from README.en.md → index.md or remove
    if rel_path == 'README.en.md':
        content = content.replace('](../README.en.md)', '](index.md)')
        content = content.replace('](../rule-packs/README.md)', '](rule-packs/README.md)')
        if content != original:
            fixes += 1

    # --- Pattern 6: index.md references ---
    if rel_path == 'index.md':
        content = content.replace('](./README.md)', '](index.md)')
        if content != original:
            fixes += 1

    # Write if changed
    if content != original:
        if not dry_run:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
        return fixes
    return 0


def main():
    parser = argparse.ArgumentParser(description='Fix broken MkDocs links')
    parser.add_argument('--dry-run', action='store_true', help='Preview fixes without writing')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show details')
    args = parser.parse_args()

    total_fixes = 0
    total_files = 0

    for root, _dirs, files in os.walk(DOCS_DIR):
        for fname in files:
            if not fname.endswith('.md'):
                continue
            filepath = os.path.join(root, fname)
            count = fix_links_in_file(filepath, dry_run=args.dry_run, verbose=args.verbose)
            if count > 0:
                total_files += 1
                total_fixes += count

    mode = ' (dry-run)' if args.dry_run else ''
    print(f'\n{total_fixes} fixes in {total_files} files{mode}')


if __name__ == '__main__':
    main()
