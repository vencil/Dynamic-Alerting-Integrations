#!/usr/bin/env python3
"""generate_nav.py — 從 docs/ 的 front matter 產生一份 MkDocs nav 草稿

掃描 `.md` 的 front matter（title, tags），依 tags 分類到 nav 區段，印出一份
可以貼進 `mkdocs.yml` 的草稿。

用法:
  python3 scripts/tools/dx/generate_nav.py          # 印出草稿

⚠️ 本工具**只讀不寫也不判對錯**，不會改 `mkdocs.yml`，rc 永遠是 0（除非 `docs/`
不存在）。它是打草稿的，不是閘門。

⛔ **「文件漏收進 nav」的閘門在 `mkdocs.yml`，不在這裡**：`validation.nav.omitted_files`
（檔案在、nav 沒列）與 `validation.nav.not_found`（nav 列了、檔案不在）兩個方向都由
mkdocs 自己在 build 時判定，經 `scripts/tools/lint/mkdocs_strict_check.sh` 轉成 rc，
pre-push 與 CI 的 `MkDocs Build Verification` 共用同一支（#1903）。

⚠️ 曾經有一個 `--check` 旗標宣稱在守這件事，但它比對的兩個集合**從來不在同一個座標
系**：nav 條目相對於 `docs_dir`（`adr/001-….md`），掃描結果相對於 repo root
（`docs/adr/001-….md`），交集實測為 0 ⇒ 它把幾乎每一份文件都報成「missing」，那個數字
沒有人能拿來行動。#1903 之後更是連 nav 區塊都讀不到了（見下）。⇒ 閘門既然已經在
mkdocs 手上，就把這半邊刪掉，不留兩份互相矛盾的答案。

⚠️ 也曾經有一個 `--update` 旗標宣稱會寫回，但它從來沒有實作（`args.update` 從未被
讀取）；#1884 把它刪掉，不留一個說謊的介面。
"""

import argparse
import re
import sys
from pathlib import Path
import os

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_CALLER_ERROR  # noqa: E402

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

# Which docs this draft leaves out.
#
# ⛔ This is NOT the authority. `not_in_nav` in mkdocs.yml is — that is what the
# build-time gate reads. This list only keeps the draft below tidy; nothing is
# enforced from here, so a drift between the two costs a noisy draft, not a
# false green. Kept as plain substrings on purpose: mkdocs.yml cannot be parsed
# with `yaml.safe_load` (it carries `!!python/name:` tags), and a second,
# hand-rolled parse of that file is exactly the failure this tool already had.
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


def is_excluded(path: str) -> bool:
    """Check if path matches exclusion patterns."""
    return any(pat in path for pat in EXCLUDE_PATTERNS)


def scan_docs(docs_dir: Path, repo_root: Path) -> list:
    """Scan all .md files under docs_dir and return their metadata.

    ⛔ `path` is relative to **docs_dir**, not to the repo root — that is the
    coordinate system a MkDocs nav entry is written in, and printing anything
    else makes the draft unusable without hand-editing every line.
    """
    files = []
    for md_file in sorted(docs_dir.rglob('*.md')):
        if md_file.name.endswith('.en.md'):
            continue
        rel = str(md_file.relative_to(docs_dir))
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


def scan_bridged_rule_packs(repo_root: Path) -> list:
    """Scan repo-root `rule-packs/*.md`, which the site serves via a hook.

    ⚠️ `docs/rule-packs/` does not exist on disk; `scripts/mkdocs/rule_packs_bridge.py`
    materialises those pages at build time, so their nav path happens to equal
    their repo-relative path. Everything else outside `docs/` is NOT bridged and
    therefore cannot be a nav entry at all — which is why this function is
    narrow instead of globbing the repo root.
    """
    files = []
    for rp_md in sorted((repo_root / 'rule-packs').glob('*.md')):
        if rp_md.name.endswith('.en.md'):
            continue
        fm = extract_front_matter(rp_md)
        files.append({
            'path': str(rp_md.relative_to(repo_root)),
            'title': fm.get('title', rp_md.stem),
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
    """CLI entry point: 從 docs/ 的 front matter 產生一份 MkDocs nav 草稿."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description='Draft a MkDocs nav skeleton from docs/ front matter '
                    '(read-only; the nav gate lives in mkdocs.yml)'
    )
    parser.add_argument('--repo-root', default='.',
                        help='Repository root directory')
    args = parser.parse_args()

    repo_root = Path(args.repo_root)
    docs_dir = repo_root / 'docs'

    if not docs_dir.exists():
        print(f'Error: docs directory not found: {docs_dir}', file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    all_docs = scan_docs(docs_dir, repo_root)
    all_docs.extend(scan_bridged_rule_packs(repo_root))

    by_section = {}
    for doc in all_docs:
        by_section.setdefault(classify_section(doc['tags']), []).append(doc)

    print(f'Scanned {len(all_docs)} docs')
    print('\n--- nav draft (paths are relative to docs_dir, as mkdocs nav '
          'expects) ---')
    for section, docs in sorted(by_section.items()):
        print(f'\n  {section}:')
        for doc in docs:
            title = doc['title'].strip('"').strip("'")
            print(f'    - {title}: {doc["path"]}')

    sys.exit(EXIT_OK)


if __name__ == '__main__':
    main()
