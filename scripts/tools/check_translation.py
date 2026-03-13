#!/usr/bin/env python3
"""check_translation.py — 自動化翻譯品質檢查

比對中英文文件的結構一致性（標題、程式碼區塊、表格、圖表數量），
偵測翻譯遺漏或結構偏移。

用法:
  python3 scripts/tools/check_translation.py              # 顯示報告
  python3 scripts/tools/check_translation.py --ci          # CI 模式
  python3 scripts/tools/check_translation.py --verbose     # 詳細比較
"""

import re
import sys
import os
from pathlib import Path
from typing import Dict, List, Tuple
import argparse


def count_headings(content: str) -> Dict[int, int]:
    """Count H1, H2, H3 headings in markdown content."""
    counts = {1: 0, 2: 0, 3: 0}
    for line in content.split('\n'):
        match = re.match(r'^(#{1,3})\s+', line)
        if match:
            level = len(match.group(1))
            if level in counts:
                counts[level] += 1
    return counts


def count_code_blocks(content: str) -> int:
    """Count ``` fenced code blocks."""
    return len(re.findall(r'```', content)) // 2


def count_mermaid_diagrams(content: str) -> int:
    """Count mermaid diagrams (```mermaid ... ```)."""
    return len(re.findall(r'```mermaid', content))


def count_tables(content: str) -> int:
    """Count markdown tables (lines with |)."""
    count = 0
    in_table = False
    for line in content.split('\n'):
        stripped = line.strip()
        if '|' in stripped and not stripped.startswith('|'):
            continue
        if '|' in stripped:
            if not in_table:
                in_table = True
                count += 1
            continue
        else:
            in_table = False
    return count


def count_links(content: str) -> int:
    """Count markdown links [text](url)."""
    return len(re.findall(r'\[([^\]]+)\]\(([^\)]+)\)', content))


def read_file(path: Path) -> str:
    """Read file with UTF-8 encoding."""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def extract_front_matter(content: str) -> Dict:
    """Extract YAML front matter fields from markdown content."""
    fm = {}
    match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return fm
    for line in match.group(1).split('\n'):
        kv = line.split(':', 1)
        if len(kv) == 2:
            fm[kv[0].strip()] = kv[1].strip()
    return fm


def compare_front_matter(zh_fm: Dict, en_fm: Dict) -> List[str]:
    """Compare front matter fields that should match between zh and en."""
    issues = []
    for key in ('tags', 'audience', 'version'):
        zh_val = zh_fm.get(key, '')
        en_val = en_fm.get(key, '')
        if zh_val != en_val:
            issues.append(f'front_matter.{key}: ZH={zh_val} vs EN={en_val}')
    # lang should differ
    if zh_fm.get('lang', '') == en_fm.get('lang', ''):
        issues.append(f'front_matter.lang: both are "{zh_fm.get("lang", "")}" (should differ)')
    return issues


def analyze_file(path: Path) -> Dict:
    """Analyze markdown file structure."""
    content = read_file(path)
    headings = count_headings(content)
    return {
        'path': str(path),
        'h1': headings.get(1, 0),
        'h2': headings.get(2, 0),
        'h3': headings.get(3, 0),
        'code_blocks': count_code_blocks(content),
        'mermaid': count_mermaid_diagrams(content),
        'tables': count_tables(content),
        'links': count_links(content),
        'front_matter': extract_front_matter(content),
    }


def find_bilingual_pairs(docs_dir: Path) -> List[Tuple[Path, Path]]:
    """Find .md and .en.md file pairs."""
    pairs = []
    zh_files = {}

    for file_path in docs_dir.rglob('*.md'):
        if file_path.name.endswith('.en.md'):
            continue
        base_name = file_path.name
        zh_files[base_name] = file_path

    for file_path in docs_dir.rglob('*.en.md'):
        base_name = file_path.name.replace('.en.md', '.md')
        if base_name in zh_files:
            pairs.append((zh_files[base_name], file_path))

    return sorted(pairs)


def check_link_variance(zh_links: int, en_links: int) -> bool:
    """Check if link counts are within 20% variance."""
    if zh_links == 0 and en_links == 0:
        return True
    if zh_links == 0 or en_links == 0:
        return False
    variance = abs(zh_links - en_links) / max(zh_links, en_links)
    return variance <= 0.20


def compare_files(zh_analysis: Dict, en_analysis: Dict) -> Dict:
    """Compare two file analyses for structural consistency."""
    mismatches = {}

    if zh_analysis['h1'] != en_analysis['h1']:
        mismatches['h1'] = (zh_analysis['h1'], en_analysis['h1'])

    if zh_analysis['h2'] != en_analysis['h2']:
        mismatches['h2'] = (zh_analysis['h2'], en_analysis['h2'])

    if zh_analysis['h3'] != en_analysis['h3']:
        mismatches['h3'] = (zh_analysis['h3'], en_analysis['h3'])

    if zh_analysis['code_blocks'] != en_analysis['code_blocks']:
        mismatches['code_blocks'] = (zh_analysis['code_blocks'], en_analysis['code_blocks'])

    if zh_analysis['mermaid'] != en_analysis['mermaid']:
        mismatches['mermaid'] = (zh_analysis['mermaid'], en_analysis['mermaid'])

    if zh_analysis['tables'] != en_analysis['tables']:
        mismatches['tables'] = (zh_analysis['tables'], en_analysis['tables'])

    if not check_link_variance(zh_analysis['links'], en_analysis['links']):
        mismatches['links'] = (zh_analysis['links'], en_analysis['links'])

    return mismatches


def format_element_name(key: str) -> str:
    """Format element key for display."""
    mapping = {
        'h1': 'H1 Headings',
        'h2': 'H2 Headings',
        'h3': 'H3 Headings',
        'code_blocks': 'Code Blocks',
        'mermaid': 'Mermaid Diagrams',
        'tables': 'Tables',
        'links': 'Markdown Links',
    }
    return mapping.get(key, key)


def main():
    parser = argparse.ArgumentParser(
        description='Validate bilingual markdown document consistency'
    )
    parser.add_argument('--ci', action='store_true', help='CI mode (exit 1 if mismatches)')
    parser.add_argument('--verbose', action='store_true', help='Show detailed per-file comparison')
    parser.add_argument('--docs-dir', default='docs', help='Path to docs directory')
    args = parser.parse_args()

    docs_dir = Path(args.docs_dir)
    if not docs_dir.exists():
        print(f'Error: docs directory not found: {docs_dir}', file=sys.stderr)
        sys.exit(1)

    pairs = find_bilingual_pairs(docs_dir)

    if not pairs:
        print('No bilingual document pairs found')
        sys.exit(0)

    total_mismatches = 0
    mismatch_details = []

    fm_issues_total = 0

    for zh_file, en_file in pairs:
        zh_analysis = analyze_file(zh_file)
        en_analysis = analyze_file(en_file)
        mismatches = compare_files(zh_analysis, en_analysis)
        fm_issues = compare_front_matter(
            zh_analysis.get('front_matter', {}),
            en_analysis.get('front_matter', {}),
        )

        if args.verbose:
            base_name = zh_file.name
            print(f'\n{base_name}:')
            print(f'  ZH: H1={zh_analysis["h1"]} H2={zh_analysis["h2"]} H3={zh_analysis["h3"]} Code={zh_analysis["code_blocks"]} Mermaid={zh_analysis["mermaid"]} Tables={zh_analysis["tables"]} Links={zh_analysis["links"]}')
            print(f'  EN: H1={en_analysis["h1"]} H2={en_analysis["h2"]} H3={en_analysis["h3"]} Code={en_analysis["code_blocks"]} Mermaid={en_analysis["mermaid"]} Tables={en_analysis["tables"]} Links={en_analysis["links"]}')
            if fm_issues:
                for issue in fm_issues:
                    print(f'  ⚠ {issue}')

        if fm_issues:
            fm_issues_total += len(fm_issues)

        if mismatches:
            total_mismatches += 1
            detail = {
                'zh_file': zh_file.name,
                'en_file': en_file.name,
                'mismatches': mismatches,
                'fm_issues': fm_issues,
            }
            mismatch_details.append(detail)
        elif fm_issues:
            total_mismatches += 1
            mismatch_details.append({
                'zh_file': zh_file.name,
                'en_file': en_file.name,
                'mismatches': {},
                'fm_issues': fm_issues,
            })

    if mismatch_details:
        print(f'\nMismatches Found: {total_mismatches} file pair(s)\n')
        for detail in mismatch_details:
            print(f'{detail["zh_file"]} vs {detail["en_file"]}:')
            for key, (zh_val, en_val) in detail['mismatches'].items():
                element_name = format_element_name(key)
                print(f'  {element_name}: ZH={zh_val} vs EN={en_val}')
            for issue in detail.get('fm_issues', []):
                print(f'  ⚠ {issue}')
            print()

        if args.ci:
            sys.exit(1)
    else:
        if args.verbose or not args.ci:
            print(f'✓ All {len(pairs)} document pairs have consistent structure and front matter')
        sys.exit(0)


if __name__ == '__main__':
    main()
