#!/usr/bin/env python3
"""doc_impact.py — 文件變更影響分析

分析 PR 中文件變更的影響範圍：受影響角色、相關文件、需同步的雙語對。

用法:
  python3 scripts/tools/doc_impact.py docs/architecture-and-design.md
  git diff --name-only main | python3 scripts/tools/doc_impact.py --stdin
  python3 scripts/tools/doc_impact.py --ci docs/byo-prometheus-integration.md
  python3 scripts/tools/doc_impact.py --json docs/*.md
  python3 scripts/tools/doc_impact.py --docs-dir docs --ci

輸出:
  標準輸出: 人類可讀的影響報告
  JSON 格式 (--json): 機器可讀的結構化報告
  CI 模式 (--ci): 檢測到雙語同步需求時 exit 1
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional


# =============================================================================
# Front Matter Parsing
# =============================================================================

def parse_front_matter(content: str) -> Tuple[Dict, str]:
    """Parse YAML front matter from markdown.

    Returns: (front_matter_dict, body_content)
    """
    if not content.startswith('---'):
        return {}, content

    # Find closing ---
    try:
        end_match = re.search(r'\n---\n', content)
        if not end_match:
            return {}, content

        front_matter_str = content[4:end_match.start()]
        body = content[end_match.end():]

        # Simple YAML parsing (avoiding yaml library for minimal deps)
        fm = {}
        for line in front_matter_str.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if ':' not in line:
                continue

            key, value = line.split(':', 1)
            key = key.strip()
            value = value.strip()

            # Handle YAML arrays [item1, item2]
            if value.startswith('[') and value.endswith(']'):
                value = [v.strip() for v in value[1:-1].split(',')]
            elif value.lower() in ('true', 'false'):
                value = value.lower() == 'true'

            fm[key] = value

        return fm, body
    except re.error:
        return {}, content


def read_file_utf8(path: Path) -> str:
    """Read file with UTF-8 encoding."""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


# =============================================================================
# File Metadata Extraction
# =============================================================================

def extract_file_metadata(path: Path) -> Dict:
    """Extract front matter metadata from a doc file."""
    if not path.exists():
        return {}

    content = read_file_utf8(path)
    fm, body = parse_front_matter(content)

    return {
        'path': path,
        'name': path.name,
        'base_name': path.stem,  # name without extension
        'front_matter': fm,
        'body': body,
        'audience': fm.get('audience', []),
        'tags': fm.get('tags', []),
        'version': fm.get('version'),
        'lang': fm.get('lang', 'unknown'),
        'title': fm.get('title', ''),
    }


def get_bilingual_pair(path: Path) -> Optional[Path]:
    """Get the bilingual pair of a doc file.

    docs/foo.md <-> docs/foo.en.md
    """
    if path.name.endswith('.en.md'):
        # This is English, find Chinese
        zh_path = path.parent / path.name.replace('.en.md', '.md')
        return zh_path if zh_path.exists() else None
    elif path.name.endswith('.md'):
        # This is Chinese, find English
        en_path = path.parent / (path.stem + '.en.md')
        return en_path if en_path.exists() else None
    return None


# =============================================================================
# Link Extraction and Relationship Finding
# =============================================================================

def extract_doc_links(content: str) -> Set[str]:
    """Extract all markdown links to doc files from content.

    Returns set of filenames like 'architecture-and-design.md', 'benchmarks.en.md'
    """
    links = set()

    # Match [text](path) patterns
    pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    for match in re.finditer(pattern, content):
        url = match.group(2)
        # Extract filename (handle relative paths like ../foo.md, ../docs/foo.md)
        url_path = Path(url)
        if url_path.suffix == '.md':
            # Use just the filename for matching
            links.add(url_path.name)

    return links


def find_related_docs(
    changed_file_metadata: Dict,
    all_metadata: Dict[str, Dict],
    docs_dir: Path
) -> Dict[str, List[str]]:
    """Find docs related to a changed file by tags and cross-references.

    Returns:
    {
        'same_tags': [list of filenames],
        'references_changed': [list of filenames that link to changed file],
        'referenced_by_changed': [list of filenames referenced by changed file],
    }
    """
    result = {
        'same_tags': [],
        'references_changed': [],
        'referenced_by_changed': [],
    }

    changed_name = changed_file_metadata['name']
    changed_tags = set(changed_file_metadata['tags'])

    # Extract links from changed file
    changed_links = extract_doc_links(changed_file_metadata['body'])

    for other_name, other_meta in all_metadata.items():
        if other_name == changed_name:
            continue

        other_tags = set(other_meta['tags'])

        # Same tags?
        if changed_tags and other_tags and (changed_tags & other_tags):
            result['same_tags'].append(other_name)

        # Does other file reference changed file?
        other_links = extract_doc_links(other_meta['body'])
        if changed_name in other_links:
            result['references_changed'].append(other_name)

        # Does changed file reference other file?
        if other_name in changed_links:
            result['referenced_by_changed'].append(other_name)

    # Deduplicate
    for key in result:
        result[key] = sorted(list(set(result[key])))

    return result


# =============================================================================
# Report Generation
# =============================================================================

def generate_text_report(
    changed_files: List[Dict],
    impact_analysis: Dict[str, Dict],
    bilingual_sync_needed: bool
) -> str:
    """Generate human-readable impact report."""

    lines = []
    lines.append('=' * 70)
    lines.append('文件變更影響分析 (Documentation Change Impact Analysis)')
    lines.append('=' * 70)
    lines.append('')

    # Summary
    lines.append(f'變更文件數: {len(changed_files)}')
    if bilingual_sync_needed:
        lines.append('⚠  偵測到雙語同步需求')
    lines.append('')

    # Changed files section
    lines.append('📝 變更文件 (Changed Files)')
    lines.append('-' * 70)
    for cf in changed_files:
        lines.append(f"  {cf['name']}")
        if cf['audience']:
            lines.append(f"    受眾: {', '.join(cf['audience'])}")
        if cf['tags']:
            lines.append(f"    標籤: {', '.join(cf['tags'])}")
        if cf['version']:
            lines.append(f"    版本: {cf['version']}")

        # Bilingual info
        pair = get_bilingual_pair(cf['path'])
        if pair:
            if pair.exists():
                lines.append(f"    雙語對: {pair.name} (需同步)")
            else:
                lines.append(f"    雙語對: 缺失 ({pair.name})")

        # Impact analysis
        impact = impact_analysis.get(cf['name'], {})
        if impact.get('same_tags'):
            lines.append(f"    相同標籤的文件:")
            for related in impact['same_tags']:
                lines.append(f"      • {related}")

        if impact.get('references_changed'):
            lines.append(f"    參考此文件的文件:")
            for ref_by in impact['references_changed']:
                lines.append(f"      • {ref_by}")

        if impact.get('referenced_by_changed'):
            lines.append(f"    此文件參考的文件:")
            for ref in impact['referenced_by_changed']:
                lines.append(f"      • {ref}")

        lines.append('')

    # Bilingual pairs needing sync
    bilingual_pairs = []
    for cf in changed_files:
        pair = get_bilingual_pair(cf['path'])
        if pair and pair.exists():
            bilingual_pairs.append((cf['name'], pair.name))

    if bilingual_pairs:
        lines.append('🌍 雙語文件對需同步 (Bilingual Sync Required)')
        lines.append('-' * 70)
        for zh_file, en_file in bilingual_pairs:
            lines.append(f"  {zh_file} ↔ {en_file}")
        lines.append('')

    # Summary statistics
    all_related = set()
    for impact in impact_analysis.values():
        all_related.update(impact.get('same_tags', []))
        all_related.update(impact.get('references_changed', []))
        all_related.update(impact.get('referenced_by_changed', []))

    lines.append('📊 影響統計')
    lines.append('-' * 70)
    lines.append(f"  直接變更: {len(changed_files)} 個文件")
    lines.append(f"  相關文件: {len(all_related)} 個文件")
    if bilingual_pairs:
        lines.append(f"  雙語對: {len(bilingual_pairs)} 對")
    lines.append('')

    # Recommendations
    lines.append('💡 建議')
    lines.append('-' * 70)
    if bilingual_pairs:
        lines.append("  1. 同步雙語文件翻譯")
    if all_related:
        lines.append("  2. 檢查相關文件的內部連結是否需要更新")
        lines.append("  3. 驗證受影響角色的文件組合是否邏輯一致")
    lines.append('')

    lines.append('=' * 70)

    return '\n'.join(lines)


def generate_json_report(
    changed_files: List[Dict],
    impact_analysis: Dict[str, Dict],
    bilingual_sync_needed: bool
) -> str:
    """Generate machine-readable JSON report."""

    report = {
        'summary': {
            'changed_files_count': len(changed_files),
            'bilingual_sync_needed': bilingual_sync_needed,
        },
        'changed_files': [],
        'related_files': {},
        'bilingual_pairs': [],
    }

    # Changed files
    for cf in changed_files:
        report['changed_files'].append({
            'name': cf['name'],
            'audience': cf['audience'],
            'tags': cf['tags'],
            'version': cf['version'],
            'lang': cf['lang'],
        })

    # Impact analysis
    for cf in changed_files:
        impact = impact_analysis.get(cf['name'], {})
        report['related_files'][cf['name']] = impact

    # Bilingual pairs
    for cf in changed_files:
        pair = get_bilingual_pair(cf['path'])
        if pair and pair.exists():
            report['bilingual_pairs'].append({
                'zh': cf['name'],
                'en': pair.name,
            })

    return json.dumps(report, indent=2, ensure_ascii=False)


# =============================================================================
# Main
# =============================================================================

def main():
    """CLI entry point: 文件變更影響分析."""
    parser = argparse.ArgumentParser(
        description='分析文件變更的影響範圍 (Analyze doc change impact)'
    )
    parser.add_argument(
        'files',
        nargs='*',
        help='Changed doc files (default: read from stdin with --stdin)'
    )
    parser.add_argument(
        '--stdin',
        action='store_true',
        help='Read changed filenames from stdin (one per line)'
    )
    parser.add_argument(
        '--docs-dir',
        default='docs',
        help='Documentation directory path (default: docs)'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Output as JSON'
    )
    parser.add_argument(
        '--ci',
        action='store_true',
        help='CI mode: exit 1 if bilingual sync needed'
    )

    args = parser.parse_args()

    # Get list of changed files
    changed_file_paths = []

    if args.stdin:
        # Read from stdin
        for line in sys.stdin:
            line = line.strip()
            if line:
                changed_file_paths.append(line)
    else:
        changed_file_paths = args.files

    if not changed_file_paths:
        print('Error: No files specified', file=sys.stderr)
        sys.exit(1)

    docs_dir = Path(args.docs_dir)
    if not docs_dir.exists():
        print(f'Error: docs directory not found: {docs_dir}', file=sys.stderr)
        sys.exit(1)

    # Load all doc metadata
    all_metadata = {}
    for doc_file in docs_dir.rglob('*.md'):
        meta = extract_file_metadata(doc_file)
        if meta:
            all_metadata[doc_file.name] = meta

    # Process changed files
    changed_files = []
    for file_path_str in changed_file_paths:
        file_path = Path(file_path_str)

        # If path is relative, resolve relative to docs_dir
        if not file_path.is_absolute():
            resolved_path = docs_dir / file_path
        else:
            resolved_path = file_path

        if not resolved_path.exists():
            # Try just the filename
            for doc_file in docs_dir.rglob(file_path.name if file_path.name else file_path.stem + '.md'):
                resolved_path = doc_file
                break

        if resolved_path.exists():
            meta = extract_file_metadata(resolved_path)
            if meta:
                changed_files.append(meta)

    if not changed_files:
        print(f'Warning: No valid doc files found in {changed_file_paths}', file=sys.stderr)
        if args.json:
            print(json.dumps({'error': 'No valid doc files found'}, indent=2))
        sys.exit(0)

    # Analyze impact
    impact_analysis = {}
    for cf in changed_files:
        impact_analysis[cf['name']] = find_related_docs(cf, all_metadata, docs_dir)

    # Check if bilingual sync needed
    bilingual_sync_needed = False
    for cf in changed_files:
        pair = get_bilingual_pair(cf['path'])
        if pair and pair.exists():
            bilingual_sync_needed = True
            break

    # Generate output
    if args.json:
        output = generate_json_report(changed_files, impact_analysis, bilingual_sync_needed)
        print(output)
    else:
        output = generate_text_report(changed_files, impact_analysis, bilingual_sync_needed)
        print(output)

    # CI mode
    if args.ci and bilingual_sync_needed:
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
