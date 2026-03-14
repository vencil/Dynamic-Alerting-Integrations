#!/usr/bin/env python3
"""generate_nav.py — 從 docs/ 目錄自動生成 MkDocs nav 結構

掃描所有 .md 檔案的 front matter（title, tags, audience），
自動分類到 nav 區段，並比對 mkdocs.yml 現有 nav 找出遺漏。

用法:
  python3 scripts/tools/generate_nav.py              # 顯示建議 nav
  python3 scripts/tools/generate_nav.py --check      # CI 模式：偵測遺漏
  python3 scripts/tools/generate_nav.py --update      # 更新 mkdocs.yml nav
"""

import argparse
import re
import sys
from pathlib import Path

# Nav section mapping: front matter tags → nav section
SECTION_MAP = {
    'getting-started': '快速入門',
    'architecture': '核心架構',
    'integration': '整合指南',
    'migration': '遷移',
    'governance': '治理與安全',
    'scenario': '場景',
    'reference': '參考',
    'adr': '參考',
}

# Files to exclude from nav (internal, auto-generated, includes)
EXCLUDE_PATTERNS = [
    'internal/',
    'includes/',
    'tags.md',
    'overrides/',
    'assets/',
]


def extract_front_matter(path: Path) -> dict:
    """Extract YAML front matter from a markdown file."""
    try:
        content = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return {}

    match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return {}

    fm = {}
    for line in match.group(1).split('\n'):
        kv = line.split(':', 1)
        if len(kv) == 2:
            key = kv[0].strip()
            val = kv[1].strip()
            fm[key] = val
    return fm


def extract_nav_entries(mkdocs_path: Path) -> set:
    """Extract all file paths referenced in current mkdocs.yml nav."""
    entries = set()
    try:
        content = mkdocs_path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return entries

    in_nav = False
    for line in content.split('\n'):
        if line.strip() == 'nav:':
            in_nav = True
            continue
        if in_nav:
            if line and not line[0].isspace() and ':' in line:
                break
            match = re.search(r':\s*(\S+\.md)\s*$', line)
            if match:
                entries.add(match.group(1))
    return entries


def is_excluded(path: str) -> bool:
    """Check if path matches exclusion patterns."""
    return any(pat in path for pat in EXCLUDE_PATTERNS)


def scan_docs(docs_dir: Path, repo_root: Path) -> list:
    """Scan all .md files and return their metadata."""
    files = []
    for md_file in sorted(docs_dir.rglob('*.md')):
        if md_file.name.endswith('.en.md'):
            continue
        rel = str(md_file.relative_to(repo_root))
        if is_excluded(rel):
            continue
        fm = extract_front_matter(md_file)
        files.append({
            'path': rel,
            'title': fm.get('title', md_file.stem),
            'tags': fm.get('tags', '[]'),
            'lang': fm.get('lang', 'zh'),
        })
    return files


def classify_section(tags_str: str) -> str:
    """Determine nav section from tags."""
    tags_lower = tags_str.lower()
    for tag_key, section in SECTION_MAP.items():
        if tag_key in tags_lower:
            return section
    return '參考'


def main():
    parser = argparse.ArgumentParser(
        description='Auto-generate MkDocs nav from docs/ front matter'
    )
    parser.add_argument('--check', action='store_true',
                        help='CI mode: exit 1 if docs missing from nav')
    parser.add_argument('--update', action='store_true',
                        help='Update mkdocs.yml nav section')
    parser.add_argument('--repo-root', default='.',
                        help='Repository root directory')
    args = parser.parse_args()

    repo_root = Path(args.repo_root)
    docs_dir = repo_root / 'docs'
    mkdocs_path = repo_root / 'mkdocs.yml'

    if not docs_dir.exists():
        print(f'Error: docs directory not found: {docs_dir}', file=sys.stderr)
        sys.exit(1)

    # Scan all docs
    all_docs = scan_docs(docs_dir, repo_root)

    # Also scan rule-packs/ and root-level .md
    for root_md in sorted(repo_root.glob('*.md')):
        if root_md.name.endswith('.en.md'):
            continue
        fm = extract_front_matter(root_md)
        all_docs.append({
            'path': root_md.name,
            'title': fm.get('title', root_md.stem),
            'tags': fm.get('tags', '[]'),
            'lang': fm.get('lang', 'zh'),
        })
    for rp_md in sorted((repo_root / 'rule-packs').glob('*.md')):
        if rp_md.name.endswith('.en.md'):
            continue
        fm = extract_front_matter(rp_md)
        all_docs.append({
            'path': str(rp_md.relative_to(repo_root)),
            'title': fm.get('title', rp_md.stem),
            'tags': fm.get('tags', '[]'),
            'lang': fm.get('lang', 'zh'),
        })

    # Get current nav entries
    current_nav = extract_nav_entries(mkdocs_path)

    # Find missing
    all_paths = {d['path'] for d in all_docs}
    missing = all_paths - current_nav
    extra = current_nav - all_paths

    # Classify missing docs by section
    missing_by_section = {}
    for doc in all_docs:
        if doc['path'] in missing:
            section = classify_section(doc['tags'])
            missing_by_section.setdefault(section, []).append(doc)

    # Report
    print(f'Scanned {len(all_docs)} docs, {len(current_nav)} in nav')
    print(f'Missing from nav: {len(missing)}')

    if missing_by_section:
        print('\n--- Missing from nav ---')
        for section, docs in sorted(missing_by_section.items()):
            print(f'\n  {section}:')
            for doc in docs:
                title = doc['title'].strip('"').strip("'")
                print(f'    - {title}: {doc["path"]}')

    if extra:
        print(f'\nIn nav but not found: {len(extra)}')
        for path in sorted(extra):
            print(f'  - {path}')

    if not missing and not extra:
        print('✓ Nav is complete — all docs are included')

    if args.check and missing:
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
